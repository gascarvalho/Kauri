"""Red-first CLI contracts for the focused N7/N31 crash-pair runner."""

from __future__ import annotations

import importlib
import hashlib
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from experiments.adaptive.tests import (
    test_focused_crash_pair_runtime as runtime_fixture,
)
from experiments.adaptive.tests import test_n31_crash_pair_contract as native_fixture
from experiments.adaptive.tests import (
    test_run_n31_crash_pair_campaign as campaign_fixture,
)

RUNNER = "experiments.adaptive.run_focused_n31_crash_pair"
PROFILE_ROOT = Path(__file__).parents[1] / "profiles"
N7_PROFILE_V6 = PROFILE_ROOT / "n7-f2-q5-two-crash-pair-smoke-v6.json"
N31_PROFILE_V6 = PROFILE_ROOT / "n31-f5-q21-three-crash-pair-v6.json"
N7_PROFILE_V7 = PROFILE_ROOT / "n7-f2-q5-two-crash-pair-smoke-v7.json"
N31_PROFILE_V7 = PROFILE_ROOT / "n31-f5-q21-three-crash-pair-v7.json"


def _runner() -> Any:
    return importlib.import_module(RUNNER)


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _inputs(
    tmp_path: Path,
    *,
    mode: str,
    pairs: int,
    output: Path,
) -> dict[str, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    values = {
        "profile": tmp_path / "profile.json",
        "preflight": tmp_path / "preflight.json",
        "authorization": tmp_path / "authorization.json",
        "trusted": tmp_path / "trusted.json",
    }
    source_profile = N7_PROFILE_V7 if mode == "smoke" else N31_PROFILE_V7
    profile = json.loads(source_profile.read_text(encoding="utf-8"))
    source_proof = runtime_fixture._topology_proof_path(source_profile, profile)
    values["proof"] = tmp_path / profile["topology"]["proof_path"]
    values["proof"].parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_proof, values["proof"])
    profile["topology"]["proof_sha256"] = hashlib.sha256(
        values["proof"].read_bytes()
    ).hexdigest()
    values["profile"].write_bytes(_canonical(profile))
    profile_sha256 = runtime_fixture._canonical_profile_sha256(profile)
    request = {
        "schema_version": 1,
        "mode": mode,
        "pair_count": pairs,
        "profile_sha256": profile_sha256,
        "topology_proof_sha256": profile["topology"]["proof_sha256"],
        "output_root": str(output.resolve()),
        "automatic_retries": 0,
        "replacement_policy": "none",
        "authorization_nonce": hashlib.sha256(
            f"{mode}:{pairs}:{output.resolve()}".encode()
        ).hexdigest(),
    }
    request_sha256 = hashlib.sha256(_canonical(request)).hexdigest()
    values["preflight"].write_bytes(
        _canonical(
            {
                **request,
                "request_sha256": request_sha256,
                "execution_authorized": False,
                "launch_permitted": False,
            }
        )
    )
    values["authorization"].write_bytes(
        _canonical(
            {
                **request,
                "request_sha256": request_sha256,
                "approval_reference": "thesis-author-approved-focused-run",
                "approved_utc": "2026-08-12T12:00:00+00:00",
            }
        )
    )
    values["trusted"].write_bytes(
        _canonical({"schema_version": 1, "profile_sha256": profile_sha256})
    )
    return values


def _execution_sentinels(runner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runner,
        "run_focused_pair",
        lambda **_kwargs: pytest.fail("invalid CLI input reached pair execution"),
    )
    monkeypatch.setattr(
        runner,
        "run_focused_campaign",
        lambda **_kwargs: pytest.fail("invalid CLI input reached campaign execution"),
    )


def _stub_aggregate_validators(
    runner: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    list[tuple[Path, Mapping[str, object]]], list[tuple[Path, Mapping[str, object]]]
]:
    pair_calls: list[tuple[Path, Mapping[str, object]]] = []
    campaign_calls: list[tuple[Path, Mapping[str, object]]] = []

    def validate_pair(
        directory: Path, *, trusted_provenance: Mapping[str, object]
    ) -> Mapping[str, object]:
        pair_calls.append((Path(directory), trusted_provenance))
        return {"pair_id": Path(directory).name, "verdict": "PASS"}

    def validate_campaign(
        directory: Path, *, trusted_provenance: Mapping[str, object]
    ) -> Mapping[str, object]:
        campaign_calls.append((Path(directory), trusted_provenance))
        return {"verdict": "PASS"}

    monkeypatch.setattr(runner, "validate_sealed_pair", validate_pair)
    monkeypatch.setattr(runner, "validate_sealed_campaign", validate_campaign)
    return pair_calls, campaign_calls


def _direct_focused_invocation(
    output: Path,
    *,
    mode: str,
) -> dict[str, object]:
    pair_count = 5 if mode == "campaign" else 1
    return {
        "mode": mode,
        "pair_count": pair_count,
        "output_root": output,
        "profile": SimpleNamespace(profile_sha256="a" * 64),
        "preflight_receipt": {
            "execution_context": {
                "issuer_public_key": native_fixture.ISSUER_PUBLIC_KEY,
                "profile_sha256": "a" * 64,
                "topology_proof_sha256": "b" * 64,
            }
        },
    }


class _ExecutionReached(Exception):
    pass


class _RecordingLaunchBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str | None]] = []
        self.issuers_by_pair: dict[str, set[str]] = {}

    def bind_execution_context(
        self,
        invocation: Mapping[str, object],
    ) -> Mapping[str, object]:
        preflight = invocation["preflight_receipt"]
        assert isinstance(preflight, Mapping)
        context = preflight["execution_context"]
        assert isinstance(context, Mapping)
        assert context["issuer_public_key"] == native_fixture.ISSUER_PUBLIC_KEY
        assert context["profile_sha256"] == invocation["profile"].profile_sha256
        self.calls.append(("identity", None, None))
        return {**invocation, "execution_context": context}

    def materialize_arm_configuration(
        self,
        context: Mapping[str, object],
        *,
        pair_ordinal: int,
        arm: str,
    ) -> Mapping[str, object]:
        pair_id = f"pair-{pair_ordinal:02d}"
        execution = context["execution_context"]
        assert isinstance(execution, Mapping)
        self.issuers_by_pair.setdefault(pair_id, set()).add(
            str(execution["issuer_public_key"])
        )
        self.calls.append(("configuration", pair_id, arm))
        return {"context": context, "pair_id": pair_id, "arm": arm}

    def spawn_processes(self, configuration: Mapping[str, object]) -> object:
        self._record("spawn", configuration)
        return object()

    def execute_atomic_fault_batch(
        self,
        configuration: Mapping[str, object],
        _processes: object,
    ) -> Mapping[str, object]:
        self._record("fault", configuration)
        return {"atomic": True}

    def drive_event_hooks(
        self,
        configuration: Mapping[str, object],
        _processes: object,
        _fault_receipt: Mapping[str, object],
    ) -> Mapping[str, object]:
        self._record("events", configuration)
        return {"runtime_graph": "complete"}

    def cleanup(
        self,
        configuration: Mapping[str, object],
        _processes: object,
    ) -> Mapping[str, object]:
        self._record("cleanup", configuration)
        return {"complete": True}

    def seal(
        self,
        configuration: Mapping[str, object],
        _outcome: Mapping[str, object],
        _cleanup: Mapping[str, object],
    ) -> Mapping[str, object]:
        self._record("seal", configuration)
        return {"seal_sha256": "d" * 64}

    def validate(
        self,
        configuration: Mapping[str, object],
        _seal: Mapping[str, object],
    ) -> Mapping[str, object]:
        self._record("validate", configuration)
        return {
            "verdict": "PROVISIONAL",
            "trusted_provenance_supplied": False,
        }

    def append_ledger(
        self,
        _context: Mapping[str, object],
        configuration: Mapping[str, object],
        _validation: Mapping[str, object],
    ) -> Mapping[str, object]:
        self._record("ledger", configuration)
        return {"terminal": True}

    def _record(self, name: str, configuration: Mapping[str, object]) -> None:
        self.calls.append(
            (name, str(configuration["pair_id"]), str(configuration["arm"]))
        )


@pytest.mark.parametrize("command", ("smoke", "pair", "campaign"))
def test_default_cli_executes_the_injectable_focused_launch_backend(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    pair_count = 5 if command == "campaign" else 1
    output = tmp_path / "results"
    paths = _inputs(tmp_path, mode=command, pairs=pair_count, output=output)
    preflight = json.loads(paths["preflight"].read_text(encoding="utf-8"))
    preflight["execution_context"] = {
        "issuer_public_key": native_fixture.ISSUER_PUBLIC_KEY,
        "profile_sha256": preflight["profile_sha256"],
        "topology_proof_sha256": preflight["topology_proof_sha256"],
    }
    paths["preflight"].write_bytes(_canonical(preflight))
    backend = _RecordingLaunchBackend()
    monkeypatch.setattr(runner, "FOCUSED_LAUNCH_BACKEND", backend, raising=False)
    pair_calls, campaign_calls = _stub_aggregate_validators(runner, monkeypatch)
    captured: list[Mapping[str, object]] = []
    monkeypatch.setattr(runner, "_write_cli_result", captured.append)
    assert (
        runner.main(
            [
                command,
                "--profile",
                str(paths["profile"]),
                "--pairs",
                str(pair_count),
                "--preflight-receipt",
                str(paths["preflight"]),
                "--authorization-receipt",
                str(paths["authorization"]),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    expected = [("identity", None, None)]
    slots = (
        campaign_fixture._expected_slots()
        if command == "campaign"
        else (
            {"pair_id": "pair-01", "arm": "control"},
            {"pair_id": "pair-01", "arm": "adaptive"},
        )
    )
    for slot in slots:
        expected.extend(
            (name, str(slot["pair_id"]), str(slot["arm"]))
            for name in (
                "configuration",
                "spawn",
                "fault",
                "events",
                "cleanup",
                "seal",
                "validate",
                "ledger",
            )
        )
    assert backend.calls == expected
    assert backend.issuers_by_pair == {
        f"pair-{pair_ordinal:02d}": {native_fixture.ISSUER_PUBLIC_KEY}
        for pair_ordinal in range(1, pair_count + 1)
    }
    assert len(captured) == 1
    assert captured[0]["validation_status"] == "PROVISIONAL"
    assert captured[0]["trusted_provenance_required"] is True
    assert pair_calls == []
    assert campaign_calls == []
    assert all(
        validation["trusted_provenance_supplied"] is False
        and validation["pending_external_provenance"]["children"]
        for validation in captured[0]["pair_validations"]
    )


@pytest.mark.parametrize(("mode", "pair_count"), (("pair", 1), ("campaign", 5)))
def test_default_execution_writes_one_canonical_sealed_parent_evidence_tree(
    mode: str,
    pair_count: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    output = tmp_path / "results"
    backend = _RecordingLaunchBackend()
    pair_calls, campaign_calls = _stub_aggregate_validators(runner, monkeypatch)
    result = runner._execute_focused(
        {
            "mode": mode,
            "pair_count": pair_count,
            "output_root": output,
            "profile": SimpleNamespace(profile_sha256="a" * 64),
            "preflight_receipt": {
                "execution_context": {
                    "issuer_public_key": native_fixture.ISSUER_PUBLIC_KEY,
                    "profile_sha256": "a" * 64,
                    "topology_proof_sha256": "b" * 64,
                }
            },
        },
        backend=backend,
    )
    for ordinal in range(1, pair_count + 1):
        pair_root = output / f"pair-{ordinal:02d}"
        assert (pair_root / "pair-receipt.json").is_file()
        assert (pair_root / "evidence-seal.json").is_file()
    if mode == "campaign":
        assert (output / "plan.json").is_file()
        assert (output / "campaign-ledger.jsonl").is_file()
        assert (output / "campaign-summary.json").is_file()
        assert (output / "evidence-seal.json").is_file()
        assert not any(path.is_dir() for path in output.glob("*ledger*"))
        assert result["campaign_validation"]["verdict"] == "PROVISIONAL"
    else:
        assert result["pair_validations"][0]["verdict"] == "PROVISIONAL"
    assert result["validation_status"] == "PROVISIONAL"
    assert pair_calls == []
    assert campaign_calls == []


def test_execution_never_invokes_or_self_promotes_aggregate_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    output = tmp_path / "external-provenance-required"
    invocation = _direct_focused_invocation(output, mode="campaign")
    backend = _RecordingLaunchBackend()

    monkeypatch.setattr(
        runner,
        "validate_sealed_pair",
        lambda *_args, **_kwargs: pytest.fail(
            "execution supplied aggregate provenance"
        ),
    )
    monkeypatch.setattr(
        runner,
        "validate_sealed_campaign",
        lambda *_args, **_kwargs: pytest.fail(
            "execution supplied aggregate provenance"
        ),
    )

    result = runner._execute_focused(invocation, backend=backend)

    assert result["validation_status"] == "PROVISIONAL"
    for validation in [*result["pair_validations"], result["campaign_validation"]]:
        assert validation["verdict"] == "PROVISIONAL"
        assert validation["trusted_provenance_required"] is True
        assert validation["trusted_provenance_supplied"] is False
        assert validation["pending_external_provenance"]["evidence_tree_sha256"]


def test_campaign_run_arm_failure_seals_only_an_incomplete_abort_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runtime failure retains its exact prefix but can never become a campaign."""

    runner = _runner()
    output = tmp_path / "aborted-campaign"
    runtime = importlib.import_module(
        "experiments.adaptive.kauri_experiment.focused_crash_pair_runtime"
    )
    validation_module = importlib.import_module(
        "experiments.adaptive.kauri_experiment.focused_crash_pair_validation"
    )
    actual_validate_campaign = runner.validate_sealed_campaign
    pair_calls, campaign_calls = _stub_aggregate_validators(runner, monkeypatch)
    seal_order: list[Path] = []
    original_seal = runner.create_evidence_seal

    def seal(directory: Path) -> object:
        directory = Path(directory)
        seal_order.append(directory)
        if directory.name == "slot-02":
            assert (directory / "cleanup.json").is_file()
            assert (directory / "slot-abort.json").is_file()
        elif directory == output:
            assert (output / "campaign-abort-prefix.json").is_file()
            assert (output / "children" / "slot-02" / "evidence-seal.json").is_file()
        return original_seal(directory)

    monkeypatch.setattr(runner, "create_evidence_seal", seal)

    class AbortingBackend(_RecordingLaunchBackend):
        def __init__(self) -> None:
            super().__init__()
            self.failure = runtime.FocusedCrashPairRuntimeError("native arm failed")

        def materialize_arm_configuration(
            self,
            context: Mapping[str, object],
            *,
            pair_ordinal: int,
            arm: str,
        ) -> Mapping[str, object]:
            configuration = dict(
                super().materialize_arm_configuration(
                    context, pair_ordinal=pair_ordinal, arm=arm
                )
            )
            root = Path(context["slot_directory"])
            root.mkdir(parents=True, exist_ok=False)
            configuration["run_directory"] = root
            configuration["slot_id"] = str(context["slot_id"])
            return configuration

        def run_arm(
            self, configuration: Mapping[str, object], _processes: object
        ) -> Mapping[str, object]:
            self._record("run", configuration)
            if configuration["slot_id"] == "slot-02":
                raise self.failure
            return {"runtime_graph": "complete"}

        def cleanup(
            self, configuration: Mapping[str, object], processes: object
        ) -> Mapping[str, object]:
            self._record("cleanup", configuration)
            return {
                "complete": True,
                "outcomes": [
                    {
                        "name": "adaptive-manager",
                        "replica_id": -1,
                        "pid": 101,
                        "pgid": 101,
                        "signal_number": 15,
                        "returncode": -15,
                    }
                ],
            }

        def seal(
            self,
            configuration: Mapping[str, object],
            outcome: Mapping[str, object],
            cleanup: Mapping[str, object],
        ) -> Mapping[str, object]:
            self._record("seal", configuration)
            metadata = original_seal(Path(configuration["run_directory"]))
            return {
                "tree_sha256": metadata.tree_sha256,
                "seal_sha256": metadata.seal_sha256,
            }

    backend = AbortingBackend()
    with pytest.raises(runtime.FocusedCrashPairRuntimeError) as raised:
        runner._execute_focused(
            _direct_focused_invocation(output, mode="campaign"), backend=backend
        )
    assert raised.value is backend.failure
    failed_root = output / "children" / "slot-02"
    cleanup = json.loads((failed_root / "cleanup.json").read_text())
    abort = json.loads((failed_root / "slot-abort.json").read_text())
    prefix = json.loads((output / "campaign-abort-prefix.json").read_text())
    assert cleanup["outcomes"][0]["returncode"] == -15
    assert abort == {
        "schema_version": 1,
        "kind": "kauri-focused-slot-abort-v1",
        "state": "INCOMPLETE",
        "claim_eligible": False,
        "plan_sha256": prefix["plan_sha256"],
        "slot_id": "slot-02",
        "pair_id": "pair-01",
        "arm": "adaptive",
        "pair_seed": 41_720,
        "execution_ordinal": 2,
        "attempt_ordinal": 1,
        "automatic_retries": 0,
        "replacement_policy": "none",
        "failure": {"category": "runtime_error", "reason": "native arm failed"},
    }
    assert [row["slot_id"] for row in prefix["completed_prefix"]] == ["slot-01"]
    assert prefix["failed_slot"]["slot_id"] == "slot-02"
    completed_seal = runner.verify_evidence_seal(output / "children" / "slot-01")
    assert (
        prefix["completed_prefix"][0]["child_tree_sha256"] == completed_seal.tree_sha256
    )
    assert (
        prefix["completed_prefix"][0]["child_seal_sha256"] == completed_seal.seal_sha256
    )
    assert [row["slot_id"] for row in prefix["not_started"]] == [
        f"slot-{ordinal:02d}" for ordinal in range(3, 11)
    ]
    assert seal_order == [failed_root, output]
    assert (failed_root / "evidence-seal.json").is_file()
    assert (output / "evidence-seal.json").is_file()
    assert not (output / "campaign-ledger.jsonl").exists()
    assert not (output / "campaign-summary.json").exists()
    assert not tuple(output.glob("pair-*"))
    assert pair_calls == []
    assert campaign_calls == []
    assert [call for call in backend.calls if call[0] == "configuration"] == [
        ("configuration", "pair-01", "control"),
        ("configuration", "pair-01", "adaptive"),
    ]
    assert not [
        call
        for call in backend.calls
        if call[2] == "adaptive" and call[0] in {"seal", "validate", "ledger"}
    ]
    with pytest.raises(
        runner.FocusedCrashPairValidationError, match="ledger is absent"
    ):
        actual_validate_campaign(output, trusted_provenance={})
    with pytest.raises(validation_module.FocusedCrashPairValidationError):
        validation_module.validate_sealed_arm(failed_root, trusted_provenance={})


@pytest.mark.parametrize(
    ("completed_seal_mode", "expected_reason", "completed_seal_exists"),
    (
        ("missing", "evidence seal is missing", False),
        ("mismatched", "differs from its persisted evidence", True),
    ),
)
def test_campaign_abort_rejects_unverified_completed_prefix_seals(
    completed_seal_mode: str,
    expected_reason: str,
    completed_seal_exists: bool,
    tmp_path: Path,
) -> None:
    runner = _runner()
    output = tmp_path / completed_seal_mode
    runtime = importlib.import_module(
        "experiments.adaptive.kauri_experiment.focused_crash_pair_runtime"
    )

    class InvalidCompletedSealBackend(_RecordingLaunchBackend):
        def materialize_arm_configuration(
            self,
            context: Mapping[str, object],
            *,
            pair_ordinal: int,
            arm: str,
        ) -> Mapping[str, object]:
            configuration = dict(
                super().materialize_arm_configuration(
                    context, pair_ordinal=pair_ordinal, arm=arm
                )
            )
            root = Path(context["slot_directory"])
            root.mkdir(parents=True, exist_ok=False)
            configuration["run_directory"] = root
            configuration["slot_id"] = str(context["slot_id"])
            return configuration

        def run_arm(
            self, configuration: Mapping[str, object], _processes: object
        ) -> Mapping[str, object]:
            self._record("run", configuration)
            if configuration["slot_id"] == "slot-02":
                raise runtime.FocusedCrashPairRuntimeError("native arm failed")
            return {"runtime_graph": "complete"}

        def cleanup(
            self, configuration: Mapping[str, object], processes: object
        ) -> Mapping[str, object]:
            self._record("cleanup", configuration)
            return {"complete": True, "outcomes": []}

        def seal(
            self,
            configuration: Mapping[str, object],
            outcome: Mapping[str, object],
            cleanup: Mapping[str, object],
        ) -> Mapping[str, object]:
            self._record("seal", configuration)
            if completed_seal_mode == "mismatched":
                runner.create_evidence_seal(Path(configuration["run_directory"]))
            return {"tree_sha256": "a" * 64, "seal_sha256": "b" * 64}

    with pytest.raises(runtime.FocusedCrashPairRuntimeError, match="native arm failed"):
        runner._execute_focused(
            _direct_focused_invocation(output, mode="campaign"),
            backend=InvalidCompletedSealBackend(),
        )

    completed_root = output / "children" / "slot-01"
    failed_root = output / "children" / "slot-02"
    finalization_failure = json.loads(
        (failed_root / "abort-finalization-failure.json").read_text(encoding="utf-8")
    )
    assert expected_reason in finalization_failure["reason"]
    assert (completed_root / "evidence-seal.json").exists() is completed_seal_exists
    assert not (failed_root / "evidence-seal.json").exists()
    assert not (output / "campaign-abort-prefix.json").exists()
    assert not (output / "evidence-seal.json").exists()


@pytest.mark.parametrize("cleanup_mode", ("raises", "incomplete"))
def test_campaign_run_arm_failure_records_unsealed_cleanup_failure(
    cleanup_mode: str,
    tmp_path: Path,
) -> None:
    runner = _runner()
    output = tmp_path / cleanup_mode
    runtime = importlib.import_module(
        "experiments.adaptive.kauri_experiment.focused_crash_pair_runtime"
    )

    class CleanupFailureBackend(_RecordingLaunchBackend):
        def materialize_arm_configuration(
            self,
            context: Mapping[str, object],
            *,
            pair_ordinal: int,
            arm: str,
        ) -> Mapping[str, object]:
            configuration = dict(
                super().materialize_arm_configuration(
                    context, pair_ordinal=pair_ordinal, arm=arm
                )
            )
            root = Path(context["slot_directory"])
            root.mkdir(parents=True, exist_ok=False)
            configuration["run_directory"] = root
            configuration["slot_id"] = str(context["slot_id"])
            return configuration

        def run_arm(
            self, configuration: Mapping[str, object], _processes: object
        ) -> Mapping[str, object]:
            self._record("run", configuration)
            raise runtime.FocusedCrashPairRuntimeError("native arm failed")

        def cleanup(
            self, configuration: Mapping[str, object], processes: object
        ) -> Mapping[str, object]:
            self._record("cleanup", configuration)
            if cleanup_mode == "raises":
                raise RuntimeError("cleanup failed")
            return {"complete": False, "outcomes": []}

    with pytest.raises(runtime.FocusedCrashPairRuntimeError, match="native arm failed"):
        runner._execute_focused(
            _direct_focused_invocation(output, mode="campaign"),
            backend=CleanupFailureBackend(),
        )
    slot = output / "children" / "slot-01"
    failure = json.loads((slot / "cleanup-failure.json").read_text())
    assert failure["state"] == "INCOMPLETE"
    assert failure["claim_eligible"] is False
    assert not (slot / "evidence-seal.json").exists()
    assert not (output / "campaign-abort-prefix.json").exists()
    assert not (output / "evidence-seal.json").exists()


def test_campaign_run_arm_failure_keeps_original_error_when_abort_finalization_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    output = tmp_path / "finalizer-failure"
    runtime = importlib.import_module(
        "experiments.adaptive.kauri_experiment.focused_crash_pair_runtime"
    )

    class FinalizerFailureBackend(_RecordingLaunchBackend):
        def __init__(self) -> None:
            super().__init__()
            self.failure = runtime.FocusedCrashPairRuntimeError("native arm failed")

        def materialize_arm_configuration(
            self,
            context: Mapping[str, object],
            *,
            pair_ordinal: int,
            arm: str,
        ) -> Mapping[str, object]:
            configuration = dict(
                super().materialize_arm_configuration(
                    context, pair_ordinal=pair_ordinal, arm=arm
                )
            )
            root = Path(context["slot_directory"])
            root.mkdir(parents=True, exist_ok=False)
            configuration["run_directory"] = root
            configuration["slot_id"] = str(context["slot_id"])
            return configuration

        def run_arm(
            self, configuration: Mapping[str, object], _processes: object
        ) -> Mapping[str, object]:
            raise self.failure

        def cleanup(
            self, configuration: Mapping[str, object], processes: object
        ) -> Mapping[str, object]:
            return {"complete": True, "outcomes": []}

    monkeypatch.setattr(
        runner,
        "create_evidence_seal",
        lambda _directory: (_ for _ in ()).throw(RuntimeError("seal failed")),
    )
    monkeypatch.setattr(
        runner.sys,
        "stderr",
        SimpleNamespace(
            write=lambda _message: (_ for _ in ()).throw(OSError("stderr closed"))
        ),
    )
    backend = FinalizerFailureBackend()
    with pytest.raises(runtime.FocusedCrashPairRuntimeError) as raised:
        runner._execute_focused(
            _direct_focused_invocation(output, mode="campaign"),
            backend=backend,
        )
    assert raised.value is backend.failure
    slot = output / "children" / "slot-01"
    assert (slot / "cleanup.json").is_file()
    assert (slot / "slot-abort.json").is_file()
    failure = json.loads(
        (slot / "abort-finalization-failure.json").read_text(encoding="utf-8")
    )
    assert failure["state"] == "INCOMPLETE"
    assert failure["category"] == "finalization_error"
    assert not (slot / "evidence-seal.json").exists()
    assert not (output / "evidence-seal.json").exists()


def test_campaign_runtime_abort_keeps_cli_exit_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    runtime = importlib.import_module(
        "experiments.adaptive.kauri_experiment.focused_crash_pair_runtime"
    )
    monkeypatch.setattr(
        runner,
        "_authorized_execution",
        lambda _arguments: (SimpleNamespace(), {}, {}),
    )
    monkeypatch.setattr(
        runner,
        "run_focused_campaign",
        lambda **_kwargs: (_ for _ in ()).throw(
            runtime.FocusedCrashPairRuntimeError("native arm failed")
        ),
    )

    with pytest.raises(SystemExit) as raised:
        runner.main(
            [
                "campaign",
                "--pairs",
                "5",
                "--preflight-receipt",
                str(tmp_path / "preflight.json"),
                "--authorization-receipt",
                str(tmp_path / "authorization.json"),
                "--output",
                str(tmp_path / "output"),
                "--retries",
                "0",
            ]
        )

    assert raised.value.code == 2


@pytest.mark.parametrize("command", ("smoke", "pair"))
def test_smoke_pair_runtime_abort_keeps_cli_exit_two(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner()
    runtime = importlib.import_module(
        "experiments.adaptive.kauri_experiment.focused_crash_pair_runtime"
    )
    monkeypatch.setattr(
        runner,
        "_authorized_execution",
        lambda _arguments: (SimpleNamespace(), {}, {}),
    )
    monkeypatch.setattr(
        runner,
        "run_focused_pair",
        lambda **_kwargs: (_ for _ in ()).throw(
            runtime.FocusedCrashPairRuntimeError("native arm failed")
        ),
    )
    with pytest.raises(SystemExit) as raised:
        runner.main(
            [
                command,
                "--preflight-receipt",
                str(tmp_path / "preflight.json"),
                "--authorization-receipt",
                str(tmp_path / "authorization.json"),
                "--output",
                str(tmp_path / "output"),
                "--retries",
                "0",
            ]
        )
    assert raised.value.code == 2


class _PairAbortBackend(_RecordingLaunchBackend):
    def __init__(self, runtime: Any, *, failed_arm: str, cleanup_mode: str = "complete") -> None:
        super().__init__()
        self.failure = runtime.FocusedCrashPairRuntimeError("native arm failed")
        self.failed_arm = failed_arm
        self.cleanup_mode = cleanup_mode

    def materialize_arm_configuration(
        self, context: Mapping[str, object], *, pair_ordinal: int, arm: str
    ) -> Mapping[str, object]:
        configuration = dict(
            super().materialize_arm_configuration(
                context, pair_ordinal=pair_ordinal, arm=arm
            )
        )
        root = Path(context["output_root"]) / str(configuration["pair_id"]) / arm
        root.mkdir(parents=True, exist_ok=False)
        configuration["run_directory"] = root
        return configuration

    def run_arm(
        self, configuration: Mapping[str, object], _processes: object
    ) -> Mapping[str, object]:
        self._record("run", configuration)
        if configuration["arm"] == self.failed_arm:
            raise self.failure
        return {"runtime_graph": "complete"}

    def cleanup(
        self, configuration: Mapping[str, object], _processes: object
    ) -> Mapping[str, object]:
        self._record("cleanup", configuration)
        if self.cleanup_mode == "raises":
            raise RuntimeError("cleanup failed")
        return {"complete": self.cleanup_mode == "complete", "outcomes": []}

    def seal(
        self,
        configuration: Mapping[str, object],
        _outcome: Mapping[str, object],
        _cleanup: Mapping[str, object],
    ) -> Mapping[str, object]:
        self._record("seal", configuration)
        metadata = _runner().create_evidence_seal(Path(configuration["run_directory"]))
        return {"tree_sha256": metadata.tree_sha256, "seal_sha256": metadata.seal_sha256}


@pytest.mark.parametrize(
    ("mode", "failed_arm", "completed_arms", "not_started"),
    (
        ("smoke", "control", [], ["adaptive"]),
        ("pair", "control", [], ["adaptive"]),
        ("pair", "adaptive", ["control"], []),
    ),
)
def test_smoke_pair_runtime_abort_seals_only_quiescent_pair_prefix(
    mode: str,
    failed_arm: str,
    completed_arms: list[str],
    not_started: list[str],
    tmp_path: Path,
) -> None:
    runner = _runner()
    runtime = importlib.import_module(
        "experiments.adaptive.kauri_experiment.focused_crash_pair_runtime"
    )
    output = tmp_path / f"{mode}-{failed_arm}"
    backend = _PairAbortBackend(runtime, failed_arm=failed_arm)

    with pytest.raises(runtime.FocusedCrashPairRuntimeError) as raised:
        runner._execute_focused(
            _direct_focused_invocation(output, mode=mode), backend=backend
        )
    assert raised.value is backend.failure
    pair_root = output / "pair-01"
    failed_root = pair_root / failed_arm
    abort = json.loads((failed_root / "arm-abort.json").read_text())
    pair_abort = json.loads((pair_root / "pair-abort.json").read_text())
    assert (failed_root / "cleanup.json").is_file()
    assert abort["state"] == "INCOMPLETE"
    assert abort["mode"] == mode
    assert abort["slot_id"] == ("slot-01" if failed_arm == "control" else "slot-02")
    assert [row["arm"] for row in pair_abort["completed_prefix"]] == completed_arms
    assert pair_abort["failed_arm"]["arm"] == failed_arm
    assert pair_abort["mode"] == mode
    assert pair_abort["not_started_arms"] == not_started
    assert (failed_root / "evidence-seal.json").is_file()
    assert (pair_root / "evidence-seal.json").is_file()
    failed_seal = runner.verify_evidence_seal(failed_root)
    pair_seal = runner.verify_evidence_seal(pair_root)
    assert pair_abort["failed_arm"]["tree_sha256"] == failed_seal.tree_sha256
    assert pair_abort["failed_arm"]["seal_sha256"] == failed_seal.seal_sha256
    assert pair_seal.seal_sha256
    assert not (output / "campaign-ledger.jsonl").exists()
    assert not (output / "campaign-summary.json").exists()
    assert [call for call in backend.calls if call[0] == "configuration"] == [
        ("configuration", "pair-01", arm)
        for arm in (("control",) if failed_arm == "control" else ("control", "adaptive"))
    ]
    with pytest.raises(runner.FocusedCrashPairValidationError):
        runner.validate_sealed_pair(pair_root, trusted_provenance={})


@pytest.mark.parametrize("cleanup_mode", ("raises", "incomplete"))
def test_smoke_pair_abort_cleanup_failure_is_unsealed(
    cleanup_mode: str, tmp_path: Path
) -> None:
    runner = _runner()
    runtime = importlib.import_module(
        "experiments.adaptive.kauri_experiment.focused_crash_pair_runtime"
    )
    output = tmp_path / cleanup_mode
    backend = _PairAbortBackend(
        runtime, failed_arm="control", cleanup_mode=cleanup_mode
    )
    with pytest.raises(runtime.FocusedCrashPairRuntimeError) as raised:
        runner._execute_focused(
            _direct_focused_invocation(output, mode="smoke"), backend=backend
        )
    assert raised.value is backend.failure
    failed_root = output / "pair-01" / "control"
    assert (failed_root / "cleanup-failure.json").is_file()
    assert not (failed_root / "cleanup.json").exists()
    assert not (failed_root / "arm-abort.json").exists()
    assert not (failed_root / "evidence-seal.json").exists()
    assert not (output / "pair-01" / "evidence-seal.json").exists()


def test_smoke_pair_abort_keeps_original_error_when_seal_and_stderr_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner()
    runtime = importlib.import_module(
        "experiments.adaptive.kauri_experiment.focused_crash_pair_runtime"
    )
    output = tmp_path / "finalizer-failure"
    backend = _PairAbortBackend(runtime, failed_arm="control")
    monkeypatch.setattr(
        runner,
        "create_evidence_seal",
        lambda _directory: (_ for _ in ()).throw(RuntimeError("seal failed")),
    )
    monkeypatch.setattr(
        runner.sys,
        "stderr",
        SimpleNamespace(
            write=lambda _message: (_ for _ in ()).throw(OSError("stderr closed"))
        ),
    )
    with pytest.raises(runtime.FocusedCrashPairRuntimeError) as raised:
        runner._execute_focused(
            _direct_focused_invocation(output, mode="pair"), backend=backend
        )
    assert raised.value is backend.failure
    failed_root = output / "pair-01" / "control"
    assert (failed_root / "cleanup.json").is_file()
    assert (failed_root / "arm-abort.json").is_file()
    assert (output / "pair-01" / "abort-finalization-failure.json").is_file()
    assert not (failed_root / "evidence-seal.json").exists()


def test_smoke_pair_abort_pair_seal_failure_keeps_arm_seal_and_marks_pair_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner()
    runtime = importlib.import_module(
        "experiments.adaptive.kauri_experiment.focused_crash_pair_runtime"
    )
    output = tmp_path / "pair-seal-failure"
    backend = _PairAbortBackend(runtime, failed_arm="control")
    original_seal = runner.create_evidence_seal

    def seal(directory: Path) -> object:
        if Path(directory) == output / "pair-01":
            raise RuntimeError("pair seal failed")
        return original_seal(directory)

    monkeypatch.setattr(runner, "create_evidence_seal", seal)
    with pytest.raises(runtime.FocusedCrashPairRuntimeError) as raised:
        runner._execute_focused(
            _direct_focused_invocation(output, mode="pair"), backend=backend
        )
    assert raised.value is backend.failure
    failed_root = output / "pair-01" / "control"
    assert runner.verify_evidence_seal(failed_root).seal_sha256
    failure = json.loads(
        (output / "pair-01" / "abort-finalization-failure.json").read_text()
    )
    assert failure["category"] == "finalization_error"
    assert not (output / "pair-01" / "evidence-seal.json").exists()


def test_smoke_pair_abort_rejects_lexical_arm_symlink(tmp_path: Path) -> None:
    runner = _runner()
    runtime = importlib.import_module(
        "experiments.adaptive.kauri_experiment.focused_crash_pair_runtime"
    )
    output = tmp_path / "symlink-output"
    pair_root = output / "pair-01"
    target = tmp_path / "target"
    target.mkdir(parents=True)
    pair_root.mkdir(parents=True)
    (pair_root / "control").symlink_to(target, target_is_directory=True)
    slot = {
        "slot_id": "slot-01",
        "pair_id": "pair-01",
        "arm": "control",
        "pair_seed": 41_720,
        "execution_ordinal": 1,
    }
    with pytest.raises(runner.FocusedCrashPairCliError, match="symlink"):
        runner._finalize_pair_abort(
            output_root=output,
            slot=slot,
            configuration={
                **slot,
                "run_directory": pair_root / "control",
            },
            completed_records=(),
            mode="smoke",
            runtime_error=runtime.FocusedCrashPairRuntimeError("native arm failed"),
            cleanup={"complete": True},
            cleanup_error=None,
        )


def test_smoke_pair_abort_rejects_forged_completed_slot_identity(tmp_path: Path) -> None:
    runner = _runner()
    runtime = importlib.import_module(
        "experiments.adaptive.kauri_experiment.focused_crash_pair_runtime"
    )
    output = tmp_path / "forged-prefix"
    pair_root = output / "pair-01"
    control_root = pair_root / "control"
    failed_root = pair_root / "adaptive"
    control_root.mkdir(parents=True)
    failed_root.mkdir()
    seal = runner.create_evidence_seal(control_root)
    slot = {
        "slot_id": "slot-02",
        "pair_id": "pair-01",
        "arm": "adaptive",
        "pair_seed": 41_720,
        "execution_ordinal": 2,
    }
    forged = {
        "slot_id": "slot-99",
        "pair_id": "pair-01",
        "arm": "control",
        "pair_seed": 41_720,
        "execution_ordinal": 1,
        "configuration": {
            "slot_id": "slot-99",
            "pair_id": "pair-01",
            "arm": "control",
            "pair_seed": 41_720,
            "execution_ordinal": 1,
            "run_directory": control_root,
        },
        "seal": {"tree_sha256": seal.tree_sha256, "seal_sha256": seal.seal_sha256},
    }
    with pytest.raises(runner.FocusedCrashPairCliError, match="binding drifted"):
        runner._finalize_pair_abort(
            output_root=output,
            slot=slot,
            configuration={**slot, "run_directory": failed_root},
            completed_records=(forged,),
            mode="pair",
            runtime_error=runtime.FocusedCrashPairRuntimeError("native arm failed"),
            cleanup={"complete": True},
            cleanup_error=None,
        )


def test_smoke_pair_abort_symlink_finalization_failure_never_writes_external(
    tmp_path: Path,
) -> None:
    runner = _runner()
    runtime = importlib.import_module(
        "experiments.adaptive.kauri_experiment.focused_crash_pair_runtime"
    )
    output = tmp_path / "symlink-finalizer"
    external = tmp_path / "external"
    external.mkdir()

    class SymlinkBackend(_PairAbortBackend):
        def run_arm(
            self, configuration: Mapping[str, object], _processes: object
        ) -> Mapping[str, object]:
            pair_root = output / "pair-01"
            shutil.rmtree(pair_root)
            pair_root.symlink_to(external, target_is_directory=True)
            raise self.failure

    backend = SymlinkBackend(runtime, failed_arm="control")
    with pytest.raises(runtime.FocusedCrashPairRuntimeError) as raised:
        runner._execute_focused(
            _direct_focused_invocation(output, mode="smoke"), backend=backend
        )
    assert raised.value is backend.failure
    assert (output / "pair-abort-finalization-failure.json").is_file()
    assert not (external / "abort-finalization-failure.json").exists()


@pytest.mark.parametrize(
    ("child_validation", "expected_outcome", "expected_integrity", "expected_claim"),
    (
        (
            {
                "verdict": "PASS",
                "outcome": "PASS",
                "integrity_valid": True,
                "claim_slot": True,
            },
            "PASS",
            True,
            True,
        ),
        (
            {
                "verdict": "PROVISIONAL",
                "outcome": "INCOMPLETE",
                "integrity_valid": False,
                "claim_slot": False,
            },
            "INCOMPLETE",
            False,
            False,
        ),
    ),
)
def test_canonical_campaign_ledger_uses_actual_child_validation(
    child_validation: Mapping[str, object],
    expected_outcome: str,
    expected_integrity: bool,
    expected_claim: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()

    class ChildValidationBackend(_RecordingLaunchBackend):
        def validate(
            self,
            configuration: Mapping[str, object],
            _seal: Mapping[str, object],
        ) -> Mapping[str, object]:
            self._record("validate", configuration)
            return dict(child_validation)

    output = tmp_path / expected_outcome.lower()
    _stub_aggregate_validators(runner, monkeypatch)
    runner._execute_focused(
        _direct_focused_invocation(output, mode="campaign"),
        backend=ChildValidationBackend(),
    )
    records = [
        json.loads(line)
        for line in (output / "campaign-ledger.jsonl").read_text().splitlines()
    ]
    assert len(records) == 10
    assert {
        (
            record["execution_outcome"],
            record["validation"]["outcome"],
            record["validation"]["integrity_valid"],
            record["validation"]["claim_slot"],
        )
        for record in records
    } == {
        (
            expected_outcome,
            expected_outcome,
            expected_integrity,
            expected_claim,
        )
    }


def test_campaign_has_one_canonical_ledger_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    runtime = importlib.import_module(
        "experiments.adaptive.kauri_experiment.focused_crash_pair_runtime"
    )

    class DefaultLedgerBackend(_RecordingLaunchBackend):
        def __init__(self) -> None:
            super().__init__()
            self._ledger_tail: str | None = None

        def materialize_arm_configuration(
            self,
            context: Mapping[str, object],
            *,
            pair_ordinal: int,
            arm: str,
        ) -> Mapping[str, object]:
            configuration = dict(
                super().materialize_arm_configuration(
                    context, pair_ordinal=pair_ordinal, arm=arm
                )
            )
            configuration["slot_id"] = context["slot_id"]
            return configuration

        append_ledger = runtime.FocusedLaunchBackend.append_ledger

    output = tmp_path / "campaign"
    _stub_aggregate_validators(runner, monkeypatch)
    runner._execute_focused(
        _direct_focused_invocation(output, mode="campaign"),
        backend=DefaultLedgerBackend(),
    )
    assert (output / "campaign-ledger.jsonl").is_file()
    assert not (output / "ledger").exists()
    assert not tuple(output.glob("**/slot-*.json"))


def test_campaign_executes_prederived_slots_without_self_validating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    output = tmp_path / "results"
    backend = _RecordingLaunchBackend()
    monkeypatch.setattr(
        runner,
        "validate_sealed_pair",
        lambda *_args, **_kwargs: pytest.fail("execution self-validated a pair"),
    )
    monkeypatch.setattr(
        runner,
        "validate_sealed_campaign",
        lambda *_args, **_kwargs: pytest.fail("execution self-validated a campaign"),
    )
    runner._execute_focused(
        {
            "mode": "campaign",
            "pair_count": 5,
            "output_root": output,
            "profile": SimpleNamespace(profile_sha256="a" * 64),
            "preflight_receipt": {
                "execution_context": {
                    "issuer_public_key": native_fixture.ISSUER_PUBLIC_KEY,
                    "profile_sha256": "a" * 64,
                    "topology_proof_sha256": "b" * 64,
                }
            },
        },
        backend=backend,
    )
    actual_slots = [
        (pair_id, arm)
        for action, pair_id, arm in backend.calls
        if action == "configuration"
    ]
    expected_slots = [
        (slot["pair_id"], slot["arm"]) for slot in campaign_fixture._expected_slots()
    ]
    children_root = output / "children"
    assert {
        "execution_slots": actual_slots,
        "child_slots": (
            sorted(path.name for path in children_root.iterdir())
            if children_root.is_dir()
            else []
        ),
        "pair_validator_calls": [],
        "campaign_validator_calls": [],
    } == {
        "execution_slots": expected_slots,
        "child_slots": [f"slot-{ordinal:02d}" for ordinal in range(1, 11)],
        "pair_validator_calls": [],
        "campaign_validator_calls": [],
    }


def test_campaign_cannot_pass_with_relabelled_pair_local_arm(
    tmp_path: Path,
) -> None:
    runner = _runner()

    class RelabelledBackend(_RecordingLaunchBackend):
        def validate(
            self,
            configuration: Mapping[str, object],
            seal: Mapping[str, object],
        ) -> Mapping[str, object]:
            result = dict(super().validate(configuration, seal))
            result["pair_id"] = (
                "pair-01"
                if configuration["pair_id"] == "pair-02"
                and configuration["arm"] == "adaptive"
                else configuration["pair_id"]
            )
            result["arm"] = configuration["arm"]
            return result

    with pytest.raises(
        (
            runner.FocusedCrashPairCliError,
            runner.FocusedCrashPairRuntimeError,
            runner.FocusedCrashPairValidationError,
        ),
        match="pair|relabel|identity",
    ):
        runner._execute_focused(
            {
                "mode": "campaign",
                "pair_count": 5,
                "output_root": tmp_path / "relabelled",
                "profile": SimpleNamespace(profile_sha256="a" * 64),
                "preflight_receipt": {
                    "execution_context": {
                        "issuer_public_key": native_fixture.ISSUER_PUBLIC_KEY,
                        "profile_sha256": "a" * 64,
                        "topology_proof_sha256": "b" * 64,
                    }
                },
            },
            backend=RelabelledBackend(),
        )


def test_campaign_child_authorization_derives_without_rewriting_parent(
    tmp_path: Path,
) -> None:
    runtime = importlib.import_module(
        "experiments.adaptive.kauri_experiment.focused_crash_pair_runtime"
    )
    paths = _inputs(
        tmp_path,
        mode="campaign",
        pairs=5,
        output=tmp_path / "results",
    )
    parent = json.loads(paths["authorization"].read_text(encoding="utf-8"))
    child = runtime.derive_focused_child_authorization(
        parent_authorization=parent,
        pair_id="pair-03",
        slot_id="slot-06",
        arm="adaptive",
        pair_seed=41_722,
    )
    assert isinstance(child, dict)
    assert child == {
        "schema_version": 1,
        "parent_authorization": parent,
        "parent_request_sha256": parent["request_sha256"],
        "derivation": {
            "pair_id": "pair-03",
            "slot_id": "slot-06",
            "arm": "adaptive",
            "pair_seed": 41_722,
        },
    }
    assert (
        runtime.verify_focused_child_authorization(
            parent_authorization=parent,
            child_authorization=child,
            pair_id="pair-03",
            slot_id="slot-06",
            arm="adaptive",
            pair_seed=41_722,
        )["derivation"]
        == child["derivation"]
    )
    for field in ("pair_id", "slot_id"):
        changed = json.loads(json.dumps(child))
        changed["derivation"][field] = "relabelled"
        with pytest.raises(runtime.FocusedCrashPairRuntimeError):
            runtime.verify_focused_child_authorization(
                parent_authorization=parent,
                child_authorization=changed,
                pair_id="pair-03",
                slot_id="slot-06",
                arm="adaptive",
                pair_seed=41_722,
            )


class _CliPreflightChecks:
    def __init__(self, allocator: object | None = None) -> None:
        self.calls: list[str] = []
        self.allocator = allocator

    def _check(self, name: str, result: Mapping[str, object]) -> Mapping[str, object]:
        self.calls.append(name)
        return result

    def repository(self, _profile: object) -> Mapping[str, object]:
        return self._check("repository", {"revision": "a" * 40})

    def build(self, _profile: object) -> Mapping[str, object]:
        return self._check("build", {"build_sha256": "b" * 64})

    def binaries(self, _profile: object) -> Mapping[str, object]:
        return self._check("binaries", {"verified": True})

    def ports(self, _profile: object) -> Mapping[str, object]:
        return self._check("ports", {"available": True})

    def clock(self, _profile: object) -> Mapping[str, object]:
        return self._check("clock", {"monotonic": True})

    def native_topology(self, profile: object) -> Mapping[str, object]:
        return self._check(
            "native_topology",
            {
                "epoch_zero_digest": profile.raw["topology"]["epoch_zero_digest"],
                "topology_proof_sha256": profile.topology_proof_sha256,
            },
        )

    def issuer_public_key(self, _profile: object) -> Mapping[str, object]:
        return self._check(
            "issuer_public_key",
            (
                {"issuer_public_key": native_fixture.ISSUER_PUBLIC_KEY}
                if self.allocator is None
                else self.allocator.allocate_pair_issuers()
            ),
        )


def test_default_cli_preflight_binds_checks_and_generated_issuer_into_auth_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    runtime = importlib.import_module(
        "experiments.adaptive.kauri_experiment.focused_crash_pair_runtime"
    )
    profile = runtime.load_focused_profile(N7_PROFILE_V7)
    assert profile.issuer_public_key is None
    checks = _CliPreflightChecks()
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(runner, "FOCUSED_PREFLIGHT_CHECKS", checks, raising=False)
    monkeypatch.setattr(
        runner, "_write_cli_result", lambda value: captured.append(value)
    )
    output = tmp_path / "results"
    assert (
        runner.main(
            [
                "preflight",
                "--mode",
                "smoke",
                "--profile",
                str(N7_PROFILE_V7),
                "--pairs",
                "1",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert checks.calls == [
        "repository",
        "build",
        "binaries",
        "ports",
        "clock",
        "native_topology",
        "issuer_public_key",
    ]
    preflight = captured[0]
    context = preflight["execution_context"]
    assert context["issuer_public_key"] == native_fixture.ISSUER_PUBLIC_KEY
    assert (
        preflight["profile_sha256"]
        == "ca948e998acfd1fc9511139321d11d5de9dd9856eb50293612f2ea31732f7d3d"
    )
    assert (
        preflight["topology_proof_sha256"]
        == "7e2d06acfaeebeb6c4b97fd83726cda86a64e7b9d419103a86502c189c04aa9b"
    )
    request = runtime.build_focused_authorization_request(preflight)
    request_document = json.loads(request)
    assert request_document["profile_sha256"] == preflight["profile_sha256"]
    assert (
        request_document["topology_proof_sha256"] == preflight["topology_proof_sha256"]
    )
    assert (
        request_document["execution_context_sha256"]
        == hashlib.sha256(_canonical(context)).hexdigest()
    )
    receipt = {
        **request_document,
        "request_sha256": hashlib.sha256(request).hexdigest(),
        "approval_reference": "thesis-author-approved-focused-run",
        "approved_utc": "2026-08-12T12:00:00+00:00",
    }
    assert (
        runtime.verify_focused_authorization_receipt(
            request,
            receipt,
        )["execution_context_sha256"]
        == request_document["execution_context_sha256"]
    )
    changed = json.loads(json.dumps(preflight))
    changed["execution_context"]["ports"]["available"] = False
    with pytest.raises(runtime.FocusedCrashPairRuntimeError):
        runtime.build_focused_authorization_request(changed)


@pytest.mark.parametrize(
    ("mode", "pairs", "profile_id", "profile_sha256", "proof_sha256"),
    (
        (
            "smoke",
            1,
            "n7-f2-q5-two-crash-pair-smoke-v7",
            "ca948e998acfd1fc9511139321d11d5de9dd9856eb50293612f2ea31732f7d3d",
            "7e2d06acfaeebeb6c4b97fd83726cda86a64e7b9d419103a86502c189c04aa9b",
        ),
        (
            "pair",
            1,
            "n31-f5-q21-three-crash-pair-v7",
            "188890afb3dd2fff0b2e5f4cbf8614f6a21afdf67a1874844e9b466fe76c5abb",
            "60b53e89d24c76ff2016f43f49dbfdd8c88d80da9c4b96a15251d89a3bc870f3",
        ),
        (
            "campaign",
            5,
            "n31-f5-q21-three-crash-pair-v7",
            "188890afb3dd2fff0b2e5f4cbf8614f6a21afdf67a1874844e9b466fe76c5abb",
            "60b53e89d24c76ff2016f43f49dbfdd8c88d80da9c4b96a15251d89a3bc870f3",
        ),
    ),
)
def test_cli_preflight_defaults_to_the_exact_v7_profile_and_proof(
    mode: str,
    pairs: int,
    profile_id: str,
    profile_sha256: str,
    proof_sha256: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    captured: list[Mapping[str, object]] = []

    def preflight(**kwargs: object) -> dict[str, object]:
        captured.append(kwargs)
        return {"profile_sha256": kwargs["profile"].profile_sha256}

    monkeypatch.setattr(runner, "prepare_focused_preflight", preflight)
    monkeypatch.setattr(runner, "_write_cli_result", lambda _value: None)

    assert (
        runner.main(
            [
                "preflight",
                "--mode",
                mode,
                "--pairs",
                str(pairs),
                "--output",
                str(tmp_path / "results"),
            ]
        )
        == 0
    )

    profile = captured[0]["profile"]
    assert profile.profile_id == profile_id
    assert profile.profile_sha256 == profile_sha256
    assert profile.topology_proof_sha256 == proof_sha256


@pytest.mark.parametrize(
    ("mode", "profile_path", "pairs"),
    (
        ("smoke", runtime_fixture.N7_PROFILE, 1),
        ("smoke", runtime_fixture.N7_PROFILE_V2, 1),
        ("smoke", runtime_fixture.N7_PROFILE_V3, 1),
        ("smoke", PROFILE_ROOT / "n7-f2-q5-two-crash-pair-smoke-v4.json", 1),
        ("smoke", runtime_fixture.N7_PROFILE_V5, 1),
        ("smoke", N7_PROFILE_V6, 1),
        ("pair", runtime_fixture.N31_PROFILE, 1),
        ("pair", runtime_fixture.N31_PROFILE_V2, 1),
        ("pair", runtime_fixture.N31_PROFILE_V3, 1),
        ("pair", PROFILE_ROOT / "n31-f5-q21-three-crash-pair-v4.json", 1),
        ("pair", runtime_fixture.N31_PROFILE_V5, 1),
        ("pair", N31_PROFILE_V6, 1),
        ("campaign", runtime_fixture.N31_PROFILE, 5),
        ("campaign", runtime_fixture.N31_PROFILE_V2, 5),
        ("campaign", runtime_fixture.N31_PROFILE_V3, 5),
        ("campaign", PROFILE_ROOT / "n31-f5-q21-three-crash-pair-v4.json", 5),
        ("campaign", runtime_fixture.N31_PROFILE_V5, 5),
        ("campaign", N31_PROFILE_V6, 5),
    ),
)
def test_cli_rejects_archived_v1_to_v6_profiles_for_new_execution(
    mode: str,
    profile_path: Path,
    pairs: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Archived v1-v6 evidence remains readable but cannot authorize a new run."""

    runner = _runner()
    _execution_sentinels(runner, monkeypatch)
    with pytest.raises(SystemExit):
        runner.main(
            [
                "preflight",
                "--mode",
                mode,
                "--profile",
                str(profile_path),
                "--pairs",
                str(pairs),
                "--output",
                str(tmp_path / mode),
            ]
        )


def test_cli_rejects_rebound_noncanonical_v7_profile_for_preflight_and_execution(
    tmp_path: Path,
) -> None:
    runner = _runner()
    profile = json.loads(N31_PROFILE_V7.read_text(encoding="utf-8"))
    proof_path = runtime_fixture._topology_proof_path(N31_PROFILE_V7, profile)
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    profile["timers"]["arm_hard_deadline_seconds"] = 481
    rebound_proof = tmp_path / "topology-proof.json"
    profile["topology"]["proof_path"] = rebound_proof.name
    proof["profile_sha256"] = runtime_fixture._canonical_profile_sha256(profile)
    rebound_proof.write_bytes(_canonical(proof))
    profile["topology"]["proof_sha256"] = hashlib.sha256(
        rebound_proof.read_bytes()
    ).hexdigest()
    rebound_profile = tmp_path / "profile.json"
    rebound_profile.write_bytes(_canonical(profile))

    with pytest.raises(SystemExit):
        runner.main(
            [
                "preflight",
                "--mode",
                "pair",
                "--profile",
                str(rebound_profile),
                "--pairs",
                "1",
                "--output",
                str(tmp_path / "preflight-output"),
            ]
        )
    with pytest.raises(runner.FocusedCrashPairCliError):
        runner._authorized_execution(
            SimpleNamespace(
                command="pair",
                pairs=1,
                retries=0,
                output=tmp_path / "execution-output",
                profile=rebound_profile,
                preflight_receipt=tmp_path / "unused-preflight.json",
                authorization_receipt=tmp_path / "unused-authorization.json",
            )
        )


def _authorized_receipt(
    runtime: Any, preflight: Mapping[str, object]
) -> dict[str, object]:
    request = runtime.build_focused_authorization_request(preflight)
    return {
        **json.loads(request),
        "request_sha256": hashlib.sha256(request).hexdigest(),
        "approval_reference": "thesis-author-approved-focused-run",
        "approved_utc": "2026-08-12T12:00:00+00:00",
    }


def _issuer_bound_campaign_inputs(
    tmp_path: Path,
) -> tuple[dict[str, Path], dict[str, object]]:
    runtime = importlib.import_module(
        "experiments.adaptive.kauri_experiment.focused_crash_pair_runtime"
    )
    output = tmp_path / "results"
    paths = _inputs(tmp_path, mode="campaign", pairs=5, output=output)
    preflight = json.loads(paths["preflight"].read_text(encoding="utf-8"))
    allocations: dict[str, object] = {}
    for ordinal in range(1, 6):
        pair_id = f"pair-{ordinal:02d}"
        private_path = tmp_path / "issuer-allocation" / pair_id / "issuer.sec"
        public_path = private_path.with_name("issuer.pub")
        private_path.parent.mkdir(parents=True)
        private_path.write_text(f"{ordinal:064x}\n", encoding="ascii")
        private_path.chmod(0o600)
        point = runtime.factorial_validation._secp256k1_multiply(
            ordinal,
            (
                runtime.factorial_validation._SECP256K1_GX,
                runtime.factorial_validation._SECP256K1_GY,
            ),
        )
        assert point is not None
        public_key = f"{2 + point[1] % 2:02x}{point[0]:064x}"
        public_path.write_text(public_key + "\n", encoding="ascii")
        allocations[pair_id] = {
            "private_key_path": str(private_path.resolve()),
            "private_key_sha256": hashlib.sha256(private_path.read_bytes()).hexdigest(),
            "public_key_path": str(public_path.resolve()),
            "public_key": public_key,
        }
    context = {
        "profile_sha256": preflight["profile_sha256"],
        "topology_proof_sha256": preflight["topology_proof_sha256"],
        "issuer_public_key": allocations["pair-01"]["public_key"],
        "pair_issuers": allocations,
    }
    preflight["execution_context"] = context
    preflight["execution_context_sha256"] = hashlib.sha256(
        _canonical(context)
    ).hexdigest()
    request = runtime.build_focused_authorization_request(preflight)
    preflight["request_sha256"] = hashlib.sha256(request).hexdigest()
    paths["preflight"].write_bytes(_canonical(preflight))
    receipt = _authorized_receipt(runtime, preflight)
    paths["authorization"].write_bytes(_canonical(receipt))
    return paths, preflight


class _FakePairIssuerAllocator:
    def __init__(self, allocation_root: Path, pair_count: int) -> None:
        self.allocation_root = allocation_root.resolve()
        self.pair_count = pair_count

    def allocate_pair_issuers(self) -> Mapping[str, object]:
        runtime = importlib.import_module(
            "experiments.adaptive.kauri_experiment.focused_crash_pair_runtime"
        )
        rows: dict[str, object] = {}
        for ordinal in range(1, self.pair_count + 1):
            pair_id = f"pair-{ordinal:02d}"
            pair_root = self.allocation_root / pair_id
            pair_root.mkdir(parents=True)
            private_path = pair_root / "issuer.sec"
            public_path = pair_root / "issuer.pub"
            private_path.write_text(f"{ordinal:064x}\n", encoding="ascii")
            private_path.chmod(0o600)
            point = runtime.factorial_validation._secp256k1_multiply(
                ordinal,
                (
                    runtime.factorial_validation._SECP256K1_GX,
                    runtime.factorial_validation._SECP256K1_GY,
                ),
            )
            assert point is not None
            public_key = f"{2 + point[1] % 2:02x}{point[0]:064x}"
            public_path.write_text(public_key + "\n", encoding="ascii")
            rows[pair_id] = {
                "pair_id": pair_id,
                "private_key_path": str(private_path),
                "private_key_sha256": hashlib.sha256(
                    private_path.read_bytes()
                ).hexdigest(),
                "public_key_path": str(public_path),
                "public_key": public_key,
            }
        return {
            "issuer_public_key": rows["pair-01"]["public_key"],
            "allocation_root": str(self.allocation_root),
            "pair_issuers": rows,
        }


@pytest.mark.parametrize("mutation", (None, "missing", "mode", "bytes"))
def test_separate_cli_execution_reloads_pair_issuer_before_backend(
    mutation: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    paths, preflight = _issuer_bound_campaign_inputs(tmp_path)
    private_path = Path(
        preflight["execution_context"]["pair_issuers"]["pair-01"]["private_key_path"]
    )
    if mutation == "missing":
        private_path.unlink()
    elif mutation == "mode":
        private_path.chmod(0o644)
    elif mutation == "bytes":
        private_path.write_bytes(private_path.read_bytes() + b"drift")

    class Backend:
        def bind_execution_context(self, invocation: Mapping[str, object]) -> object:
            if mutation is not None:
                pytest.fail("issuer mutation reached the launch backend")
            loaded = invocation["pair_issuer_allocations"]
            assert len(loaded) == 5
            for pair_id, issuer in loaded.items():
                assert (
                    issuer["control"]["public_key"] == issuer["adaptive"]["public_key"]
                )
            raise _ExecutionReached

    monkeypatch.setattr(runner, "FOCUSED_LAUNCH_BACKEND", Backend())
    argv = [
        "campaign",
        "--profile",
        str(paths["profile"]),
        "--pairs",
        "5",
        "--preflight-receipt",
        str(paths["preflight"]),
        "--authorization-receipt",
        str(paths["authorization"]),
        "--output",
        str(tmp_path / "results"),
    ]
    expected = (
        pytest.raises(_ExecutionReached)
        if mutation is None
        else pytest.raises(SystemExit)
    )
    with expected:
        runner.main(argv)


def test_default_preflight_persists_and_execution_reloads_pair_issuer_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    runtime = importlib.import_module(
        "experiments.adaptive.kauri_experiment.focused_crash_pair_runtime"
    )
    allocator = _FakePairIssuerAllocator(tmp_path / "issuer-allocation", 5)
    checks = _CliPreflightChecks(allocator)
    monkeypatch.setattr(runner, "FOCUSED_PREFLIGHT_CHECKS", checks, raising=False)
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(runner, "_write_cli_result", captured.append)
    output = tmp_path / "results"
    assert (
        runner.main(
            [
                "preflight",
                "--mode",
                "campaign",
                "--profile",
                str(N31_PROFILE_V7),
                "--pairs",
                "5",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    preflight = captured[0]
    issuer_allocations = preflight["execution_context"]["pair_issuers"]
    serialized = _canonical(preflight)
    assert b'"sec"' not in serialized
    assert all(
        bytes.fromhex(allocation["private_key_sha256"]) not in serialized
        for allocation in issuer_allocations.values()
    )
    assert set(issuer_allocations) == {f"pair-{index:02d}" for index in range(1, 6)}
    public_keys: set[str] = set()
    for pair_id, allocation in issuer_allocations.items():
        private_path = Path(allocation["private_key_path"])
        assert private_path.parent.name == pair_id
        assert stat.S_IMODE(private_path.stat().st_mode) == 0o600
        private_bytes = private_path.read_bytes()
        assert (
            allocation["private_key_sha256"]
            == hashlib.sha256(private_bytes).hexdigest()
        )
        assert "private_key" not in allocation
        public_keys.add(allocation["public_key"])
    assert len(public_keys) == 5
    request = runtime.build_focused_authorization_request(preflight)
    request_document = json.loads(request)
    assert (
        request_document["execution_context_sha256"]
        == hashlib.sha256(_canonical(preflight["execution_context"])).hexdigest()
    )
    receipt = _authorized_receipt(runtime, preflight)

    reloaded = runtime.reload_pair_issuer_allocations(
        preflight=preflight,
        authorization=receipt,
    )
    assert {pair_id: value["public_key"] for pair_id, value in reloaded.items()} == {
        pair_id: value["public_key"] for pair_id, value in issuer_allocations.items()
    }
    for pair_id in issuer_allocations:
        assert (
            reloaded[pair_id]["control"]["public_key"]
            == reloaded[pair_id]["adaptive"]["public_key"]
        )

    first_path = Path(issuer_allocations["pair-01"]["private_key_path"])
    mutations = (
        "missing",
        "mode",
        "bytes",
        "symlink",
        "outside-root",
        "malformed-row",
        "public-drift",
        "context-digest",
        "authorization-digest",
    )
    for mutation in mutations:
        saved = first_path.read_bytes()
        saved_mode = stat.S_IMODE(first_path.stat().st_mode)
        changed_preflight = json.loads(json.dumps(preflight))
        changed_receipt = json.loads(json.dumps(receipt))
        if mutation == "missing":
            first_path.unlink()
        elif mutation == "mode":
            first_path.chmod(0o644)
        elif mutation == "bytes":
            first_path.write_bytes(saved + b"drift")
        elif mutation == "symlink":
            first_path.unlink()
            first_path.symlink_to(tmp_path / "outside.sec")
        elif mutation == "outside-root":
            outside = tmp_path / "outside.sec"
            outside.write_bytes(saved)
            outside.chmod(0o600)
            changed_preflight["execution_context"]["pair_issuers"]["pair-01"][
                "private_key_path"
            ] = str(outside)
        elif mutation == "malformed-row":
            del changed_preflight["execution_context"]["pair_issuers"]["pair-01"][
                "public_key_path"
            ]
        elif mutation == "public-drift":
            changed_preflight["execution_context"]["pair_issuers"]["pair-01"][
                "public_key"
            ] = changed_preflight["execution_context"]["pair_issuers"]["pair-02"][
                "public_key"
            ]
        elif mutation == "context-digest":
            changed_preflight["execution_context_sha256"] = "0" * 64
        else:
            changed_receipt["execution_context_sha256"] = "0" * 64
        with pytest.raises(runtime.FocusedCrashPairRuntimeError):
            runtime.reload_pair_issuer_allocations(
                preflight=changed_preflight,
                authorization=changed_receipt,
            )
        if first_path.is_symlink():
            first_path.unlink()
        if not first_path.exists():
            first_path.write_bytes(saved)
        else:
            first_path.write_bytes(saved)
        first_path.chmod(saved_mode)


@pytest.mark.parametrize("command", ("smoke", "pair", "campaign"))
def test_valid_cli_inputs_reach_exact_execution_route(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    pair_count = 5 if command == "campaign" else 1
    output = tmp_path / "results"
    paths = _inputs(tmp_path, mode=command, pairs=pair_count, output=output)

    def reached(**_kwargs: object) -> None:
        raise _ExecutionReached

    monkeypatch.setattr(runner, "run_focused_pair", reached)
    monkeypatch.setattr(runner, "run_focused_campaign", reached)
    with pytest.raises(_ExecutionReached):
        runner.main(
            [
                command,
                "--profile",
                str(paths["profile"]),
                "--pairs",
                str(pair_count),
                "--preflight-receipt",
                str(paths["preflight"]),
                "--authorization-receipt",
                str(paths["authorization"]),
                "--output",
                str(output),
            ]
        )


@pytest.mark.parametrize("command", ("smoke", "pair", "campaign"))
@pytest.mark.parametrize("missing", ("preflight", "authorization"))
def test_execution_cli_refuses_missing_receipts(
    command: str,
    missing: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    pair_count = 5 if command == "campaign" else 1
    output = tmp_path / "results"
    paths = _inputs(
        tmp_path,
        mode=command,
        pairs=pair_count,
        output=output,
    )
    _execution_sentinels(runner, monkeypatch)
    argv = [
        command,
        "--profile",
        str(paths["profile"]),
        "--pairs",
        str(pair_count),
        "--preflight-receipt",
        str(paths["preflight"]),
        "--authorization-receipt",
        str(paths["authorization"]),
        "--output",
        str(output),
    ]
    flag = f"--{missing}-receipt"
    index = argv.index(flag)
    del argv[index : index + 2]
    with pytest.raises(SystemExit):
        runner.main(argv)


@pytest.mark.parametrize(
    ("command", "pairs"),
    (("smoke", 5), ("pair", 5), ("campaign", 1)),
)
def test_cli_refuses_wrong_pair_count_or_retry_controls(
    command: str,
    pairs: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    expected_pairs = 5 if command == "campaign" else 1
    output = tmp_path / "results"
    paths = _inputs(
        tmp_path,
        mode=command,
        pairs=pairs,
        output=output,
    )
    _execution_sentinels(runner, monkeypatch)
    common = [
        command,
        "--profile",
        str(paths["profile"]),
        "--pairs",
        str(pairs),
        "--preflight-receipt",
        str(paths["preflight"]),
        "--authorization-receipt",
        str(paths["authorization"]),
        "--output",
        str(output),
    ]
    with pytest.raises(SystemExit):
        runner.main(common)
    retry_root = tmp_path / "retry"
    retry_output = retry_root / "results"
    retry_paths = _inputs(
        retry_root,
        mode=command,
        pairs=expected_pairs,
        output=retry_output,
    )
    correct = [
        command,
        "--profile",
        str(retry_paths["profile"]),
        "--pairs",
        str(expected_pairs),
        "--preflight-receipt",
        str(retry_paths["preflight"]),
        "--authorization-receipt",
        str(retry_paths["authorization"]),
        "--output",
        str(retry_output),
    ]
    with pytest.raises(SystemExit):
        runner.main([*correct, "--retries", "1"])


def test_cli_refuses_to_overwrite_allocated_result_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    output = tmp_path / "results"
    paths = _inputs(tmp_path, mode="pair", pairs=1, output=output)
    output.mkdir()
    _execution_sentinels(runner, monkeypatch)
    with pytest.raises(SystemExit):
        runner.main(
            [
                "pair",
                "--profile",
                str(paths["profile"]),
                "--pairs",
                "1",
                "--preflight-receipt",
                str(paths["preflight"]),
                "--authorization-receipt",
                str(paths["authorization"]),
                "--output",
                str(output),
            ]
        )


@pytest.mark.parametrize(
    ("command", "expected_route"),
    (
        ("preflight", "preflight"),
        ("smoke", "pair"),
        ("pair", "pair"),
        ("campaign", "campaign"),
        ("validate-pair", "validate-pair"),
        ("validate-campaign", "validate-campaign"),
    ),
)
def test_cli_routes_each_supported_mode_once(
    command: str,
    expected_route: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    pair_count = 5 if command == "campaign" else 1
    mode = (
        command
        if command in {"smoke", "pair", "campaign"}
        else "smoke" if command == "preflight" else "pair"
    )
    output = tmp_path / "results"
    paths = _inputs(tmp_path, mode=mode, pairs=pair_count, output=output)
    observed: list[tuple[str, dict[str, object]]] = []

    def route(name: str) -> Any:
        def invoke(**kwargs: object) -> dict[str, object]:
            observed.append((name, dict(kwargs)))
            return {"verdict": "PASS", "path": str(tmp_path / name)}

        return invoke

    monkeypatch.setattr(runner, "prepare_focused_preflight", route("preflight"))
    monkeypatch.setattr(runner, "run_focused_pair", route("pair"))
    monkeypatch.setattr(runner, "run_focused_campaign", route("campaign"))
    monkeypatch.setattr(runner, "validate_sealed_pair", route("validate-pair"))
    monkeypatch.setattr(
        runner,
        "validate_sealed_campaign",
        route("validate-campaign"),
    )
    monkeypatch.setattr(
        runner,
        "_write_cli_result",
        lambda value: json.dumps(value, sort_keys=True),
        raising=False,
    )

    if command == "preflight":
        argv = [
            command,
            "--mode",
            "smoke",
            "--profile",
            str(paths["profile"]),
            "--pairs",
            "1",
            "--output",
            str(output),
        ]
    elif command in {"smoke", "pair", "campaign"}:
        argv = [
            command,
            "--profile",
            str(paths["profile"]),
            "--pairs",
            "5" if command == "campaign" else "1",
            "--preflight-receipt",
            str(paths["preflight"]),
            "--authorization-receipt",
            str(paths["authorization"]),
            "--output",
            str(output),
        ]
    else:
        root_flag = "--pair-root" if command == "validate-pair" else "--campaign-root"
        argv = [
            command,
            root_flag,
            str(tmp_path / "sealed"),
            "--trusted-provenance",
            str(paths["trusted"]),
        ]

    assert runner.main(argv) == 0
    assert [name for name, _kwargs in observed] == [expected_route]


@pytest.mark.parametrize("mutation", ("replay", "mismatch"))
def test_cli_refuses_replayed_or_mismatched_authorization_receipt(
    mutation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    output = tmp_path / "results"
    paths = _inputs(tmp_path, mode="pair", pairs=1, output=output)
    _execution_sentinels(runner, monkeypatch)
    if mutation == "replay":
        other = _inputs(
            tmp_path / "prior",
            mode="pair",
            pairs=1,
            output=tmp_path / "prior-results",
        )
    else:
        other = _inputs(
            tmp_path / "other-profile",
            mode="smoke",
            pairs=1,
            output=output,
        )
    paths["authorization"].write_bytes(other["authorization"].read_bytes())
    authorization = json.loads(paths["authorization"].read_text())
    request = {
        key: authorization[key]
        for key in (
            "schema_version",
            "mode",
            "pair_count",
            "profile_sha256",
            "topology_proof_sha256",
            "output_root",
            "automatic_retries",
            "replacement_policy",
            "authorization_nonce",
        )
    }
    assert (
        authorization["request_sha256"]
        == hashlib.sha256(_canonical(request)).hexdigest()
    )
    with pytest.raises(SystemExit):
        runner.main(
            [
                "pair",
                "--profile",
                str(paths["profile"]),
                "--pairs",
                "1",
                "--preflight-receipt",
                str(paths["preflight"]),
                "--authorization-receipt",
                str(paths["authorization"]),
                "--output",
                str(output),
            ]
        )


def test_runner_script_imports_when_invoked_directly_from_kauri_root() -> None:
    repository_root = Path(__file__).parents[3]
    script = (
        repository_root / "experiments" / "adaptive" / "run_focused_n31_crash_pair.py"
    )
    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "preflight" in completed.stdout
