"""Prospective producer contract for inherited wait-exempt placement v25."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from experiments.adaptive import run_shape_factorial_campaign as cli
from experiments.adaptive.kauri_experiment import factorial_execution as execution
from experiments.adaptive.kauri_experiment import factorial_manifest as manifest_module
from experiments.adaptive.kauri_experiment import factorial_runtime as runtime_module


REPOSITORY = Path(__file__).resolve().parents[3]
V25_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v25.json"
)
V24_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v24.json"
)
PLACEMENT_FIELD = "inherited_consensus_wait_exempt_placement_contract"
PLACEMENT_CONTRACT = (
    "exact_selected_wait_exempt_replicas_are_excluded_from_root_and_internal_"
    "assignment_and_placed_as_leaves_in_every_successor_tree_v1"
)
REVISION = "a" * 40


def _encoded(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def _parse_candidate(
    monkeypatch: pytest.MonkeyPatch, document: dict[str, object] | None = None
):
    source = json.loads(V25_MANIFEST.read_bytes()) if document is None else document
    semantic = _encoded(source)
    monkeypatch.setattr(
        manifest_module,
        "FROZEN_SEMANTIC_SHA256",
        hashlib.sha256(semantic).hexdigest(),
    )
    return manifest_module.parse_manifest_bytes(V25_MANIFEST.read_bytes() if document is None else semantic)


def _coverage_contract_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    manifest = _parse_candidate(monkeypatch)
    plan = manifest_module.build_factorial_plan(manifest)
    monkeypatch.setattr(
        manifest_module,
        "FROZEN_MANIFEST_SHA256",
        manifest.manifest_sha256,
    )
    monkeypatch.setattr(manifest_module, "FROZEN_PLAN_SHA256", plan.plan_sha256)
    runtime = runtime_module.build_factorial_runtime(plan)
    coverage = cli._n31_coverage_smoke(plan)
    assert isinstance(coverage.runtime, execution.N31CoverageSmokeRuntime)
    static_artifacts = {
        "manifest.json": V25_MANIFEST.read_bytes(),
        "plan.json": plan.canonical_bytes,
        "runtime.json": cli._direct_runtime_bytes(coverage.runtime),
    }
    repository = tmp_path / "Kauri"
    root = repository / "results/shape-placement-factorial-v25-coverage-smoke"
    root.mkdir(parents=True)
    (root / execution.BUILD_EVIDENCE_DIRECTORY).mkdir()
    build_provenance = {"schema_version": 1, "revision": REVISION}
    authorization_payload = execution.build_execution_authorization_receipt(
        scope="excluded_n31_coverage_smoke",
        approval_reference="test thesis-author approval",
        approved_utc="2026-08-10T00:00:00+00:00",
        kauri_revision=REVISION,
        slot_ids=tuple(slot.slot_id for slot in coverage.slots),
        result_root=Path(coverage.slot.result_path).parent.as_posix(),
        static_artifacts=static_artifacts,
        build_provenance_sha256=hashlib.sha256(
            execution._canonical_json_bytes(build_provenance)
        ).hexdigest(),
    )
    authorization = json.loads(authorization_payload)
    contract_payload = execution._canonical_json_bytes(
        execution.build_coverage_smoke_execution_contract(
            runtime=coverage.runtime,
            static_artifacts=static_artifacts,
            authorization=authorization,
            authorization_payload=authorization_payload,
            build_provenance=build_provenance,
        )
    )
    (root / execution.COVERAGE_SMOKE_AUTHORIZATION_FILENAME).write_bytes(
        authorization_payload
    )
    (root / execution.COVERAGE_SMOKE_CONTRACT_FILENAME).write_bytes(
        contract_payload
    )

    def preflight(index: int):
        slot = coverage.slots[index]
        return execution.ExecutionPreflight(
            revision=REVISION,
            repository=repository,
            build_directory=repository / "build-adaptive",
            result_root=root,
            slot_directory=root / slot.slot_id,
            free_bytes=100_000_000_000,
            binaries=execution.ExecutionBinaries(
                app=repository / "build-adaptive/examples/hotstuff-app",
                manager=repository / "build-adaptive/examples/adaptation-manager",
                keygen=repository / "build-adaptive/hotstuff-keygen",
                tls_keygen=repository / "build-adaptive/hotstuff-tls-keygen",
            ),
            build_provenance=build_provenance,
        )

    validation = {
        "outcome": "PASS",
        "reason": None,
        "integrity_valid": True,
        "campaign_member": False,
        "figure_eligible": False,
    }
    rows: list[dict[str, object]] = []

    def previous() -> str:
        return (
            "0" * 64
            if not rows
            else hashlib.sha256(execution._canonical_json_bytes(rows[-1])).hexdigest()
        )

    rows.append(
        execution.build_coverage_smoke_started_record(
            runtime=coverage.runtime,
            spec=coverage.runtimes[0],
            coverage_execution_ordinal=1,
            preflight=preflight(0),
            static_artifacts=static_artifacts,
            authorization=authorization,
            authorization_payload=authorization_payload,
            contract_payload=contract_payload,
            previous_record_sha256=previous(),
            recorded_utc="2026-08-10T00:00:01+00:00",
            recorded_monotonic_ns=1,
        )
    )
    return SimpleNamespace(
        manifest=manifest,
        plan=plan,
        runtime=runtime,
        coverage=coverage,
        static_artifacts=static_artifacts,
        repository=repository,
        root=root,
        build_provenance=build_provenance,
        authorization_payload=authorization_payload,
        authorization=authorization,
        contract_payload=contract_payload,
        preflight=preflight,
        validation=validation,
        rows=rows,
        previous=previous,
    )


def _add_primary_terminal_and_repair_start(fixture) -> None:
    primary = fixture.coverage.runtimes[0]
    fixture.rows.append(
        execution.build_coverage_smoke_terminal_record(
            runtime=fixture.coverage.runtime,
            spec=primary,
            coverage_execution_ordinal=1,
            execution=execution.SlotExecutionResult(
                slot_directory=fixture.root / primary.slot_id,
                outcome="PASS",
                reason=None,
                launch_count=primary.replica_count + 1,
                phase_cutoffs={},
                cleanup_ledger=(),
            ),
            validation=fixture.validation,
            static_artifacts=fixture.static_artifacts,
            authorization=fixture.authorization,
            authorization_payload=fixture.authorization_payload,
            contract_payload=fixture.contract_payload,
            build_provenance=fixture.build_provenance,
            previous_record_sha256=fixture.previous(),
            recorded_utc="2026-08-10T00:00:02+00:00",
            recorded_monotonic_ns=2,
        )
    )
    repair = fixture.coverage.runtimes[1]
    fixture.rows.append(
        execution.build_coverage_smoke_started_record(
            runtime=fixture.coverage.runtime,
            spec=repair,
            coverage_execution_ordinal=2,
            preflight=fixture.preflight(1),
            static_artifacts=fixture.static_artifacts,
            authorization=fixture.authorization,
            authorization_payload=fixture.authorization_payload,
            contract_payload=fixture.contract_payload,
            previous_record_sha256=fixture.previous(),
            recorded_utc="2026-08-10T00:00:03+00:00",
            recorded_monotonic_ns=3,
        )
    )


def _write_coverage_ledger(fixture) -> None:
    (fixture.root / execution.COVERAGE_SMOKE_LEDGER_FILENAME).write_bytes(
        b"".join(execution._canonical_json_bytes(row) for row in fixture.rows)
    )


def _write_primary_predecessor_artifacts(fixture) -> None:
    primary_root = fixture.root / fixture.coverage.slots[0].slot_id
    (primary_root / "runtime").mkdir(parents=True)
    (primary_root / "execution-authorization.json").write_bytes(
        fixture.authorization_payload
    )
    (primary_root / "runtime/exact-build-provenance.json").write_bytes(
        execution._canonical_json_bytes(fixture.build_provenance)
    )
    (primary_root / "outcome.json").write_bytes(
        execution._canonical_json_bytes(
            {
                "schema_version": 1,
                "slot_id": fixture.coverage.slots[0].slot_id,
                "history": [
                    {"sequence": 0, "state": "NOT_STARTED", "reason": None},
                    {"sequence": 1, "state": "PASS", "reason": None},
                ],
                "sealed_files": {"manifest.json": "ab" * 32},
            }
        )
    )


def _pass_validation(
    path: Path,
    *,
    _coverage_predecessor_replay: bool = False,
):
    return cli.SlotValidationResult(
        slot_id=path.name,
        outcome="PASS",
        reason=None,
        integrity_valid=True,
        campaign_member=False,
    )


def _slot_launch_receipt(slot_root: Path, slot_id: str) -> bytes:
    return execution._canonical_json_bytes(
        {
            "schema_version": 1,
            "slot_id": slot_id,
            "replica_argv": [
                {
                    "replica_id": 0,
                    "argv": [str(slot_root / "runtime/main.conf")],
                }
            ],
        }
    )


def _complete_coverage_fixture(
    monkeypatch: pytest.MonkeyPatch,
    fixture,
) -> None:
    _add_primary_terminal_and_repair_start(fixture)
    _write_coverage_ledger(fixture)
    _write_primary_predecessor_artifacts(fixture)
    monkeypatch.setattr(
        "experiments.adaptive.kauri_experiment.factorial_validation.validate_slot",
        _pass_validation,
    )
    binding = execution._validate_coverage_smoke_launch_order(
        spec=fixture.coverage.runtimes[1],
        preflight=fixture.preflight(1),
        static_artifacts=fixture.static_artifacts,
        authorization_receipt=fixture.authorization_payload,
        authorization=fixture.authorization,
    )
    primary_root = fixture.root / fixture.coverage.slots[0].slot_id
    (primary_root / execution.COVERAGE_SMOKE_CONTRACT_FILENAME).write_bytes(
        fixture.contract_payload
    )
    (primary_root / execution.COVERAGE_SMOKE_LEDGER_PREFIX_FILENAME).write_bytes(
        execution._canonical_json_bytes(fixture.rows[0])
    )
    (primary_root / "slot.json").write_bytes(
        _slot_launch_receipt(primary_root, fixture.coverage.slots[0].slot_id)
    )
    repair = fixture.coverage.runtimes[1]
    fixture.rows.append(
        execution.build_coverage_smoke_terminal_record(
            runtime=fixture.coverage.runtime,
            spec=repair,
            coverage_execution_ordinal=2,
            execution=execution.SlotExecutionResult(
                slot_directory=fixture.root / repair.slot_id,
                outcome="PASS",
                reason=None,
                launch_count=repair.replica_count + 1,
                phase_cutoffs={},
                cleanup_ledger=(),
            ),
            validation=fixture.validation,
            static_artifacts=fixture.static_artifacts,
            authorization=fixture.authorization,
            authorization_payload=fixture.authorization_payload,
            contract_payload=fixture.contract_payload,
            build_provenance=fixture.build_provenance,
            previous_record_sha256=fixture.previous(),
            recorded_utc="2026-08-10T00:00:04+00:00",
            recorded_monotonic_ns=4,
        )
    )
    _write_coverage_ledger(fixture)
    repair_root = fixture.root / repair.slot_id
    (repair_root / "runtime").mkdir(parents=True)
    (repair_root / "execution-authorization.json").write_bytes(
        fixture.authorization_payload
    )
    (repair_root / "runtime/exact-build-provenance.json").write_bytes(
        execution._canonical_json_bytes(fixture.build_provenance)
    )
    (repair_root / execution.COVERAGE_SMOKE_CONTRACT_FILENAME).write_bytes(
        fixture.contract_payload
    )
    (repair_root / execution.COVERAGE_SMOKE_LEDGER_PREFIX_FILENAME).write_bytes(
        b"".join(execution._canonical_json_bytes(row) for row in fixture.rows[:3])
    )
    assert binding.predecessor_receipt_payload is not None
    (
        repair_root / execution.COVERAGE_SMOKE_PREDECESSOR_RECEIPT_FILENAME
    ).write_bytes(binding.predecessor_receipt_payload)
    (repair_root / "slot.json").write_bytes(
        _slot_launch_receipt(repair_root, repair.slot_id)
    )
    (repair_root / "outcome.json").write_bytes(
        execution._canonical_json_bytes(
            {
                "schema_version": 1,
                "slot_id": repair.slot_id,
                "history": [
                    {"sequence": 0, "state": "NOT_STARTED", "reason": None},
                    {"sequence": 1, "state": "PASS", "reason": None},
                ],
                "sealed_files": {"manifest.json": "cd" * 32},
            }
        )
    )


def _runtime_identities(path: Path) -> tuple[str, str, str, str, str, str]:
    manifest = manifest_module.load_frozen_manifest(path)
    plan = manifest_module.build_factorial_plan(manifest)
    runtime = runtime_module.build_factorial_runtime(plan)
    n7 = execution.build_n7_ps_smoke_slot(plan.slots[0])
    first = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    if manifest.manifest_id in {
        manifest_module.V25_MANIFEST_ID,
        manifest_module.FROZEN_MANIFEST_ID,
    }:
        repair = next(slot for slot in plan.slots if slot.execution_ordinal == 5)
        n31 = execution.build_n31_coverage_smoke_slot(
            first,
            repair_template=repair,
        )
    else:
        n31 = execution.build_n31_coverage_smoke_slot(first)
    semantic = _encoded(json.loads(path.read_bytes()))
    return (
        manifest.manifest_sha256,
        hashlib.sha256(semantic).hexdigest(),
        plan.plan_sha256,
        hashlib.sha256(runtime_module.canonical_runtime_bytes(runtime)).hexdigest(),
        hashlib.sha256(execution._canonical_json_bytes(n7.runtime.as_document())).hexdigest(),
        hashlib.sha256(execution._canonical_json_bytes(n31.runtime.as_document())).hexdigest(),
    )


def test_v25_profile_delta_is_exactly_three_authorized_json_paths() -> None:
    v25 = json.loads(V25_MANIFEST.read_bytes())
    v24 = json.loads(V24_MANIFEST.read_bytes())

    assert v25.pop("manifest_id") == "shape-placement-factorial-v25"
    assert v24.pop("manifest_id") == "shape-placement-factorial-v24"
    assert v25["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v25"
    )
    assert v24["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v24"
    )
    assert v25["byzantine"]["responsive_degradation"].pop(  # type: ignore[index]
        PLACEMENT_FIELD
    ) == PLACEMENT_CONTRACT
    assert PLACEMENT_FIELD not in v24["byzantine"][  # type: ignore[operator]
        "responsive_degradation"
    ]
    assert v25 == v24


def test_v25_requires_the_exact_new_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    document = json.loads(V25_MANIFEST.read_bytes())
    responsive = document["byzantine"]["responsive_degradation"]

    missing = json.loads(json.dumps(document))
    missing["byzantine"]["responsive_degradation"].pop(PLACEMENT_FIELD)
    with pytest.raises(manifest_module.FactorialManifestError, match="placement"):
        _parse_candidate(monkeypatch, missing)

    responsive[PLACEMENT_FIELD] = f"{PLACEMENT_CONTRACT}-drift"
    with pytest.raises(manifest_module.FactorialManifestError, match="placement"):
        _parse_candidate(monkeypatch, document)


def test_v25_propagates_contract_without_schedule_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _parse_candidate(monkeypatch)
    plan = manifest_module.build_factorial_plan(manifest)
    runtime = runtime_module.build_factorial_runtime(plan)
    v24_plan = manifest_module.build_factorial_plan(
        manifest_module.load_frozen_manifest(V24_MANIFEST)
    )

    assert manifest.byzantine.responsive_degradation is not None
    assert (
        getattr(manifest.byzantine.responsive_degradation, PLACEMENT_FIELD)
        == PLACEMENT_CONTRACT
    )
    assert len(plan.slots) == len(v24_plan.slots) == 68
    assert plan.execution_schedule == v24_plan.execution_schedule
    assert plan.automatic_retries == 0
    assert plan.replacement_policy == "none"
    for slot, prior in zip(plan.slots, v24_plan.slots, strict=True):
        responsive = slot.byzantine.responsive_degradation
        assert responsive is not None
        assert getattr(responsive, PLACEMENT_FIELD) == PLACEMENT_CONTRACT
        assert slot.execution_ordinal == prior.execution_ordinal
        assert slot.block_id == prior.block_id
        assert slot.arm_code == prior.arm_code
        spec = runtime_module.build_slot_runtime(slot)
        assert getattr(spec.causal_acceptance, PLACEMENT_FIELD) == PLACEMENT_CONTRACT
        assert spec.causal_acceptance.as_document()[PLACEMENT_FIELD] == PLACEMENT_CONTRACT
        assert spec.transitions[1].request.minimum_predecessor_residency_ms == 60_000
        assert (
            spec.causal_acceptance.minimum_primary_n31_f5_epoch1_internal_role_opportunities_per_actor_before_selection
            == 82
        )

    assert runtime.automatic_retries == 0
    assert runtime.replacement_policy == "none"


def test_v25_contract_changes_slot_artifact_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _parse_candidate(monkeypatch)
    slot = manifest_module.build_factorial_plan(manifest).slots[0]
    responsive = slot.byzantine.responsive_degradation
    assert responsive is not None
    candidate = runtime_module.build_slot_runtime(slot)
    historical = replace(
        slot,
        byzantine=replace(
            slot.byzantine,
            responsive_degradation=replace(
                responsive,
                inherited_consensus_wait_exempt_placement_contract=None,
            ),
        ),
    )
    assert runtime_module.build_slot_runtime(historical).artifact_id != candidate.artifact_id

    with pytest.raises(manifest_module.FactorialManifestError, match="placement"):
        runtime_module.build_slot_runtime(
            replace(
                slot,
                byzantine=replace(
                    slot.byzantine,
                    responsive_degradation=replace(
                        responsive,
                        inherited_consensus_wait_exempt_placement_contract=(
                            f"{PLACEMENT_CONTRACT}-drift"
                        ),
                    ),
                ),
            )
        )


def test_v24_preserves_all_six_frozen_identities() -> None:
    assert _runtime_identities(V24_MANIFEST) == (
        manifest_module.V24_MANIFEST_SHA256,
        manifest_module.V24_SEMANTIC_SHA256,
        manifest_module.V24_PLAN_SHA256,
        runtime_module.V24_RUNTIME_SHA256,
        runtime_module.V24_SMOKE_RUNTIME_SHA256,
        runtime_module.V24_COVERAGE_SMOKE_RUNTIME_SHA256,
    )


def test_v25_freezes_all_six_recomputed_identities() -> None:
    assert _runtime_identities(V25_MANIFEST) == (
        manifest_module.V25_MANIFEST_SHA256,
        manifest_module.V25_SEMANTIC_SHA256,
        manifest_module.V25_PLAN_SHA256,
        runtime_module.V25_RUNTIME_SHA256,
        runtime_module.V25_SMOKE_RUNTIME_SHA256,
        runtime_module.V25_COVERAGE_SMOKE_RUNTIME_SHA256,
    )


def test_v25_is_validation_only_and_keeps_its_ordered_zero_retry_roots(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.DEFAULT_MANIFEST.name == "shape-placement-factorial-v43.json"
    assert cli.main(["--manifest", str(V25_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v42 are validation-only" in refusal["reason"]

    manifest = _parse_candidate(monkeypatch)
    plan = manifest_module.build_factorial_plan(manifest)
    n7 = execution.build_n7_ps_smoke_slot(plan.slots[0])
    first = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    repair = next(slot for slot in plan.slots if slot.execution_ordinal == 5)
    n31 = execution.build_n31_coverage_smoke_slot(
        first,
        repair_template=repair,
    )
    assert n7.slot.result_path == (
        "results/shape-placement-factorial-v25-smoke/smoke-n7-f2-PS"
    )
    assert tuple(slot.result_path for slot in n31.slots) == (
        "results/shape-placement-factorial-v25-coverage-smoke/slot-066-n31-f5-b05-P",
        "results/shape-placement-factorial-v25-coverage-smoke/slot-037-n31-f2-b04-00",
    )
    assert tuple(slot.slot_id for slot in n31.runtimes) == (
        "slot-066-n31-f5-b05-P",
        "slot-037-n31-f2-b04-00",
    )
    assert isinstance(n31.runtime, execution.N31CoverageSmokeRuntime)
    assert n31.runtime.automatic_retries == 0
    assert n31.runtime.replacement_policy == "none"
    assert n31.runtime.stop_on_first_non_pass is True
    assert n7.runtime.transitions[1].request.minimum_predecessor_residency_ms == 60_000
    assert all(
        spec.transitions[1].request.minimum_predecessor_residency_ms == 60_000
        for spec in n31.runtimes
    )


def _exercise_v25_coverage_smoke(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    first_validation_outcome: str,
) -> tuple[int, dict[str, object], list[str], list[dict[str, object]]]:
    manifest = _parse_candidate(monkeypatch)
    plan = manifest_module.build_factorial_plan(manifest)
    runtime = runtime_module.build_factorial_runtime(plan)
    coverage = cli._n31_coverage_smoke(plan)
    coverage_payload = cli._direct_runtime_bytes(coverage.runtime)
    monkeypatch.setattr(
        cli,
        "FROZEN_COVERAGE_SMOKE_RUNTIME_SHA256",
        hashlib.sha256(coverage_payload).hexdigest(),
    )
    events: list[str] = []
    authorizations: list[dict[str, object]] = []
    ledger_rows: list[dict[str, object]] = []
    repository = tmp_path / "Kauri"
    coverage_root = (
        repository / "results/shape-placement-factorial-v25-coverage-smoke"
    )

    def preflight(slot, **_kwargs):
        events.append(f"preflight:{slot.slot_id}")
        return SimpleNamespace(
            revision="a" * 40,
            repository=repository,
            result_root=coverage_root,
            slot_directory=coverage_root / slot.slot_id,
            free_bytes=100_000_000_000,
            build_provenance={"schema_version": 1, "revision": "a" * 40},
        )

    def execute(slot, _spec, **kwargs):
        events.append(f"execute:{slot.slot_id}")
        authorizations.append(json.loads(kwargs["authorization_receipt"]))
        return execution.SlotExecutionResult(
            slot_directory=kwargs["preflight"].slot_directory,
            outcome="PASS",
            reason=None,
            launch_count=slot.replica_count + 1,
            phase_cutoffs={},
            cleanup_ledger=(),
        )

    def validate(path: Path, *, campaign_member: bool):
        assert campaign_member is False
        events.append(f"validate:{path.name}")
        outcome = (
            first_validation_outcome
            if path.name == "slot-066-n31-f5-b05-P"
            else "PASS"
        )
        return cli.SlotValidationResult(
            slot_id=path.name,
            outcome=outcome,
            reason=None if outcome == "PASS" else "red fixture",
            integrity_valid=outcome == "PASS",
            campaign_member=False,
        )

    monkeypatch.setattr(cli, "_preflight", preflight)
    monkeypatch.setattr(cli, "_require_fresh_result_root", lambda *_args: None)
    monkeypatch.setattr(cli, "_require_validated_smoke", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "preserve_build_evidence", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli,
        "coverage_smoke_previous_record_sha256",
        lambda _path: (
            "0" * 64
            if not ledger_rows
            else hashlib.sha256(cli._canonical_json_bytes(ledger_rows[-1])).hexdigest()
        ),
    )
    monkeypatch.setattr(
        cli,
        "append_coverage_smoke_ledger_record",
        lambda _path, value: ledger_rows.append(value),
    )
    monkeypatch.setattr(cli, "execute_slot_once", execute)
    monkeypatch.setattr(cli, "_validate_attempt", validate)
    arguments = SimpleNamespace(
        manifest=V25_MANIFEST,
        approval_reference="test thesis-author approval",
        authorization_receipt=None,
        approved_utc="2026-08-10T00:00:00+00:00",
    )
    code, result = cli._run_coverage_smoke(
        arguments,
        plan=plan,
        runtime=runtime,
        repository=repository,
        build_directory=repository / "build-adaptive",
        build_provenance=repository / "build-adaptive/build-provenance.json",
        smoke_root=repository / "results/shape-placement-factorial-v25-smoke",
        coverage_smoke_root=coverage_root,
    )
    return code, result, events, authorizations


def test_v25_coverage_smoke_runs_and_validates_exact_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    code, result, events, authorizations = _exercise_v25_coverage_smoke(
        monkeypatch,
        tmp_path,
        first_validation_outcome="PASS",
    )

    assert code == 0
    assert result["attempted_slot_count"] == result["expected_slot_count"] == 2
    assert events == [
        "preflight:slot-066-n31-f5-b05-P",
        "execute:slot-066-n31-f5-b05-P",
        "validate:slot-066-n31-f5-b05-P",
        "preflight:slot-037-n31-f2-b04-00",
        "execute:slot-037-n31-f2-b04-00",
        "validate:slot-037-n31-f2-b04-00",
    ]
    assert len(authorizations) == 2
    assert authorizations[0] == authorizations[1]
    assert authorizations[0]["slot_ids"] == [
        "slot-066-n31-f5-b05-P",
        "slot-037-n31-f2-b04-00",
    ]
    assert authorizations[0]["automatic_retries"] == 0
    assert authorizations[0]["replacement_policy"] == "none"


def test_v25_coverage_smoke_stops_before_repair_on_first_non_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    code, result, events, authorizations = _exercise_v25_coverage_smoke(
        monkeypatch,
        tmp_path,
        first_validation_outcome="FAIL",
    )

    assert code == 1
    assert result["attempted_slot_count"] == 1
    assert result["expected_slot_count"] == 2
    assert events == [
        "preflight:slot-066-n31-f5-b05-P",
        "execute:slot-066-n31-f5-b05-P",
        "validate:slot-066-n31-f5-b05-P",
    ]
    assert len(authorizations) == 1


def test_v25_direct_or_reverse_coverage_launch_cannot_bypass_ledger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = _coverage_contract_fixture(monkeypatch, tmp_path)
    with pytest.raises(
        execution.FactorialExecutionError,
        match="ledger",
    ):
        execution._validate_coverage_smoke_launch_order(
            spec=fixture.coverage.runtimes[0],
            preflight=fixture.preflight(0),
            static_artifacts=fixture.static_artifacts,
            authorization_receipt=fixture.authorization_payload,
            authorization=fixture.authorization,
        )

    _add_primary_terminal_and_repair_start(fixture)
    _write_coverage_ledger(fixture)
    with pytest.raises(
        execution.FactorialExecutionError,
        match="prefix|predecessor",
    ):
        execution._validate_coverage_smoke_launch_order(
            spec=fixture.coverage.runtimes[1],
            preflight=fixture.preflight(1),
            static_artifacts=fixture.static_artifacts,
            authorization_receipt=fixture.authorization_payload,
            authorization=fixture.authorization,
        )


def test_v25_repair_launch_replays_exact_primary_terminal_and_seal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = _coverage_contract_fixture(monkeypatch, tmp_path)
    _add_primary_terminal_and_repair_start(fixture)
    _write_coverage_ledger(fixture)
    _write_primary_predecessor_artifacts(fixture)

    replay_modes: list[bool] = []

    def validate_predecessor(
        path: Path,
        *,
        _coverage_predecessor_replay: bool = False,
    ) -> cli.SlotValidationResult:
        replay_modes.append(_coverage_predecessor_replay)
        return cli.SlotValidationResult(
            slot_id=path.name,
            outcome="PASS",
            reason=None,
            integrity_valid=True,
            campaign_member=False,
        )

    monkeypatch.setattr(
        "experiments.adaptive.kauri_experiment.factorial_validation.validate_slot",
        validate_predecessor,
    )

    binding = execution._validate_coverage_smoke_launch_order(
        spec=fixture.coverage.runtimes[1],
        preflight=fixture.preflight(1),
        static_artifacts=fixture.static_artifacts,
        authorization_receipt=fixture.authorization_payload,
        authorization=fixture.authorization,
    )

    assert binding.contract_payload == fixture.contract_payload
    assert binding.ledger_prefix_payload == b"".join(
        execution._canonical_json_bytes(row) for row in fixture.rows
    )
    receipt = json.loads(binding.predecessor_receipt_payload)
    assert receipt["predecessor_slot_id"] == "slot-066-n31-f5-b05-P"
    assert receipt["predecessor_validation"] == fixture.validation
    assert replay_modes == [True]


@pytest.mark.parametrize(
    ("mutation", "match"),
    (
        ("non_pass", "non-PASS"),
        ("hash_chain", "hash chain|identity"),
        ("below_disk_threshold", "preflight"),
    ),
)
def test_v25_repair_launch_rejects_terminal_chain_or_disk_bypass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
    match: str,
) -> None:
    fixture = _coverage_contract_fixture(monkeypatch, tmp_path)
    _add_primary_terminal_and_repair_start(fixture)
    if mutation == "non_pass":
        fixture.rows[1]["validation"] = {
            **fixture.validation,
            "outcome": "FAIL",
            "integrity_valid": False,
        }
        fixture.rows[2]["previous_record_sha256"] = hashlib.sha256(
            execution._canonical_json_bytes(fixture.rows[1])
        ).hexdigest()
    elif mutation == "hash_chain":
        fixture.rows[2]["previous_record_sha256"] = "0" * 64
    else:
        fixture.rows[0]["preflight_free_bytes"] = 0
        fixture.rows[1]["previous_record_sha256"] = hashlib.sha256(
            execution._canonical_json_bytes(fixture.rows[0])
        ).hexdigest()
        fixture.rows[2]["previous_record_sha256"] = hashlib.sha256(
            execution._canonical_json_bytes(fixture.rows[1])
        ).hexdigest()
    _write_coverage_ledger(fixture)
    _write_primary_predecessor_artifacts(fixture)
    monkeypatch.setattr(
        "experiments.adaptive.kauri_experiment.factorial_validation.validate_slot",
        lambda path, *, _coverage_predecessor_replay=False: cli.SlotValidationResult(
            slot_id=path.name,
            outcome="PASS",
            reason=None,
            integrity_valid=True,
            campaign_member=False,
        ),
    )

    with pytest.raises(execution.FactorialExecutionError, match=match):
        execution._validate_coverage_smoke_launch_order(
            spec=fixture.coverage.runtimes[1],
            preflight=fixture.preflight(1),
            static_artifacts=fixture.static_artifacts,
            authorization_receipt=fixture.authorization_payload,
            authorization=fixture.authorization,
        )


def test_v25_completed_coverage_sequence_replays_exact_rows_and_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = _coverage_contract_fixture(monkeypatch, tmp_path)
    _complete_coverage_fixture(monkeypatch, fixture)

    replayed = execution.verify_completed_coverage_smoke_sequence(
        fixture.root,
        runtime=fixture.coverage.runtime,
        static_artifacts=fixture.static_artifacts,
        authorization_payload=fixture.authorization_payload,
        build_provenance=fixture.build_provenance,
        require_canonical_root=True,
    )

    assert tuple(result.slot_id for result in replayed) == (
        "slot-066-n31-f5-b05-P",
        "slot-037-n31-f2-b04-00",
    )


@pytest.mark.parametrize(
    ("mutation", "match"),
    (
        ("missing_contract", "contract"),
        ("missing_ledger", "ledger"),
        ("receipt", "receipt"),
        ("extra_row_field", "schema"),
        ("path", "slot root|preflight"),
    ),
)
def test_v25_completed_coverage_sequence_rejects_missing_or_tampered_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
    match: str,
) -> None:
    fixture = _coverage_contract_fixture(monkeypatch, tmp_path)
    _complete_coverage_fixture(monkeypatch, fixture)
    if mutation == "missing_contract":
        (fixture.root / execution.COVERAGE_SMOKE_CONTRACT_FILENAME).unlink()
    elif mutation == "missing_ledger":
        (fixture.root / execution.COVERAGE_SMOKE_LEDGER_FILENAME).unlink()
    elif mutation == "receipt":
        receipt = (
            fixture.root
            / fixture.coverage.slots[1].slot_id
            / execution.COVERAGE_SMOKE_PREDECESSOR_RECEIPT_FILENAME
        )
        document = json.loads(receipt.read_bytes())
        document["predecessor_terminal_record_sha256"] = "0" * 64
        receipt.write_bytes(execution._canonical_json_bytes(document))
    elif mutation == "extra_row_field":
        fixture.rows[3]["unexpected"] = True
        _write_coverage_ledger(fixture)
    else:
        receipt = (
            fixture.root / fixture.coverage.slots[0].slot_id / "slot.json"
        )
        document = json.loads(receipt.read_bytes())
        document["replica_argv"][0]["argv"] = [
            "/tampered/slot-066-n31-f5-b05-P/runtime/main.conf"
        ]
        receipt.write_bytes(execution._canonical_json_bytes(document))

    with pytest.raises(execution.FactorialExecutionError, match=match):
        execution.verify_completed_coverage_smoke_sequence(
            fixture.root,
            runtime=fixture.coverage.runtime,
            static_artifacts=fixture.static_artifacts,
            authorization_payload=fixture.authorization_payload,
            build_provenance=fixture.build_provenance,
            require_canonical_root=True,
        )


def test_v25_standalone_validation_accepts_relocated_sealed_copy_but_campaign_does_not(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = _coverage_contract_fixture(monkeypatch, tmp_path)
    _complete_coverage_fixture(monkeypatch, fixture)
    relocated = tmp_path / "archive" / fixture.root.name
    relocated.parent.mkdir()
    shutil.move(str(fixture.root), relocated)

    replayed = execution.verify_completed_coverage_smoke_sequence(
        relocated,
        runtime=fixture.coverage.runtime,
        static_artifacts=fixture.static_artifacts,
        authorization_payload=fixture.authorization_payload,
        build_provenance=fixture.build_provenance,
    )
    assert len(replayed) == 2
    with pytest.raises(
        execution.FactorialExecutionError,
        match="relocated",
    ):
        execution.verify_completed_coverage_smoke_sequence(
            relocated,
            runtime=fixture.coverage.runtime,
            static_artifacts=fixture.static_artifacts,
            authorization_payload=fixture.authorization_payload,
            build_provenance=fixture.build_provenance,
            require_canonical_root=True,
        )


@pytest.mark.parametrize(
    ("mutation", "reason_fragment"),
    (
        ("missing_contract", "contract"),
        ("missing_ledger", "ledger"),
        ("tampered_receipt", "receipt"),
        ("extra_ledger_field", "schema"),
    ),
)
def test_v25_official_coverage_validation_rejects_incomplete_or_tampered_sequence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mutation: str,
    reason_fragment: str,
) -> None:
    fixture = _coverage_contract_fixture(monkeypatch, tmp_path)
    _complete_coverage_fixture(monkeypatch, fixture)
    if mutation == "missing_contract":
        (fixture.root / execution.COVERAGE_SMOKE_CONTRACT_FILENAME).unlink()
    elif mutation == "missing_ledger":
        (fixture.root / execution.COVERAGE_SMOKE_LEDGER_FILENAME).unlink()
    elif mutation == "tampered_receipt":
        receipt_path = (
            fixture.root
            / fixture.coverage.slots[1].slot_id
            / execution.COVERAGE_SMOKE_PREDECESSOR_RECEIPT_FILENAME
        )
        receipt = json.loads(receipt_path.read_bytes())
        receipt["predecessor_terminal_record_sha256"] = "0" * 64
        receipt_path.write_bytes(execution._canonical_json_bytes(receipt))
    else:
        fixture.rows[3]["unexpected"] = True
        _write_coverage_ledger(fixture)

    assert cli.main(
        [
            "validate-coverage-smoke",
            "--manifest",
            str(V25_MANIFEST),
            "--coverage-smoke-results-root",
            str(fixture.root),
        ]
    ) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert refusal["status"] == "REJECT"
    assert reason_fragment in refusal["reason"]


def test_v25_official_coverage_validation_reports_exact_two_slot_sequence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = _coverage_contract_fixture(monkeypatch, tmp_path)
    _complete_coverage_fixture(monkeypatch, fixture)

    assert cli.main(
        [
            "validate-coverage-smoke",
            "--manifest",
            str(V25_MANIFEST),
            "--coverage-smoke-results-root",
            str(fixture.root),
        ]
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["expected_slot_count"] == 2
    assert result["validated_slot_count"] == 2
    assert result["sequence_integrity_valid"] is True
    assert [
        item["validation"]["slot_id"] for item in result["validations"]
    ] == [
        "slot-066-n31-f5-b05-P",
        "slot-037-n31-f2-b04-00",
    ]


class _CampaignAuthorizationReached(Exception):
    pass


@pytest.mark.parametrize(
    "mutation",
    (None, "missing_ledger", "build_drift"),
)
def test_v25_campaign_reaches_authorization_only_after_exact_completed_coverage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str | None,
) -> None:
    fixture = _coverage_contract_fixture(monkeypatch, tmp_path)
    _complete_coverage_fixture(monkeypatch, fixture)
    if mutation == "missing_ledger":
        (fixture.root / execution.COVERAGE_SMOKE_LEDGER_FILENAME).unlink()
    elif mutation == "build_drift":
        repair_root = fixture.root / fixture.coverage.slots[1].slot_id
        (repair_root / "runtime/exact-build-provenance.json").write_bytes(
            execution._canonical_json_bytes(
                {"schema_version": 1, "revision": "b" * 40}
            )
        )

    campaign_root = fixture.repository / fixture.runtime.results_root
    first = next(
        slot for slot in fixture.plan.slots if slot.execution_ordinal == 1
    )
    campaign_preflight = replace(
        fixture.preflight(0),
        result_root=campaign_root,
        slot_directory=campaign_root / first.slot_id,
    )
    monkeypatch.setattr(cli, "_preflight", lambda *_args, **_kwargs: campaign_preflight)
    monkeypatch.setattr(cli, "_require_validated_smoke", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "validate_slot", _pass_validation)
    monkeypatch.setattr(
        cli,
        "FROZEN_MANIFEST_SHA256",
        hashlib.sha256(fixture.static_artifacts["manifest.json"]).hexdigest(),
    )
    monkeypatch.setattr(
        cli,
        "FROZEN_PLAN_SHA256",
        hashlib.sha256(fixture.static_artifacts["plan.json"]).hexdigest(),
    )
    monkeypatch.setattr(
        cli,
        "FROZEN_COVERAGE_SMOKE_RUNTIME_SHA256",
        hashlib.sha256(fixture.static_artifacts["runtime.json"]).hexdigest(),
    )
    authorization_calls: list[dict[str, object]] = []

    def authorization_reached(*_args, **kwargs):
        authorization_calls.append(kwargs)
        raise _CampaignAuthorizationReached

    monkeypatch.setattr(cli, "_authorization_receipt", authorization_reached)
    arguments = SimpleNamespace(
        manifest=V25_MANIFEST,
        approval_reference="test thesis-author approval",
        authorization_receipt=None,
        approved_utc="2026-08-10T00:00:00+00:00",
    )
    invocation = lambda: cli._run_campaign(
        arguments,
        plan=fixture.plan,
        runtime=fixture.runtime,
        repository=fixture.repository,
        build_directory=fixture.repository / "build-adaptive",
        build_provenance=fixture.repository / "build-adaptive/build-provenance.json",
        campaign_root=campaign_root,
        smoke_root=fixture.repository / "results/shape-placement-factorial-v25-smoke",
        coverage_smoke_root=fixture.root,
        runtime_payload=runtime_module.canonical_runtime_bytes(fixture.runtime),
    )
    if mutation is None:
        with pytest.raises(_CampaignAuthorizationReached):
            invocation()
        assert len(authorization_calls) == 1
        assert authorization_calls[0]["scope"] == "shape25_campaign"
    else:
        with pytest.raises(execution.FactorialExecutionError):
            invocation()
        assert authorization_calls == []
