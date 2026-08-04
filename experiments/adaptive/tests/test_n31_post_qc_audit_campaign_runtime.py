"""Non-live orchestration tests for the frozen N=31 PQAR campaign runner."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

import pytest

from experiments.adaptive import run_n31_post_qc_audit_campaign as runner
from experiments.adaptive.kauri_experiment import n31_post_qc_audit as pqar
from experiments.adaptive.kauri_experiment import profiled_fault_runtime as runtime
from experiments.adaptive.kauri_experiment.n31_post_qc_audit_campaign import (
    CAMPAIGN_PROFILE_SHA256,
    N31PostQcAuditCampaignError,
    derive_frozen_campaign_schedule,
    load_frozen_campaign_profile,
    source_blind_order_key,
    validate_n31_pqar_campaign,
)
from experiments.adaptive.kauri_experiment.n31_static_diagnosis_runtime import (
    TrustedBinary,
    TrustedProvenance,
)
from experiments.adaptive.kauri_experiment.profiled_fault_archive import (
    create_evidence_seal,
    verify_evidence_seal,
)

REPOSITORY = Path(__file__).resolve().parents[3]
CAMPAIGN_PROFILE = (
    REPOSITORY / "experiments/adaptive/profiles/n31-f5-post-qc-audit-campaign-v2.json"
)
AUDIT_PROFILE = (
    REPOSITORY / "experiments/adaptive/profiles/n31-f5-post-qc-audit-v4.json"
)


def test_campaign_cli_defaults_select_prospective_v2_and_v4() -> None:
    revision = "a" * 40

    assert runner.DEFAULT_CAMPAIGN_PROFILE == CAMPAIGN_PROFILE
    assert runner.DEFAULT_AUDIT_PROFILE == AUDIT_PROFILE
    assert runner.DEFAULT_RESULTS_PARENT == (
        REPOSITORY / "results/n31-post-qc-audit-campaign-v2"
    )
    assert runner._default_campaign_root(REPOSITORY, revision) == (
        runner.DEFAULT_RESULTS_PARENT / "aaaaaaaa-seed41719-campaign-v2"
    )


def _trusted(tmp_path: Path) -> TrustedProvenance:
    binaries = tuple(
        TrustedBinary(
            name=name,
            path=str((tmp_path / "bin" / name).resolve()),
            size_bytes=1,
            sha256=(f"{index:x}" * 64)[:64],
        )
        for index, name in enumerate(
            ("app", "epoch_profile_digest", "keygen", "manager", "tls_keygen"),
            start=1,
        )
    )
    return TrustedProvenance(
        revision="a" * 40,
        required_branch=runtime.REQUIRED_BRANCH,
        remote_tracking_ref=f"origin/{runtime.REQUIRED_BRANCH}",
        repository_clean=True,
        head_equals_remote=True,
        repository=str(REPOSITORY.resolve()),
        build_directory=str((tmp_path / "build").resolve()),
        build_provenance_file_sha256="b" * 64,
        build_provenance_document_sha256="c" * 64,
        binaries=binaries,
    )


def _pilot_result(trusted: TrustedProvenance) -> dict[str, object]:
    return {
        "schema_version": 1,
        "scenario": pqar.SCENARIO,
        "kind": "fresh-three-arm-pilot",
        "verdict": "PASS",
        "preflight_revision": trusted.revision,
        "preflight_profile_sha256": pqar.SHIPPED_PROFILE_SHA256,
        "evidence_tree_sha256": "d" * 64,
        "evidence_seal_sha256": "e" * 64,
        "figure_eligible": False,
    }


def _preflight(trusted: TrustedProvenance) -> dict[str, object]:
    return {
        "schema_version": 1,
        "scenario": pqar.SCENARIO,
        "verdict": "PASS",
        "revision": trusted.revision,
        "audit_profile": {
            "profile_id": pqar.SHIPPED_PROFILE_ID,
            "sha256": pqar.SHIPPED_PROFILE_SHA256,
        },
        "runtime_profile": {
            "profile_id": "n31-f5-q21-internal1-sigkill-shakedown-v1",
            "sha256": (
                "0defdaa9b69c949365eea3b3029da75cee3ea8334f845401103e2f7af8507650"
            ),
            "path": str(
                REPOSITORY / "experiments/adaptive/profiles/"
                "n31-f5-internal1-crash-shakedown-v1.json"
            ),
        },
    }


def _campaign_arguments(
    tmp_path: Path,
    *,
    trusted: TrustedProvenance,
    campaign_root: Path,
) -> dict[str, Any]:
    return {
        "campaign_profile_path": CAMPAIGN_PROFILE,
        "audit_profile_path": AUDIT_PROFILE,
        "pilot_sequence_directory": tmp_path / "sealed-pilot",
        "trusted_provenance": trusted,
        "repository": REPOSITORY,
        "campaign_root": campaign_root,
        "app_binary": tmp_path / "bin/app",
        "manager_binary": tmp_path / "bin/manager",
        "keygen_binary": tmp_path / "bin/keygen",
        "tls_keygen_binary": tmp_path / "bin/tls_keygen",
        "epoch_profile_digest_binary": tmp_path / "bin/epoch_profile_digest",
        "build_directory": tmp_path / "build",
        "build_provenance_path": tmp_path / "build/build-provenance.json",
    }


def _execute_mock_campaign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    outcomes: tuple[str, ...] | None = None,
    raise_at: int | None = None,
    raised_error: BaseException | None = None,
    expected_run_calls: int | None = None,
    expected_blind_calls: int | None = None,
    blind_attack: str | None = None,
) -> dict[str, Any]:
    trusted = _trusted(tmp_path)
    profile = load_frozen_campaign_profile(CAMPAIGN_PROFILE)
    schedule = derive_frozen_campaign_schedule(profile)
    campaign_root = tmp_path / "campaign"
    if outcomes is None:
        outcomes = tuple(
            (
                "FAIL"
                if ordinal % 11 == 0
                else "INCOMPLETE" if ordinal % 7 == 0 else "PASS"
            )
            for ordinal in range(1, 91)
        )
    run_calls: list[tuple[str, Path]] = []
    blind_calls: list[Path] = []
    blind_contexts: list[dict[str, object]] = []
    child_outcomes: dict[str, str] = {}
    child_arms: dict[str, str] = {}
    build_calls: list[dict[str, Any]] = []
    preflight_calls: list[dict[str, Any]] = []
    pilot_calls: list[tuple[Path, TrustedProvenance]] = []
    events: list[str] = []
    if expected_run_calls is None:
        expected_run_calls = raise_at if raise_at is not None else 90
    if expected_blind_calls is None:
        expected_blind_calls = raise_at - 1 if raise_at is not None else 90

    def prepare_build(**kwargs: Any) -> None:
        build_calls.append(kwargs)

    def derive_provenance(**_kwargs: Any) -> TrustedProvenance:
        return trusted

    def preflight(**kwargs: Any) -> dict[str, object]:
        preflight_calls.append(kwargs)
        return _preflight(trusted)

    def validate_pilot(
        sequence_directory: Path,
        *,
        trusted_provenance: TrustedProvenance,
    ) -> dict[str, object]:
        pilot_calls.append((sequence_directory, trusted_provenance))
        return _pilot_result(trusted)

    def run_once(*, arm: str, results_root: Path, **kwargs: Any) -> tuple[Path, str]:
        ordinal = len(run_calls) + 1
        assert kwargs["trusted_provenance"] is trusted
        assert (campaign_root / "intent/evidence-seal.json").is_file()
        verify_evidence_seal(campaign_root / "intent")
        run_calls.append((arm, results_root))
        events.append(f"run-{ordinal:03d}")
        if raise_at == ordinal:
            error = raised_error or RuntimeError("injected infrastructure failure")
            raise error
        child = results_root / f"20260804T000000Z-12345-{ordinal:08x}"
        child.mkdir(mode=0o700)
        classification = profile.expected_classification(arm)
        (child / "raw.txt").write_text(
            json.dumps(
                {
                    "classification": classification,
                    "predicted_classification": classification,
                    "metrics_ns": {
                        "qc_to_deadline_slack_ns": ordinal,
                        "target_to_deadline_slack_ns": ordinal + 1,
                        "relay_to_root_latency_ns": ordinal + 2,
                        "root_verification_latency_ns": ordinal + 3,
                        "qc_to_audit_latency_ns": ordinal + 4,
                        "expiry_to_later_commit_latency_ns": ordinal + 5,
                    },
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        create_evidence_seal(child)
        child_outcomes[child.name] = outcomes[ordinal - 1]
        child_arms[child.name] = arm
        return child, outcomes[ordinal - 1]

    def classify_source_blind(
        run_directory: Path,
        *,
        trusted_provenance: TrustedProvenance,
    ) -> dict[str, object]:
        assert trusted_provenance is trusted
        # The first blind call occurs only after every permitted live launch.
        assert len(run_calls) == expected_run_calls
        assert campaign_root not in run_directory.parents
        assert run_directory.parent.name.startswith("kauri-pqar-source-blind-")
        assert {path.name for path in run_directory.parent.iterdir()} == {
            run_directory.name
        }
        payload = json.loads((run_directory / "raw.txt").read_text(encoding="utf-8"))
        predicted = payload["predicted_classification"]
        if blind_attack == "call_counter":
            guessed_arm = str(schedule[len(blind_calls)]["arm"])
            predicted = profile.expected_classification(guessed_arm)
        elif blind_attack == "timestamp_path":
            timestamped = re.fullmatch(
                r"[0-9]{8}T[0-9]{6}Z-[1-9][0-9]*-([0-9a-f]{8})",
                run_directory.name,
            )
            if timestamped is None:
                predicted = "unclassified"
            else:
                guessed_ordinal = int(timestamped.group(1), 16)
                guessed_arm = str(schedule[guessed_ordinal - 1]["arm"])
                predicted = profile.expected_classification(guessed_arm)
        blind_calls.append(run_directory)
        blind_contexts.append(
            {
                "campaign_visible": campaign_root in run_directory.parents,
                "sibling_count": len(list(run_directory.parent.iterdir())),
            }
        )
        events.append(f"blind-{len(blind_calls):03d}")
        return {
            **payload,
            "classification": predicted,
            "predicted_classification": predicted,
            "child": {
                "path": str(run_directory),
                "profile_sha256": CAMPAIGN_PROFILE_SHA256,
            },
        }

    real_summarize = runner.summarize_n31_pqar_campaign

    def guarded_summarize(*args: Any, **kwargs: Any) -> dict[str, object]:
        assert len(blind_calls) == expected_blind_calls
        events.append("truth-join")
        return real_summarize(*args, **kwargs)

    monkeypatch.setattr(runner, "summarize_n31_pqar_campaign", guarded_summarize)
    error: runner.N31PostQcAuditCampaignInterrupted | None = None
    summary_path: Path | None = None
    try:
        summary_path = runner.run_campaign(
            **_campaign_arguments(
                tmp_path, trusted=trusted, campaign_root=campaign_root
            ),
            prepare_build=prepare_build,
            derive_provenance=derive_provenance,
            preflight=preflight,
            validate_pilot=validate_pilot,
            run_once=run_once,
            classify_source_blind=classify_source_blind,
            validate_after_seal=False,
        )
    except runner.N31PostQcAuditCampaignInterrupted as caught:
        error = caught
    return {
        "trusted": trusted,
        "profile": profile,
        "schedule": schedule,
        "campaign_root": campaign_root,
        "summary_path": summary_path,
        "error": error,
        "outcomes": outcomes,
        "run_calls": run_calls,
        "blind_calls": blind_calls,
        "blind_contexts": blind_contexts,
        "child_outcomes": child_outcomes,
        "child_arms": child_arms,
        "build_calls": build_calls,
        "preflight_calls": preflight_calls,
        "pilot_calls": pilot_calls,
        "events": events,
        "classify": classify_source_blind,
        "validate_pilot": validate_pilot,
    }


def test_runner_seals_intent_before_slot_one_and_never_branches_on_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_rglob(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("campaign runner must never discover verdicts with rglob")

    monkeypatch.setattr(Path, "rglob", forbidden_rglob)
    result = _execute_mock_campaign(tmp_path, monkeypatch)
    root = result["campaign_root"]
    summary = json.loads(result["summary_path"].read_text(encoding="utf-8"))

    assert result["error"] is None
    assert len(result["build_calls"]) == 1
    assert len(result["preflight_calls"]) == 1
    assert result["pilot_calls"] == [(tmp_path / "sealed-pilot", result["trusted"])]
    assert [arm for arm, _ in result["run_calls"]] == [
        slot["arm"] for slot in result["schedule"]
    ]
    assert len(result["run_calls"]) == 90
    assert {results_root for _, results_root in result["run_calls"]} == {
        root / "children"
    }
    assert len(result["blind_calls"]) == 90
    assert (
        result["blind_contexts"]
        == [{"campaign_visible": False, "sibling_count": 1}] * 90
    )
    assert result["events"].index("blind-001") > result["events"].index("run-090")
    assert result["events"][-1] == "truth-join"
    assert summary["campaign_acceptance"] == "ACCEPTED"
    assert summary["outcome_status"] == "MIXED"
    assert summary["figure_eligible"] is True
    assert sum(summary["outcome_counts"].values()) == 90
    assert summary["invocation_status_counts"] == {
        "returned": 90,
        "raised": 0,
        "not_started": 0,
    }
    assert len(list((root / "starts").iterdir())) == 90
    assert len(list((root / "executions").iterdir())) == 90
    persisted_observations = json.loads(
        (root / "blind-observations.json").read_text(encoding="utf-8")
    )["observations"]
    execution_records = [
        json.loads(
            (root / "executions" / f"slot-{ordinal:03d}.json").read_text(
                encoding="utf-8"
            )
        )
        for ordinal in range(1, 91)
    ]
    expected_blind_ordinals = [
        record["ordinal"]
        for record in sorted(execution_records, key=source_blind_order_key)
    ]
    assert [observation["ordinal"] for observation in persisted_observations] == (
        expected_blind_ordinals
    )
    assert expected_blind_ordinals != list(range(1, 91))
    assert all(
        observation["observation"]["child"]["path"].startswith("opaque-child/")
        for observation in persisted_observations
    )
    assert all(
        str(root) not in observation["observation"]["child"]["path"]
        for observation in persisted_observations
    )
    verify_evidence_seal(root / "intent")
    verify_evidence_seal(root)


def test_source_blind_call_counter_cannot_recover_the_frozen_slot_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _execute_mock_campaign(
        tmp_path,
        monkeypatch,
        outcomes=("PASS",) * 90,
        blind_attack="call_counter",
    )
    summary = json.loads(result["summary_path"].read_text(encoding="utf-8"))
    correct = sum(
        summary["rates"][arm]["classification_correctness"]["successes"]
        for arm in result["profile"].arm_names
    )

    assert summary["campaign_acceptance"] == "ACCEPTED"
    assert correct < 90


def test_source_blind_timestamp_path_attack_sees_only_random_neutral_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _execute_mock_campaign(
        tmp_path,
        monkeypatch,
        outcomes=("PASS",) * 90,
        blind_attack="timestamp_path",
    )
    summary = json.loads(result["summary_path"].read_text(encoding="utf-8"))

    assert all(path.name.startswith("opaque-") for path in result["blind_calls"])
    assert all(
        re.fullmatch(
            r"[0-9]{8}T[0-9]{6}Z-[1-9][0-9]*-[0-9a-f]{8}",
            path.name,
        )
        is None
        for path in result["blind_calls"]
    )
    for arm in result["profile"].arm_names:
        assert summary["confusion_table"][arm]["unclassified"] == 30
        assert summary["rates"][arm]["classification_correctness"]["successes"] == 0


@pytest.mark.parametrize(
    "raised_error",
    [RuntimeError("infrastructure failure"), KeyboardInterrupt()],
    ids=("infrastructure", "keyboard-interrupt"),
)
def test_raised_error_stops_without_retry_and_accounts_untouched_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raised_error: BaseException,
) -> None:
    result = _execute_mock_campaign(
        tmp_path,
        monkeypatch,
        raise_at=4,
        raised_error=raised_error,
    )
    root = result["campaign_root"]
    summary = json.loads((root / "campaign-summary.json").read_text(encoding="utf-8"))
    executions = [
        json.loads(
            (root / "executions" / f"slot-{ordinal:03d}.json").read_text(
                encoding="utf-8"
            )
        )
        for ordinal in range(1, 91)
    ]

    assert result["error"] is not None
    assert result["error"].campaign_directory == root
    assert len(result["run_calls"]) == 4
    assert len(result["blind_calls"]) == 3
    assert [record["launch_status"] for record in executions[:4]] == [
        "returned",
        "returned",
        "returned",
        "raised",
    ]
    assert {record["launch_status"] for record in executions[4:]} == {"not_started"}
    assert summary["campaign_acceptance"] == "REJECTED"
    assert summary["figure_eligible"] is False
    assert summary["outcome_counts"] == {
        "PASS": 3,
        "FAIL": 0,
        "INCOMPLETE": 87,
    }
    assert summary["invocation_status_counts"] == {
        "returned": 3,
        "raised": 1,
        "not_started": 86,
    }
    assert len(list((root / "starts").iterdir())) == 4
    assert len(list((root / "executions").iterdir())) == 90
    verify_evidence_seal(root)


def test_outer_validator_rebuilds_accepted_campaign_and_rejects_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _execute_mock_campaign(
        tmp_path,
        monkeypatch,
        outcomes=("PASS",) * 90,
    )
    root = result["campaign_root"]

    def validate_preserved(
        run_directory: Path,
        *,
        trusted_provenance: TrustedProvenance,
    ) -> dict[str, object]:
        assert trusted_provenance is result["trusted"]
        return {
            "verdict": result["child_outcomes"][run_directory.name],
            "arm": result["child_arms"][run_directory.name],
        }

    rebuilt = validate_n31_pqar_campaign(
        root,
        trusted_provenance=result["trusted"],
        classify_preserved_run_source_blind=result["classify"],
        validate_preserved_run=validate_preserved,
        validate_pilot_sequence=result["validate_pilot"],
    )
    assert rebuilt["campaign_acceptance"] == "ACCEPTED"
    assert rebuilt["outcome_status"] == "ALL_QUALIFIED"

    (root / "campaign-summary.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(N31PostQcAuditCampaignError, match="outer campaign seal"):
        validate_n31_pqar_campaign(
            root,
            trusted_provenance=result["trusted"],
            classify_preserved_run_source_blind=result["classify"],
            validate_preserved_run=validate_preserved,
            validate_pilot_sequence=result["validate_pilot"],
        )


def _validate_outer(
    result: dict[str, Any],
    *,
    wrong_arm_child: str | None = None,
) -> dict[str, object]:
    def validate_preserved(
        run_directory: Path,
        *,
        trusted_provenance: TrustedProvenance,
    ) -> dict[str, object]:
        assert trusted_provenance is result["trusted"]
        arm = result["child_arms"][run_directory.name]
        if run_directory.name == wrong_arm_child:
            arm = (
                "static_authenticated_sham"
                if arm != "static_authenticated_sham"
                else "static_authenticated_false_report"
            )
        return {
            "verdict": result["child_outcomes"][run_directory.name],
            "arm": arm,
        }

    return validate_n31_pqar_campaign(
        result["campaign_root"],
        trusted_provenance=result["trusted"],
        classify_preserved_run_source_blind=result["classify"],
        validate_preserved_run=validate_preserved,
        validate_pilot_sequence=result["validate_pilot"],
    )


def test_outer_validator_rejects_wrong_preserved_child_arm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _execute_mock_campaign(
        tmp_path,
        monkeypatch,
        outcomes=("PASS",) * 90,
    )
    wrong_arm_child = next(iter(result["child_arms"]))

    with pytest.raises(
        N31PostQcAuditCampaignError,
        match="changed a preserved child arm or verdict",
    ):
        _validate_outer(result, wrong_arm_child=wrong_arm_child)


def test_outer_validator_rejects_resealed_nonsequential_chronology(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _execute_mock_campaign(
        tmp_path,
        monkeypatch,
        outcomes=("PASS",) * 90,
    )
    root = result["campaign_root"]
    first_execution = json.loads(
        (root / "executions/slot-001.json").read_text(encoding="utf-8")
    )
    second_start_path = root / "starts/slot-002.json"
    second_execution_path = root / "executions/slot-002.json"
    second_start = json.loads(second_start_path.read_text(encoding="utf-8"))
    second_execution = json.loads(second_execution_path.read_text(encoding="utf-8"))
    tampered_start_ns = first_execution["finished_monotonic_ns"] - 1
    second_start["started_monotonic_ns"] = tampered_start_ns
    second_execution["started_monotonic_ns"] = tampered_start_ns
    second_execution["elapsed_ns"] = (
        second_execution["finished_monotonic_ns"] - tampered_start_ns
    )
    second_start_path.write_bytes(runner.canonical_json_bytes(second_start))
    second_execution_path.write_bytes(runner.canonical_json_bytes(second_execution))
    _reseal(root)

    with pytest.raises(
        N31PostQcAuditCampaignError,
        match="violates the frozen sequential chronology",
    ):
        _validate_outer(result)


def test_start_record_write_failure_stops_and_seals_rejected_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write_json = runner._write_json_exclusive
    injected = False

    def fail_once(path: Path, value: object) -> None:
        nonlocal injected
        if (
            path.name == "slot-004.json"
            and path.parent.name == "starts"
            and not injected
        ):
            injected = True
            raise OSError("injected start persistence failure")
        real_write_json(path, value)

    monkeypatch.setattr(runner, "_write_json_exclusive", fail_once)
    result = _execute_mock_campaign(
        tmp_path,
        monkeypatch,
        expected_run_calls=3,
        expected_blind_calls=3,
    )
    root = result["campaign_root"]
    summary = json.loads((root / "campaign-summary.json").read_text(encoding="utf-8"))

    assert injected is True
    assert result["error"] is not None
    assert len(result["run_calls"]) == 3
    assert len(result["blind_calls"]) == 3
    assert len(list((root / "starts").iterdir())) == 3
    assert len(list((root / "executions").iterdir())) == 90
    assert summary["campaign_acceptance"] == "REJECTED"
    assert summary["invocation_status_counts"] == {
        "returned": 3,
        "raised": 1,
        "not_started": 86,
    }
    verify_evidence_seal(root)


def test_partial_root_allocation_failure_is_preserved_and_sealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "campaign"
    real_mkdir = Path.mkdir
    injected = False

    def fail_once(
        path: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        nonlocal injected
        if path == root / "executions" and not injected:
            injected = True
            raise OSError("injected allocation failure")
        real_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", fail_once)
    with pytest.raises(
        runner.N31PostQcAuditCampaignInterrupted,
        match="allocation aborted",
    ) as caught:
        runner._allocate_campaign_root(root)

    assert injected is True
    assert caught.value.campaign_directory == root
    abort = json.loads((root / "controller-abort.json").read_text(encoding="utf-8"))
    assert abort["phase"] == "allocation"
    assert abort["figure_eligible"] is False
    verify_evidence_seal(root)


def test_intent_write_failure_is_preserved_before_any_slot_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write_json = runner._write_json_exclusive
    injected = False

    def fail_once(path: Path, value: object) -> None:
        nonlocal injected
        if path.name == "frozen-preflight.json" and not injected:
            injected = True
            raise KeyboardInterrupt("injected intent persistence interruption")
        real_write_json(path, value)

    monkeypatch.setattr(runner, "_write_json_exclusive", fail_once)
    result = _execute_mock_campaign(
        tmp_path,
        monkeypatch,
        expected_run_calls=0,
        expected_blind_calls=0,
    )
    root = result["campaign_root"]

    assert injected is True
    assert result["error"] is not None
    assert result["run_calls"] == []
    assert result["blind_calls"] == []
    assert result["summary_path"] is None
    abort = json.loads((root / "controller-abort.json").read_text(encoding="utf-8"))
    assert abort["phase"] == "intent"
    assert abort["figure_eligible"] is False
    verify_evidence_seal(root)


def test_returned_child_record_write_failure_still_blinds_and_seals_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write_json = runner._write_json_exclusive
    injected = False

    def fail_once(path: Path, value: object) -> None:
        nonlocal injected
        if (
            path.name == "slot-004.json"
            and path.parent.name == "executions"
            and not injected
        ):
            injected = True
            raise OSError("injected returned-record persistence failure")
        real_write_json(path, value)

    monkeypatch.setattr(runner, "_write_json_exclusive", fail_once)
    result = _execute_mock_campaign(
        tmp_path,
        monkeypatch,
        outcomes=("PASS",) * 90,
        expected_run_calls=4,
        expected_blind_calls=4,
    )
    root = result["campaign_root"]
    summary = json.loads((root / "campaign-summary.json").read_text(encoding="utf-8"))
    executions = [
        json.loads(
            (root / "executions" / f"slot-{ordinal:03d}.json").read_text(
                encoding="utf-8"
            )
        )
        for ordinal in range(1, 91)
    ]

    assert injected is True
    assert result["error"] is not None
    assert len(result["run_calls"]) == 4
    assert len(result["blind_calls"]) == 4
    assert [record["launch_status"] for record in executions[:4]] == [
        "returned",
        "returned",
        "returned",
        "returned",
    ]
    assert {record["launch_status"] for record in executions[4:]} == {"not_started"}
    assert summary["campaign_acceptance"] == "REJECTED"
    assert summary["returned_invocations"] == 4
    assert summary["controller_failures"][0].startswith(
        "returned_execution_record_write_failed_004:OSError:"
    )
    verify_evidence_seal(root)
    assert _validate_outer(result)["campaign_acceptance"] == "REJECTED"


def test_blind_observation_write_failure_is_recorded_and_sealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write_json = runner._write_json_exclusive
    injected = False

    def fail_once(path: Path, value: object) -> None:
        nonlocal injected
        if path.name == "blind-observations.json" and not injected:
            injected = True
            raise OSError("injected blind aggregate persistence failure")
        real_write_json(path, value)

    monkeypatch.setattr(runner, "_write_json_exclusive", fail_once)
    result = _execute_mock_campaign(tmp_path, monkeypatch, outcomes=("PASS",) * 90)
    root = result["campaign_root"]
    summary = json.loads((root / "campaign-summary.json").read_text(encoding="utf-8"))

    assert injected is True
    assert result["error"] is None
    assert summary["campaign_acceptance"] == "REJECTED"
    assert summary["controller_failures"][0].startswith(
        "blind_observation_write_failed:OSError:"
    )
    verify_evidence_seal(root)
    assert _validate_outer(result)["campaign_acceptance"] == "REJECTED"


def test_summary_write_failure_is_recorded_and_sealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = runner._write_exclusive
    injected = False

    def fail_once(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
        nonlocal injected
        if path.name == "campaign-summary.json" and not injected:
            injected = True
            raise OSError("injected campaign summary persistence failure")
        real_write(path, payload, mode=mode)

    monkeypatch.setattr(runner, "_write_exclusive", fail_once)
    result = _execute_mock_campaign(tmp_path, monkeypatch, outcomes=("PASS",) * 90)
    root = result["campaign_root"]
    summary = json.loads((root / "campaign-summary.json").read_text(encoding="utf-8"))

    assert injected is True
    assert result["error"] is None
    assert summary["campaign_acceptance"] == "REJECTED"
    assert summary["controller_failures"][0].startswith(
        "campaign_summary_write_failed:OSError:"
    )
    verify_evidence_seal(root)
    assert _validate_outer(result)["campaign_acceptance"] == "REJECTED"


def test_outer_seal_interrupt_is_recorded_recovered_and_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign_root = tmp_path / "campaign"
    real_create_seal = runner.create_evidence_seal
    injected = False

    def fail_once(path: Path):
        nonlocal injected
        if Path(path).resolve() == campaign_root.resolve() and not injected:
            injected = True
            raise KeyboardInterrupt("injected outer seal interruption")
        return real_create_seal(path)

    monkeypatch.setattr(runner, "create_evidence_seal", fail_once)
    result = _execute_mock_campaign(tmp_path, monkeypatch, outcomes=("PASS",) * 90)
    summary = json.loads(
        (campaign_root / "campaign-summary.json").read_text(encoding="utf-8")
    )

    assert injected is True
    assert result["error"] is None
    assert summary["campaign_acceptance"] == "REJECTED"
    assert summary["controller_failures"][0].startswith(
        "campaign_seal_failed:KeyboardInterrupt:"
    )
    verify_evidence_seal(campaign_root)
    assert _validate_outer(result)["campaign_acceptance"] == "REJECTED"


def _reseal(path: Path) -> None:
    (path / "evidence-seal.json").unlink()
    create_evidence_seal(path)


@pytest.mark.parametrize(
    ("tamper", "message"),
    (
        ("intent_seal", "campaign intent seal rejected"),
        ("child_seal", "source-blind child evidence seal rejected"),
        ("extra", "campaign root membership differs"),
        ("symlink", "outer campaign seal rejected"),
        ("profile", "sealed intent profile/provenance rejected"),
        ("revision", "intent revision, provenance, or preflight digest drifted"),
        ("provenance", "intent revision, provenance, or preflight digest drifted"),
        ("returned_path", "differs from direct child accounting"),
    ),
)
def test_outer_validator_rejects_seal_membership_and_binding_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
    message: str,
) -> None:
    result = _execute_mock_campaign(
        tmp_path,
        monkeypatch,
        outcomes=("PASS",) * 90,
    )
    root = result["campaign_root"]
    intent = root / "intent"

    if tamper == "intent_seal":
        plan_path = intent / "campaign-plan.json"
        plan_path.write_bytes(plan_path.read_bytes() + b" ")
        _reseal(root)
    elif tamper == "child_seal":
        child_name = next(iter(result["child_outcomes"]))
        child_raw = root / "children" / child_name / "raw.txt"
        child_raw.write_bytes(child_raw.read_bytes() + b" ")
        _reseal(root)
    elif tamper == "extra":
        (root / "extra.json").write_text("{}\n", encoding="utf-8")
        _reseal(root)
    elif tamper == "symlink":
        (root / "escape").symlink_to(tmp_path)
    elif tamper == "profile":
        profile_path = intent / "audit-profile.json"
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        profile["profile_id"] = "tampered"
        profile_path.write_bytes(runner.canonical_json_bytes(profile))
        _reseal(intent)
        _reseal(root)
    elif tamper in {"revision", "provenance"}:
        plan_path = intent / "campaign-plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan[
            "kauri_revision" if tamper == "revision" else "trusted_provenance_sha256"
        ] = "f" * (40 if tamper == "revision" else 64)
        plan_path.write_bytes(runner.canonical_json_bytes(plan))
        _reseal(intent)
        _reseal(root)
    else:
        record_path = root / "executions/slot-001.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["run_directory"] = "children/not-the-returned-child"
        record_path.write_bytes(runner.canonical_json_bytes(record))
        _reseal(root)

    def validate_preserved(
        run_directory: Path,
        *,
        trusted_provenance: TrustedProvenance,
    ) -> dict[str, object]:
        assert trusted_provenance is result["trusted"]
        return {
            "verdict": "PASS",
            "arm": result["child_arms"][run_directory.name],
        }

    with pytest.raises((N31PostQcAuditCampaignError, ValueError), match=message):
        validate_n31_pqar_campaign(
            root,
            trusted_provenance=result["trusted"],
            classify_preserved_run_source_blind=result["classify"],
            validate_preserved_run=validate_preserved,
            validate_pilot_sequence=result["validate_pilot"],
        )


def test_one_shot_allocator_rejects_preexisting_empty_root(tmp_path: Path) -> None:
    root = tmp_path / "already-spent"
    root.mkdir()

    with pytest.raises(
        runner.N31PostQcAuditCampaignRunError,
        match="already spent",
    ):
        runner._allocate_campaign_root(root)


def test_preexisting_campaign_root_rejects_before_pilot_build_or_preflight(
    tmp_path: Path,
) -> None:
    trusted = _trusted(tmp_path)
    root = tmp_path / "already-spent"
    root.mkdir()
    calls: list[str] = []

    def forbidden(name: str):
        def callback(**_kwargs: Any) -> Any:
            calls.append(name)
            raise AssertionError(f"{name} must not be called")

        return callback

    with pytest.raises(
        runner.N31PostQcAuditCampaignRunError,
        match="already spent",
    ):
        runner.run_campaign(
            **_campaign_arguments(tmp_path, trusted=trusted, campaign_root=root),
            prepare_build=forbidden("build"),
            derive_provenance=forbidden("provenance"),
            preflight=forbidden("preflight"),
            validate_pilot=forbidden("pilot"),
            run_once=forbidden("run_once"),
            classify_source_blind=lambda path, *, trusted_provenance: {},
            validate_after_seal=False,
        )
    assert calls == []


def test_cli_rejects_noncanonical_run_root_before_campaign_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = _trusted(tmp_path)
    receipt = tmp_path / "trusted.json"
    from experiments.adaptive.kauri_experiment.n31_static_diagnosis_runtime import (
        write_trusted_provenance,
    )

    write_trusted_provenance(receipt, trusted)
    called = False

    def forbidden_run_campaign(**_kwargs: Any) -> Path:
        nonlocal called
        called = True
        raise AssertionError("campaign actions must not start")

    monkeypatch.setattr(runner, "run_campaign", forbidden_run_campaign)
    result = runner.main(
        [
            "run",
            "--trusted-provenance",
            str(receipt),
            "--pilot-sequence",
            str(tmp_path / "pilot"),
            "--campaign-root",
            str(tmp_path / "arbitrary-second-campaign"),
        ]
    )

    assert result == 2
    assert called is False
