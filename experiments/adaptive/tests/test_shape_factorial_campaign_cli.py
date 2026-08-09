"""Safe CLI-driver tests for the one-shot SHAPE25 campaign."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from experiments.adaptive import run_shape_factorial_campaign as cli
from experiments.adaptive.kauri_experiment import factorial_execution
from experiments.adaptive.kauri_experiment.factorial_manifest import (
    FROZEN_MANIFEST_SHA256,
    FROZEN_PLAN_SHA256,
    LEGACY_MANIFEST_SHA256,
    LEGACY_PLAN_SHA256,
    build_factorial_plan,
    load_frozen_manifest,
)
from experiments.adaptive.kauri_experiment.factorial_runtime import (
    FROZEN_COVERAGE_SMOKE_RUNTIME_SHA256,
    FROZEN_SMOKE_RUNTIME_SHA256,
    build_factorial_runtime,
)
from experiments.adaptive.kauri_experiment.factorial_validation import (
    CampaignValidationResult,
    LEGACY_SMOKE_RUNTIME_SHA256,
    SlotValidationResult,
)


REPOSITORY = Path(__file__).resolve().parents[3]
MANIFEST = REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v17.json"
V16_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v16.json"
)
V15_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v15.json"
)
V14_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v14.json"
)
V13_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v13.json"
)
V12_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v12.json"
)
V11_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v11.json"
)
V10_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v10.json"
)
V9_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v9.json"
)
V8_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v8.json"
)
V7_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v7.json"
)
V6_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v6.json"
)
V5_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v5.json"
)
V4_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v4.json"
)
V3_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v3.json"
)
V2_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v2.json"
)
LEGACY_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v1.json"
)
REVISION = "a" * 40


def _fake_build_provenance(
    repository: Path,
    build_directory: Path,
    revision: str,
) -> dict[str, object]:
    binary_paths = {
        "app": build_directory / "examples/hotstuff-app",
        "manager": build_directory / "examples/adaptation-manager",
        "keygen": build_directory / "hotstuff-keygen",
        "tls_keygen": build_directory / "hotstuff-tls-keygen",
        "epoch_profile_digest": build_directory / "examples/epoch-profile-digest",
    }
    metadata_paths = {
        name: build_directory / "metadata" / name
        for name in (
            "adaptation_manager_link",
            "cmake_cache",
            "compile_commands",
            "epoch_profile_digest_link",
            "hotstuff_app_link",
            "hotstuff_keygen_link",
            "hotstuff_tls_keygen_link",
        )
    }
    for name, path in {**binary_paths, **metadata_paths}.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(f"{name}\n".encode())

    def row(path: Path) -> dict[str, object]:
        payload = path.read_bytes()
        return {
            "path": str(path.resolve()),
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    return {
        "schema_version": 1,
        "revision": revision,
        "repository": str(repository),
        "binaries": {name: row(path) for name, path in binary_paths.items()},
        "build_metadata": {
            name: row(path) for name, path in metadata_paths.items()
        },
    }


@pytest.fixture(scope="module")
def frozen_contract():
    plan = build_factorial_plan(load_frozen_manifest(MANIFEST))
    return plan, build_factorial_runtime(plan)


def _preflight(slot, *, result_root: Path, revision: str = REVISION, **kwargs: Any):
    repository = kwargs["repository"].resolve()
    build_directory = kwargs["build_directory"].resolve()
    binaries = factorial_execution.ExecutionBinaries(
        app=build_directory / "examples/hotstuff-app",
        manager=build_directory / "examples/adaptation-manager",
        keygen=build_directory / "hotstuff-keygen",
        tls_keygen=build_directory / "hotstuff-tls-keygen",
    )
    return factorial_execution.ExecutionPreflight(
        revision=revision,
        repository=repository,
        build_directory=build_directory,
        result_root=result_root.resolve(),
        slot_directory=result_root.resolve() / slot.slot_id,
        free_bytes=100_000_000_000,
        binaries=binaries,
        build_provenance=_fake_build_provenance(
            repository,
            build_directory,
            revision,
        ),
    )


def _campaign_validation(outcome: str = "INCOMPLETE") -> CampaignValidationResult:
    return CampaignValidationResult(
        outcome=outcome,
        reason=None if outcome == "PASS" else "prespecified slots remain",
        slots=(),
        parameter_coverage=(),
        headline_effects=None,
        figure_eligible=False,
    )


def _slot_validation(
    slot_id: str,
    *,
    outcome: str,
    campaign_member: bool,
) -> SlotValidationResult:
    return SlotValidationResult(
        slot_id=slot_id,
        outcome=outcome,
        reason=None if outcome == "PASS" else "preserved incomplete attempt",
        integrity_valid=outcome == "PASS",
        campaign_member=campaign_member,
    )


@pytest.mark.parametrize(
    ("manifest_path", "version"),
    (
        (LEGACY_MANIFEST, "v1"),
        (V2_MANIFEST, "v2"),
        (V3_MANIFEST, "v3"),
        (V4_MANIFEST, "v4"),
        (V5_MANIFEST, "v5"),
        (V6_MANIFEST, "v6"),
        (V7_MANIFEST, "v7"),
        (V8_MANIFEST, "v8"),
        (V9_MANIFEST, "v9"),
        (V10_MANIFEST, "v10"),
        (V11_MANIFEST, "v11"),
        (V12_MANIFEST, "v12"),
        (V13_MANIFEST, "v13"),
        (V14_MANIFEST, "v14"),
        (V15_MANIFEST, "v15"),
        (V16_MANIFEST, "v16"),
    ),
)
@pytest.mark.parametrize(
    "command", ("plan", "preflight", "smoke", "coverage-smoke", "run")
)
def test_prior_manifest_is_validation_only_before_any_result_claim(
    command: str,
    manifest_path: Path,
    version: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "Kauri"
    arguments = [
        command,
        "--manifest",
        str(manifest_path),
        "--repository",
        str(repository),
    ]
    if command in {"smoke", "coverage-smoke", "run"}:
        arguments.extend(("--approval-reference", "must remain unused"))

    assert cli.main(arguments) == 2
    refusal = json.loads(capsys.readouterr().err)

    assert refusal == {
        "reason": (
            "shape-placement-factorial-v1 through v16 are validation-only; "
            "production commands require shape-placement-factorial-v17"
        ),
        "status": "REJECT",
    }
    assert not (repository / f"results/shape-placement-factorial-{version}").exists()
    assert not (
        repository / f"results/shape-placement-factorial-{version}-smoke"
    ).exists()
    assert not (
        repository
        / f"results/shape-placement-factorial-{version}-coverage-smoke"
    ).exists()


def test_smoke_preflight_validates_the_exact_excluded_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "Kauri"
    repository.mkdir()
    monkeypatch.setattr(
        cli.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=20_000_000_000),
    )

    assert cli.main(
        [
            "preflight",
            "--manifest",
            str(MANIFEST),
            "--repository",
            str(repository),
            "--preflight-target",
            "smoke",
        ]
    ) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["status"] == "PASS"
    assert result["target"] == "smoke"
    assert result["slot_count"] == 1
    assert result["runtime_id"].startswith("slot-runtime-")
    assert result["runtime_sha256"] == FROZEN_SMOKE_RUNTIME_SHA256
    assert result["launch_permitted"] is False


def test_smoke_preflight_rejects_runtime_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "Kauri"
    repository.mkdir()
    monkeypatch.setattr(
        cli.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=20_000_000_000),
    )
    monkeypatch.setattr(cli, "FROZEN_SMOKE_RUNTIME_SHA256", "0" * 64)

    assert cli.main(
        [
            "preflight",
            "--manifest",
            str(MANIFEST),
            "--repository",
            str(repository),
            "--preflight-target",
            "smoke",
        ]
    ) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert refusal == {
        "reason": "smoke runtime bytes differ from the exact frozen v17 identity",
        "status": "REJECT",
    }


def test_coverage_smoke_preflight_validates_exact_n31_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "Kauri"
    repository.mkdir()
    monkeypatch.setattr(
        cli.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=20_000_000_000),
    )

    assert cli.main(
        [
            "preflight",
            "--manifest",
            str(MANIFEST),
            "--repository",
            str(repository),
            "--preflight-target",
            "coverage-smoke",
        ]
    ) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["status"] == "PASS"
    assert result["target"] == "coverage-smoke"
    assert result["slot_count"] == 1
    assert result["runtime_id"].startswith("slot-runtime-")
    assert result["runtime_sha256"] == FROZEN_COVERAGE_SMOKE_RUNTIME_SHA256
    assert result["launch_permitted"] is False


@pytest.mark.parametrize(
    ("manifest_path", "version"),
    (
        (LEGACY_MANIFEST, "v1"),
        (V2_MANIFEST, "v2"),
        (V3_MANIFEST, "v3"),
        (V4_MANIFEST, "v4"),
        (V5_MANIFEST, "v5"),
        (V6_MANIFEST, "v6"),
        (V7_MANIFEST, "v7"),
        (V8_MANIFEST, "v8"),
        (V9_MANIFEST, "v9"),
        (V10_MANIFEST, "v10"),
        (V11_MANIFEST, "v11"),
        (V12_MANIFEST, "v12"),
    ),
)
def test_prior_validate_smoke_uses_preserved_artifacts_without_rederiving(
    manifest_path: Path,
    version: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "Kauri"
    observed: list[Path] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("legacy validation must not derive a new plan or runtime")

    monkeypatch.setattr(cli, "build_factorial_plan", forbidden)
    monkeypatch.setattr(cli, "build_factorial_runtime", forbidden)
    monkeypatch.setattr(
        cli,
        "validate_slot",
        lambda path: observed.append(path)
        or SlotValidationResult(
            slot_id=path.name,
            outcome="INCOMPLETE",
            reason=(
                "timed out waiting for exact epoch-1 command, shape decision, "
                "terminal, and activation"
            ),
            integrity_valid=False,
            campaign_member=False,
        ),
    )

    assert cli.main(
        [
            "validate-smoke",
            "--manifest",
            str(manifest_path),
            "--repository",
            str(repository),
        ]
    ) == 1
    output = json.loads(capsys.readouterr().out)

    assert observed == [
        repository
        / f"results/shape-placement-factorial-{version}-smoke/smoke-n7-f2-PS"
    ]
    assert output["validation"]["outcome"] == "INCOMPLETE"
    assert output["validation"]["reason"].startswith("timed out waiting")
    assert "identity" not in output["validation"]["reason"]
    assert not repository.exists()


@pytest.mark.parametrize(
    ("manifest_path", "version"),
    (
        (LEGACY_MANIFEST, "v1"),
        (V2_MANIFEST, "v2"),
        (V3_MANIFEST, "v3"),
        (V4_MANIFEST, "v4"),
        (V5_MANIFEST, "v5"),
        (V6_MANIFEST, "v6"),
        (V7_MANIFEST, "v7"),
        (V8_MANIFEST, "v8"),
        (V9_MANIFEST, "v9"),
        (V10_MANIFEST, "v10"),
        (V11_MANIFEST, "v11"),
        (V12_MANIFEST, "v12"),
    ),
)
def test_prior_validate_campaign_uses_preserved_artifacts_without_rederiving(
    manifest_path: Path,
    version: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "Kauri"
    observed: list[Path] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("legacy validation must not derive a new plan or runtime")

    monkeypatch.setattr(cli, "build_factorial_plan", forbidden)
    monkeypatch.setattr(cli, "build_factorial_runtime", forbidden)
    monkeypatch.setattr(
        cli,
        "validate_campaign",
        lambda path: observed.append(path) or _campaign_validation(),
    )

    assert cli.main(
        [
            "validate-campaign",
            "--manifest",
            str(manifest_path),
            "--repository",
            str(repository),
        ]
    ) == 1
    output = json.loads(capsys.readouterr().out)

    assert observed == [repository / f"results/shape-placement-factorial-{version}"]
    assert output["validation"]["outcome"] == "INCOMPLETE"
    assert not repository.exists()


def test_run_requires_explicit_authorization_before_any_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "Kauri"
    monkeypatch.setattr(cli, "_preflight", _preflight)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("execute_slot_once must not be called without authorization")

    monkeypatch.setattr(cli, "execute_slot_once", forbidden)

    assert cli.main(
        [
            "run",
            "--manifest",
            str(MANIFEST),
            "--repository",
            str(repository),
        ]
    ) == 2
    refusal = json.loads(capsys.readouterr().err)

    assert refusal["status"] == "REJECT"
    assert "approval-reference" in refusal["reason"]
    assert not (repository / "results/shape-placement-factorial-v17").exists()


def test_campaign_runtime_identity_drift_rejects_before_result_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "Kauri"
    monkeypatch.setattr(cli, "canonical_runtime_bytes", lambda _runtime: b"{}\n")

    assert cli.main(
        [
            "run",
            "--manifest",
            str(MANIFEST),
            "--repository",
            str(repository),
            "--approval-reference",
            "must remain unused",
        ]
    ) == 2
    refusal = json.loads(capsys.readouterr().err)

    assert "campaign runtime bytes differ" in refusal["reason"]
    assert not (repository / "results/shape-placement-factorial-v17").exists()


def test_smoke_runtime_identity_drift_rejects_before_result_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "Kauri"
    monkeypatch.setattr(cli, "_direct_runtime_bytes", lambda _runtime: b"{}\n")

    assert cli.main(
        [
            "smoke",
            "--manifest",
            str(MANIFEST),
            "--repository",
            str(repository),
            "--approval-reference",
            "must remain unused",
        ]
    ) == 2
    refusal = json.loads(capsys.readouterr().err)

    assert "smoke runtime bytes differ" in refusal["reason"]
    assert not (
        repository / "results/shape-placement-factorial-v17-smoke"
    ).exists()


def test_coverage_smoke_runtime_identity_drift_rejects_before_result_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "Kauri"
    monkeypatch.setattr(cli, "_direct_runtime_bytes", lambda _runtime: b"{}\n")

    assert cli.main(
        [
            "coverage-smoke",
            "--manifest",
            str(MANIFEST),
            "--repository",
            str(repository),
            "--approval-reference",
            "must remain unused",
        ]
    ) == 2
    refusal = json.loads(capsys.readouterr().err)

    assert "coverage-smoke runtime bytes differ" in refusal["reason"]
    assert not (
        repository / "results/shape-placement-factorial-v17-coverage-smoke"
    ).exists()


def test_invalid_authorization_does_not_claim_the_one_shot_smoke_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "Kauri"
    receipt = tmp_path / "invalid-authorization.json"
    receipt.write_bytes(cli._canonical_json_bytes({}))
    monkeypatch.setattr(cli, "_preflight", _preflight)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("invalid authorization must not publish or launch")

    monkeypatch.setattr(cli, "preserve_build_evidence", forbidden)
    monkeypatch.setattr(cli, "execute_slot_once", forbidden)

    assert cli.main(
        [
            "smoke",
            "--manifest",
            str(MANIFEST),
            "--repository",
            str(repository),
            "--authorization-receipt",
            str(receipt),
        ]
    ) == 2
    refusal = json.loads(capsys.readouterr().err)

    assert refusal["status"] == "REJECT"
    assert "schema drifted" in refusal["reason"]
    assert not (
        repository / "results/shape-placement-factorial-v17-smoke"
    ).exists()


def test_smoke_generates_exact_excluded_receipt_and_validates_independently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "Kauri"
    observed: dict[str, Any] = {}
    monkeypatch.setattr(cli, "_preflight", _preflight)

    def execute(slot, spec, **kwargs: Any):
        observed["slot"] = slot
        observed["spec"] = spec
        observed["campaign_member"] = kwargs["campaign_member"]
        observed["authorization"] = json.loads(kwargs["authorization_receipt"])
        return factorial_execution.SlotExecutionResult(
            slot_directory=kwargs["preflight"].slot_directory,
            outcome="PASS",
            reason=None,
            launch_count=slot.replica_count + 1,
            phase_cutoffs={},
            cleanup_ledger=(),
        )

    monkeypatch.setattr(cli, "execute_slot_once", execute)
    monkeypatch.setattr(
        cli,
        "validate_slot",
        lambda path: _slot_validation(
            path.name, outcome="PASS", campaign_member=False
        ),
    )

    assert cli.main(
        [
            "smoke",
            "--manifest",
            str(MANIFEST),
            "--repository",
            str(repository),
            "--approval-reference",
            "test thesis-author approval",
            "--approved-utc",
            "2026-08-04T00:00:00+00:00",
        ]
    ) == 0
    result = json.loads(capsys.readouterr().out)

    assert observed["slot"].replica_count == 7
    assert observed["slot"].initial_fanout == 2
    assert observed["slot"].arm_code == "PS"
    assert observed["campaign_member"] is False
    assert observed["authorization"]["scope"] == "excluded_n7_smoke"
    assert observed["authorization"]["slot_ids"] == ["smoke-n7-f2-PS"]
    assert observed["authorization"]["kauri_revision"] == REVISION
    assert (
        repository
        / "results/shape-placement-factorial-v17-smoke"
        / cli.SMOKE_AUTHORIZATION_FILENAME
    ).read_bytes() == cli._canonical_json_bytes(observed["authorization"])
    assert result["validation"]["outcome"] == "PASS"
    assert result["campaign_member"] is False
    assert result["figure_eligible"] is False


def test_smoke_accepts_an_existing_exact_authorization_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    frozen_contract,
) -> None:
    plan, _ = frozen_contract
    repository = tmp_path / "Kauri"
    smoke = factorial_execution.build_n7_ps_smoke_slot(plan.slots[0])
    artifacts = {
        "manifest.json": MANIFEST.read_bytes(),
        "plan.json": plan.canonical_bytes,
        "runtime.json": cli._direct_runtime_bytes(smoke.runtime),
    }
    receipt = factorial_execution.build_execution_authorization_receipt(
        scope="excluded_n7_smoke",
        approval_reference="test thesis-author approval",
        approved_utc="2026-08-04T00:00:00+00:00",
        kauri_revision=REVISION,
        slot_ids=(smoke.slot.slot_id,),
        result_root=Path(smoke.slot.result_path).parent.as_posix(),
        static_artifacts=artifacts,
        build_provenance_sha256=hashlib.sha256(
            cli._canonical_json_bytes(
                _fake_build_provenance(
                    repository.resolve(),
                    repository.resolve() / "build-adaptive",
                    REVISION,
                )
            )
        ).hexdigest(),
    )
    receipt_path = tmp_path / "smoke-authorization.json"
    receipt_path.write_bytes(receipt)
    observed: dict[str, bytes] = {}
    monkeypatch.setattr(cli, "_preflight", _preflight)

    def execute(slot, spec, **kwargs: Any):
        observed["receipt"] = kwargs["authorization_receipt"]
        return factorial_execution.SlotExecutionResult(
            slot_directory=kwargs["preflight"].slot_directory,
            outcome="PASS",
            reason=None,
            launch_count=slot.replica_count + 1,
            phase_cutoffs={},
            cleanup_ledger=(),
        )

    monkeypatch.setattr(cli, "execute_slot_once", execute)
    monkeypatch.setattr(
        cli,
        "validate_slot",
        lambda path: _slot_validation(
            path.name, outcome="PASS", campaign_member=False
        ),
    )

    assert cli.main(
        [
            "smoke",
            "--manifest",
            str(MANIFEST),
            "--repository",
            str(repository),
            "--authorization-receipt",
            str(receipt_path),
        ]
    ) == 0
    capsys.readouterr()

    assert observed["receipt"] == receipt


def test_coverage_smoke_requires_independently_passing_n7_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "Kauri"
    monkeypatch.setattr(cli, "_preflight", _preflight)
    monkeypatch.setattr(
        cli,
        "_require_validated_smoke",
        lambda _root, **_kwargs: (_ for _ in ()).throw(
            factorial_execution.FactorialExecutionError(
                "canonical excluded N=7 smoke did not independently validate PASS"
            )
        ),
    )

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("coverage launch must not start before the N=7 gate")

    monkeypatch.setattr(cli, "preserve_build_evidence", forbidden)
    monkeypatch.setattr(cli, "execute_slot_once", forbidden)

    assert cli.main(
        [
            "coverage-smoke",
            "--manifest",
            str(MANIFEST),
            "--repository",
            str(repository),
            "--approval-reference",
            "test thesis-author approval",
        ]
    ) == 2
    refusal = json.loads(capsys.readouterr().err)

    assert refusal["status"] == "REJECT"
    assert "N=7 smoke" in refusal["reason"]
    assert not (
        repository / "results/shape-placement-factorial-v17-coverage-smoke"
    ).exists()


def test_coverage_smoke_uses_exact_first_slot_and_separate_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    frozen_contract,
) -> None:
    plan, _ = frozen_contract
    repository = tmp_path / "Kauri"
    observed: dict[str, Any] = {}
    monkeypatch.setattr(cli, "_preflight", _preflight)

    def require_n7(root: Path, **kwargs: Any) -> None:
        observed["n7_root"] = root
        observed["n7_gate"] = kwargs

    monkeypatch.setattr(cli, "_require_validated_smoke", require_n7)

    def execute(slot, spec, **kwargs: Any):
        observed["slot"] = slot
        observed["spec"] = spec
        observed["campaign_member"] = kwargs["campaign_member"]
        observed["authorization"] = json.loads(kwargs["authorization_receipt"])
        return factorial_execution.SlotExecutionResult(
            slot_directory=kwargs["preflight"].slot_directory,
            outcome="PASS",
            reason=None,
            launch_count=slot.replica_count + 1,
            phase_cutoffs={},
            cleanup_ledger=(),
        )

    monkeypatch.setattr(cli, "execute_slot_once", execute)
    monkeypatch.setattr(
        cli,
        "validate_slot",
        lambda path: _slot_validation(
            path.name, outcome="PASS", campaign_member=False
        ),
    )

    assert cli.main(
        [
            "coverage-smoke",
            "--manifest",
            str(MANIFEST),
            "--repository",
            str(repository),
            "--approval-reference",
            "test thesis-author approval",
            "--approved-utc",
            "2026-08-04T00:00:00+00:00",
        ]
    ) == 0
    result = json.loads(capsys.readouterr().out)

    first = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    assert observed["slot"] == replace(
        first,
        result_path=(
            "results/shape-placement-factorial-v17-coverage-smoke/"
            "slot-066-n31-f5-b05-P"
        ),
    )
    assert observed["spec"].replica_count == 31
    assert observed["spec"].tree_count == 21
    assert observed["campaign_member"] is False
    authorization = observed["authorization"]
    assert authorization["scope"] == "excluded_n31_coverage_smoke"
    assert authorization["slot_ids"] == ["slot-066-n31-f5-b05-P"]
    assert authorization["result_root"] == (
        "results/shape-placement-factorial-v17-coverage-smoke"
    )
    assert authorization["automatic_retries"] == 0
    assert authorization["replacement_policy"] == "none"
    assert authorization["static_artifacts_sha256"] == {
        "manifest.json": FROZEN_MANIFEST_SHA256,
        "plan.json": FROZEN_PLAN_SHA256,
        "runtime.json": FROZEN_COVERAGE_SMOKE_RUNTIME_SHA256,
    }
    assert observed["n7_root"] == (
        repository / "results/shape-placement-factorial-v17-smoke"
    )
    assert (
        repository
        / "results/shape-placement-factorial-v17-coverage-smoke"
        / cli.COVERAGE_SMOKE_AUTHORIZATION_FILENAME
    ).read_bytes() == cli._canonical_json_bytes(authorization)
    assert result["validation"]["outcome"] == "PASS"
    assert result["campaign_member"] is False
    assert result["figure_eligible"] is False


def test_campaign_requires_independently_passing_excluded_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "Kauri"
    monkeypatch.setattr(
        cli,
        "_require_validated_smoke",
        lambda _root, **_kwargs: (_ for _ in ()).throw(
            factorial_execution.FactorialExecutionError(
                "canonical excluded N=7 smoke did not independently validate PASS"
            )
        ),
    )
    monkeypatch.setattr(cli, "_preflight", _preflight)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("launch must not start before the smoke gate")

    monkeypatch.setattr(cli, "execute_slot_once", forbidden)

    assert cli.main(
        [
            "run",
            "--manifest",
            str(MANIFEST),
            "--repository",
            str(repository),
            "--approval-reference",
            "test thesis-author approval",
        ]
    ) == 2
    refusal = json.loads(capsys.readouterr().err)

    assert refusal["status"] == "REJECT"
    assert "N=7 smoke" in refusal["reason"]


def test_campaign_requires_independently_passing_n31_coverage_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "Kauri"
    monkeypatch.setattr(cli, "_preflight", _preflight)
    monkeypatch.setattr(
        cli, "_require_validated_smoke", lambda _root, **_kwargs: None
    )
    monkeypatch.setattr(
        cli,
        "_require_validated_coverage_smoke",
        lambda _root, **_kwargs: (_ for _ in ()).throw(
            factorial_execution.FactorialExecutionError(
                "canonical excluded N=31 coverage smoke did not independently "
                "validate PASS"
            )
        ),
    )

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("campaign launch must not start before both smoke gates")

    monkeypatch.setattr(cli, "preserve_build_evidence", forbidden)
    monkeypatch.setattr(cli, "execute_slot_once", forbidden)

    assert cli.main(
        [
            "run",
            "--manifest",
            str(MANIFEST),
            "--repository",
            str(repository),
            "--approval-reference",
            "test thesis-author approval",
        ]
    ) == 2
    refusal = json.loads(capsys.readouterr().err)

    assert refusal["status"] == "REJECT"
    assert "N=31 coverage smoke" in refusal["reason"]
    assert not (
        repository / "results/shape-placement-factorial-v17"
    ).exists()


def test_campaign_smoke_gate_binds_exact_revision_and_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke_root = tmp_path / "smoke"
    slot_root = smoke_root / "smoke-n7-f2-PS"
    (slot_root / "runtime").mkdir(parents=True)
    build_provenance = {"schema_version": 1, "revision": REVISION}
    expected_static_hashes = {
        "manifest.json": FROZEN_MANIFEST_SHA256,
        "plan.json": FROZEN_PLAN_SHA256,
        "runtime.json": FROZEN_SMOKE_RUNTIME_SHA256,
    }
    (slot_root / "execution-authorization.json").write_bytes(
        cli._canonical_json_bytes(
            {
                "kauri_revision": REVISION,
                "static_artifacts_sha256": expected_static_hashes,
            }
        )
    )
    (slot_root / "runtime/exact-build-provenance.json").write_bytes(
        cli._canonical_json_bytes(build_provenance)
    )
    monkeypatch.setattr(
        cli,
        "validate_slot",
        lambda path: _slot_validation(
            path.name, outcome="PASS", campaign_member=False
        ),
    )

    result = cli._require_validated_smoke(
        smoke_root,
        expected_revision=REVISION,
        expected_build_provenance=build_provenance,
        expected_static_artifacts_sha256=expected_static_hashes,
    )

    assert result.outcome == "PASS"
    with pytest.raises(
        factorial_execution.FactorialExecutionError,
        match="exact revision and build",
    ):
        cli._require_validated_smoke(
            smoke_root,
            expected_revision="b" * 40,
            expected_build_provenance=build_provenance,
            expected_static_artifacts_sha256=expected_static_hashes,
        )

    (slot_root / "execution-authorization.json").write_bytes(
        cli._canonical_json_bytes(
            {
                "kauri_revision": REVISION,
                "static_artifacts_sha256": {
                    "manifest.json": LEGACY_MANIFEST_SHA256,
                    "plan.json": LEGACY_PLAN_SHA256,
                    "runtime.json": LEGACY_SMOKE_RUNTIME_SHA256,
                },
            }
        )
    )
    with pytest.raises(
        factorial_execution.FactorialExecutionError,
        match="frozen static artifacts",
    ):
        cli._require_validated_smoke(
            smoke_root,
            expected_revision=REVISION,
            expected_build_provenance=build_provenance,
            expected_static_artifacts_sha256=expected_static_hashes,
        )


def test_campaign_coverage_smoke_gate_binds_exact_revision_build_and_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coverage_root = tmp_path / "coverage-smoke"
    slot_root = coverage_root / "slot-066-n31-f5-b05-P"
    (slot_root / "runtime").mkdir(parents=True)
    build_provenance = {"schema_version": 1, "revision": REVISION}
    expected_static_hashes = {
        "manifest.json": FROZEN_MANIFEST_SHA256,
        "plan.json": FROZEN_PLAN_SHA256,
        "runtime.json": FROZEN_COVERAGE_SMOKE_RUNTIME_SHA256,
    }
    authorization_path = slot_root / "execution-authorization.json"
    authorization_path.write_bytes(
        cli._canonical_json_bytes(
            {
                "kauri_revision": REVISION,
                "static_artifacts_sha256": expected_static_hashes,
            }
        )
    )
    (slot_root / "runtime/exact-build-provenance.json").write_bytes(
        cli._canonical_json_bytes(build_provenance)
    )
    monkeypatch.setattr(
        cli,
        "validate_slot",
        lambda path: _slot_validation(
            path.name, outcome="PASS", campaign_member=False
        ),
    )

    result = cli._require_validated_coverage_smoke(
        coverage_root,
        expected_revision=REVISION,
        expected_build_provenance=build_provenance,
        expected_static_artifacts_sha256=expected_static_hashes,
    )

    assert result.outcome == "PASS"
    with pytest.raises(
        factorial_execution.FactorialExecutionError,
        match="exact revision and build",
    ):
        cli._require_validated_coverage_smoke(
            coverage_root,
            expected_revision="b" * 40,
            expected_build_provenance=build_provenance,
            expected_static_artifacts_sha256=expected_static_hashes,
        )

    authorization_path.write_bytes(
        cli._canonical_json_bytes(
            {
                "kauri_revision": REVISION,
                "static_artifacts_sha256": {
                    **expected_static_hashes,
                    "runtime.json": FROZEN_SMOKE_RUNTIME_SHA256,
                },
            }
        )
    )
    with pytest.raises(
        factorial_execution.FactorialExecutionError,
        match="frozen static artifacts",
    ):
        cli._require_validated_coverage_smoke(
            coverage_root,
            expected_revision=REVISION,
            expected_build_provenance=build_provenance,
            expected_static_artifacts_sha256=expected_static_hashes,
        )


def test_campaign_uses_exact_execution_order_and_stops_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    frozen_contract,
) -> None:
    _, runtime = frozen_contract
    repository = tmp_path / "Kauri"
    executed: list[str] = []
    validations: dict[str, str] = {}
    published_summaries: list[Path] = []
    monkeypatch.setattr(cli, "_preflight", _preflight)
    monkeypatch.setattr(
        cli, "_require_validated_smoke", lambda _root, **_kwargs: None
    )
    monkeypatch.setattr(
        cli, "_require_validated_coverage_smoke", lambda _root, **_kwargs: None
    )
    original_publish_summary = cli.publish_campaign_summary

    def publish_summary(path: Path, value: object) -> None:
        published_summaries.append(path)
        original_publish_summary(path, value)

    monkeypatch.setattr(cli, "publish_campaign_summary", publish_summary)

    def execute(slot, spec, **kwargs: Any):
        executed.append(slot.slot_id)
        outcome = "PASS" if len(executed) == 1 else "INCOMPLETE"
        validations[slot.slot_id] = outcome
        if outcome == "PASS":
            kwargs["preflight"].slot_directory.mkdir()
            (kwargs["preflight"].slot_directory / "execution-authorization.json").write_bytes(
                kwargs["authorization_receipt"]
            )
        return factorial_execution.SlotExecutionResult(
            slot_directory=kwargs["preflight"].slot_directory,
            outcome=outcome,
            reason=None if outcome == "PASS" else "synthetic preserved failure",
            launch_count=slot.replica_count + 1,
            phase_cutoffs={} if outcome == "PASS" else None,
            cleanup_ledger=(),
        )

    monkeypatch.setattr(cli, "execute_slot_once", execute)
    monkeypatch.setattr(
        cli,
        "validate_slot",
        lambda path: _slot_validation(
            path.name,
            outcome=validations[path.name],
            campaign_member=True,
        ),
    )
    def validate_after_summary(root: Path) -> CampaignValidationResult:
        summary_path = root / cli.CAMPAIGN_SUMMARY_FILENAME
        assert summary_path.is_file()
        summary = json.loads(summary_path.read_bytes())
        assert summary_path.read_bytes() == cli._canonical_json_bytes(summary)
        assert "independent_campaign_outcome" not in summary
        return _campaign_validation()

    monkeypatch.setattr(cli, "validate_campaign", validate_after_summary)

    assert cli.main(
        [
            "run",
            "--manifest",
            str(MANIFEST),
            "--repository",
            str(repository),
            "--approval-reference",
            "test thesis-author approval",
            "--approved-utc",
            "2026-08-04T00:00:00+00:00",
        ]
    ) == 1
    result = json.loads(capsys.readouterr().out)
    root = repository / runtime.results_root
    ledger = [
        json.loads(line)
        for line in (root / cli.CAMPAIGN_LEDGER_FILENAME).read_text().splitlines()
    ]
    summary = json.loads((root / cli.CAMPAIGN_SUMMARY_FILENAME).read_text())

    assert executed == [runtime.slots[0].slot_id, runtime.slots[1].slot_id]
    assert [row["state"] for row in ledger] == [
        "STARTED",
        "TERMINAL",
        "STARTED",
        "TERMINAL",
    ]
    assert [row["execution_ordinal"] for row in ledger] == [1, 1, 2, 2]
    assert all(row["attempt_ordinal"] == 1 for row in ledger)
    assert all(row["automatic_retries"] == 0 for row in ledger)
    assert all(row["replacement_policy"] == "none" for row in ledger)
    assert ledger[-1]["validation"]["outcome"] == "INCOMPLETE"
    assert summary["attempted_slot_count"] == 2
    assert summary["next_execution_ordinal"] == 3
    assert summary["execution_complete"] is False
    assert published_summaries == [root / cli.CAMPAIGN_SUMMARY_FILENAME]
    assert result["attempted_slot_count"] == 2
    assert "without retry or replacement" in result["stopped_reason"]

@pytest.mark.parametrize(
    ("drift", "reason_fragment"),
    (
        ("revision", "revision drifted without launch"),
        ("build", "build provenance drifted without launch"),
    ),
)
def test_campaign_does_not_launch_next_slot_after_preflight_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    frozen_contract,
    drift: str,
    reason_fragment: str,
) -> None:
    _, runtime = frozen_contract
    repository = tmp_path / "Kauri"
    preflight_count = 0
    executed: list[str] = []
    monkeypatch.setattr(
        cli, "_require_validated_smoke", lambda _root, **_kwargs: None
    )
    monkeypatch.setattr(
        cli, "_require_validated_coverage_smoke", lambda _root, **_kwargs: None
    )

    def drifting_preflight(slot, **kwargs: Any):
        nonlocal preflight_count
        preflight_count += 1
        result = _preflight(slot, revision=REVISION, **kwargs)
        if preflight_count == 1:
            return result
        if drift == "revision":
            return replace(result, revision="b" * 40)
        return replace(
            result,
            build_provenance={**result.build_provenance, "drift": True},
        )

    monkeypatch.setattr(cli, "_preflight", drifting_preflight)

    def execute(slot, spec, **kwargs: Any):
        executed.append(slot.slot_id)
        kwargs["preflight"].slot_directory.mkdir()
        (kwargs["preflight"].slot_directory / "execution-authorization.json").write_bytes(
            kwargs["authorization_receipt"]
        )
        return factorial_execution.SlotExecutionResult(
            slot_directory=kwargs["preflight"].slot_directory,
            outcome="PASS",
            reason=None,
            launch_count=slot.replica_count + 1,
            phase_cutoffs={},
            cleanup_ledger=(),
        )

    monkeypatch.setattr(cli, "execute_slot_once", execute)
    monkeypatch.setattr(
        cli,
        "validate_slot",
        lambda path: _slot_validation(
            path.name, outcome="PASS", campaign_member=True
        ),
    )
    monkeypatch.setattr(cli, "validate_campaign", lambda _root: _campaign_validation())

    assert cli.main(
        [
            "run",
            "--manifest",
            str(MANIFEST),
            "--repository",
            str(repository),
            "--approval-reference",
            "test thesis-author approval",
            "--approved-utc",
            "2026-08-04T00:00:00+00:00",
        ]
    ) == 1
    result = json.loads(capsys.readouterr().out)

    assert executed == [runtime.slots[0].slot_id]
    assert result["attempted_slot_count"] == 1
    assert reason_fragment in result["stopped_reason"]


def test_validate_campaign_uses_only_the_independent_validator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    frozen_contract,
) -> None:
    _, runtime = frozen_contract
    repository = tmp_path / "Kauri"
    root = repository / runtime.results_root
    calls: list[Path] = []
    validation = CampaignValidationResult(
        outcome="PASS",
        reason=None,
        slots=(),
        parameter_coverage=(),
        headline_effects=None,
        figure_eligible=True,
    )

    def independent_validator(path: Path) -> CampaignValidationResult:
        calls.append(path)
        return validation

    monkeypatch.setattr(cli, "validate_campaign", independent_validator)

    assert cli.main(
        [
            "validate-campaign",
            "--manifest",
            str(MANIFEST),
            "--repository",
            str(repository),
        ]
    ) == 0
    output = json.loads(capsys.readouterr().out)

    assert calls == [root]
    assert output["validation"]["outcome"] == "PASS"
    assert output["figure_eligible"] is True
    assert output["ledger_sha256"] is None


def test_validation_commands_accept_explicit_relocated_roots_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    relocated_campaign = tmp_path / "archive" / "campaign"
    relocated_smoke = tmp_path / "archive" / "smoke"
    relocated_coverage_smoke = tmp_path / "archive" / "coverage-smoke"
    campaign_calls: list[Path] = []
    smoke_calls: list[Path] = []
    monkeypatch.setattr(
        cli,
        "validate_campaign",
        lambda path: campaign_calls.append(path)
        or CampaignValidationResult(
            outcome="PASS",
            reason=None,
            slots=(),
            parameter_coverage=(),
            headline_effects=None,
            figure_eligible=True,
        ),
    )
    monkeypatch.setattr(
        cli,
        "validate_slot",
        lambda path: smoke_calls.append(path)
        or _slot_validation(path.name, outcome="PASS", campaign_member=False),
    )

    assert cli.main(
        [
            "validate-campaign",
            "--manifest",
            str(MANIFEST),
            "--campaign-results-root",
            str(relocated_campaign),
        ]
    ) == 0
    capsys.readouterr()
    assert cli.main(
        [
            "validate-smoke",
            "--manifest",
            str(MANIFEST),
            "--smoke-results-root",
            str(relocated_smoke),
        ]
    ) == 0
    capsys.readouterr()
    assert cli.main(
        [
            "validate-coverage-smoke",
            "--manifest",
            str(MANIFEST),
            "--coverage-smoke-results-root",
            str(relocated_coverage_smoke),
        ]
    ) == 0
    capsys.readouterr()

    assert campaign_calls == [relocated_campaign.resolve()]
    assert smoke_calls == [
        relocated_smoke.resolve() / "smoke-n7-f2-PS",
        relocated_coverage_smoke.resolve() / "slot-066-n31-f5-b05-P",
    ]
    assert not relocated_campaign.exists()
    assert not relocated_smoke.exists()
    assert not relocated_coverage_smoke.exists()


@pytest.mark.parametrize(
    ("command", "root_flag"),
    (
        ("validate-campaign", "--campaign-results-root"),
        ("validate-smoke", "--smoke-results-root"),
        (
            "validate-coverage-smoke",
            "--coverage-smoke-results-root",
        ),
    ),
)
def test_validation_rejects_a_symlink_result_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    root_flag: str,
) -> None:
    real_root = tmp_path / "archive" / "real"
    real_root.mkdir(parents=True)
    linked_root = tmp_path / "archive" / "linked"
    linked_root.symlink_to(real_root, target_is_directory=True)

    assert cli.main(
        [
            command,
            "--manifest",
            str(MANIFEST),
            root_flag,
            str(linked_root),
        ]
    ) == 2
    refusal = json.loads(capsys.readouterr().err)

    assert refusal["status"] == "REJECT"
    assert "must not be a symlink" in refusal["reason"]


def test_run_still_rejects_relocated_result_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "Kauri"
    relocated = tmp_path / "archive" / "campaign"
    assert cli.main(
        [
            "run",
            "--manifest",
            str(MANIFEST),
            "--repository",
            str(repository),
            "--campaign-results-root",
            str(relocated),
            "--approval-reference",
            "test approval",
        ]
    ) == 2
    reason = json.loads(capsys.readouterr().err)["reason"]
    assert "campaign results root must be exact" in reason
