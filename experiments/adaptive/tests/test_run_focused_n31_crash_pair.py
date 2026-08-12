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

from experiments.adaptive.tests import test_focused_crash_pair_runtime as runtime_fixture
from experiments.adaptive.tests import test_n31_crash_pair_contract as native_fixture
from experiments.adaptive.tests import test_run_n31_crash_pair_campaign as campaign_fixture


RUNNER = "experiments.adaptive.run_focused_n31_crash_pair"


def _runner() -> Any:
    return importlib.import_module(RUNNER)


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)
        + "\n"
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
    source_profile = (
        runtime_fixture.N7_PROFILE if mode == "smoke" else runtime_fixture.N31_PROFILE
    )
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
) -> tuple[list[tuple[Path, Mapping[str, object]]], list[tuple[Path, Mapping[str, object]]]]:
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
    assert runner.main(
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
    ) == 0
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
    assert pair_calls == [
        (
            output / f"pair-{pair_ordinal:02d}",
            {"schema_version": 1, "children": {}},
        )
        for pair_ordinal in range(1, pair_count + 1)
    ]
    assert campaign_calls == (
        [(output, {"schema_version": 1, "children": {}})]
        if command == "campaign"
        else []
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
        assert result["campaign_validation"]["verdict"] == "PASS"
    else:
        assert result["pair_validations"] == [
            {"pair_id": "pair-01", "verdict": "PASS"}
        ]
    assert pair_calls == [
        (
            output / f"pair-{ordinal:02d}",
            {"schema_version": 1, "children": {}},
        )
        for ordinal in range(1, pair_count + 1)
    ]
    assert campaign_calls == (
        [(output, {"schema_version": 1, "children": {}})]
        if mode == "campaign"
        else []
    )


@pytest.mark.parametrize(
    ("scope", "failure", "expected_status"),
    (
        ("pair", "exception", "INCOMPLETE"),
        ("pair", "rejection", "FAIL"),
        ("campaign", "exception", "INCOMPLETE"),
        ("campaign", "rejection", "FAIL"),
    ),
)
def test_aggregate_validation_failure_is_never_promoted_to_pass(
    scope: str,
    failure: str,
    expected_status: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    mode = "campaign" if scope == "campaign" else "pair"
    output = tmp_path / f"{scope}-{failure}"
    invocation = _direct_focused_invocation(output, mode=mode)
    backend = _RecordingLaunchBackend()

    def pass_pair(directory: Path, **_kwargs: object) -> Mapping[str, object]:
        return {"pair_id": Path(directory).name, "verdict": "PASS"}

    monkeypatch.setattr(runner, "validate_sealed_pair", pass_pair)
    monkeypatch.setattr(
        runner,
        "validate_sealed_campaign",
        lambda *_args, **_kwargs: {"verdict": "PASS"},
    )
    target = (
        "validate_sealed_pair" if scope == "pair" else "validate_sealed_campaign"
    )
    if failure == "exception":
        def reject(*_args: object, **_kwargs: object) -> Mapping[str, object]:
            raise runner.FocusedCrashPairValidationError(
                f"{scope} aggregate validation failed"
            )
    else:
        def reject(*_args: object, **_kwargs: object) -> Mapping[str, object]:
            return {"verdict": "FAIL"}
    monkeypatch.setattr(runner, target, reject)

    try:
        result = runner._execute_focused(invocation, backend=backend)
    except (
        runner.FocusedCrashPairCliError,
        runner.FocusedCrashPairValidationError,
    ):
        return
    assert result["validation_status"] == expected_status
    aggregate = (
        result["campaign_validation"]
        if scope == "campaign"
        else result["pair_validations"][0]
    )
    assert aggregate["verdict"] == expected_status


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


def test_campaign_executes_prederived_slots_and_invokes_sealed_validators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    output = tmp_path / "results"
    backend = _RecordingLaunchBackend()
    pair_calls: list[Path] = []
    campaign_calls: list[Path] = []

    def validate_pair(pair_directory: Path, **_kwargs: object) -> Mapping[str, object]:
        pair_calls.append(Path(pair_directory))
        return {"verdict": "PASS"}

    def validate_campaign(
        campaign_directory: Path, **_kwargs: object
    ) -> Mapping[str, object]:
        campaign_calls.append(Path(campaign_directory))
        return {"verdict": "PASS"}

    monkeypatch.setattr(runner, "validate_sealed_pair", validate_pair)
    monkeypatch.setattr(runner, "validate_sealed_campaign", validate_campaign)
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
        (slot["pair_id"], slot["arm"])
        for slot in campaign_fixture._expected_slots()
    ]
    children_root = output / "children"
    assert {
        "execution_slots": actual_slots,
        "child_slots": (
            sorted(path.name for path in children_root.iterdir())
            if children_root.is_dir()
            else []
        ),
        "pair_validator_calls": pair_calls,
        "campaign_validator_calls": campaign_calls,
    } == {
        "execution_slots": expected_slots,
        "child_slots": [f"slot-{ordinal:02d}" for ordinal in range(1, 11)],
        "pair_validator_calls": [
            output / f"pair-{ordinal:02d}" for ordinal in range(1, 6)
        ],
        "campaign_validator_calls": [output],
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
    assert runtime.verify_focused_child_authorization(
        parent_authorization=parent,
        child_authorization=child,
        pair_id="pair-03",
        slot_id="slot-06",
        arm="adaptive",
        pair_seed=41_722,
    )["derivation"] == child["derivation"]
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
    profile = runtime.load_focused_profile(runtime_fixture.N7_PROFILE)
    assert profile.issuer_public_key is None
    checks = _CliPreflightChecks()
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(runner, "FOCUSED_PREFLIGHT_CHECKS", checks, raising=False)
    monkeypatch.setattr(runner, "_write_cli_result", lambda value: captured.append(value))
    output = tmp_path / "results"
    assert runner.main(
        [
            "preflight",
            "--mode",
            "smoke",
            "--profile",
            str(runtime_fixture.N7_PROFILE),
            "--pairs",
            "1",
            "--output",
            str(output),
        ]
    ) == 0
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
    request = runtime.build_focused_authorization_request(preflight)
    request_document = json.loads(request)
    assert request_document["execution_context_sha256"] == hashlib.sha256(
        _canonical(context)
    ).hexdigest()
    receipt = {
        **request_document,
        "request_sha256": hashlib.sha256(request).hexdigest(),
        "approval_reference": "thesis-author-approved-focused-run",
        "approved_utc": "2026-08-12T12:00:00+00:00",
    }
    assert runtime.verify_focused_authorization_receipt(
        request,
        receipt,
    )["execution_context_sha256"] == request_document["execution_context_sha256"]
    changed = json.loads(json.dumps(preflight))
    changed["execution_context"]["ports"]["available"] = False
    with pytest.raises(runtime.FocusedCrashPairRuntimeError):
        runtime.build_focused_authorization_request(changed)


def _authorized_receipt(runtime: Any, preflight: Mapping[str, object]) -> dict[str, object]:
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
        preflight["execution_context"]["pair_issuers"]["pair-01"][
            "private_key_path"
        ]
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
                assert issuer["control"]["public_key"] == issuer["adaptive"][
                    "public_key"
                ]
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
    expected = pytest.raises(_ExecutionReached) if mutation is None else pytest.raises(SystemExit)
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
    assert runner.main(
        [
            "preflight",
            "--mode",
            "campaign",
            "--profile",
            str(runtime_fixture.N31_PROFILE),
            "--pairs",
            "5",
            "--output",
            str(output),
        ]
    ) == 0
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
        assert allocation["private_key_sha256"] == hashlib.sha256(private_bytes).hexdigest()
        assert "private_key" not in allocation
        public_keys.add(allocation["public_key"])
    assert len(public_keys) == 5
    request = runtime.build_focused_authorization_request(preflight)
    request_document = json.loads(request)
    assert request_document["execution_context_sha256"] == hashlib.sha256(
        _canonical(preflight["execution_context"])
    ).hexdigest()
    receipt = _authorized_receipt(runtime, preflight)

    reloaded = runtime.reload_pair_issuer_allocations(
        preflight=preflight,
        authorization=receipt,
    )
    assert {
        pair_id: value["public_key"] for pair_id, value in reloaded.items()
    } == {
        pair_id: value["public_key"] for pair_id, value in issuer_allocations.items()
    }
    for pair_id in issuer_allocations:
        assert reloaded[pair_id]["control"]["public_key"] == reloaded[pair_id][
            "adaptive"
        ]["public_key"]

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
        else "smoke"
        if command == "preflight"
        else "pair"
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
    assert authorization["request_sha256"] == hashlib.sha256(
        _canonical(request)
    ).hexdigest()
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
    script = repository_root / "experiments" / "adaptive" / "run_focused_n31_crash_pair.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "preflight" in completed.stdout
