"""Producer contract for the v42 reporter-retention readiness roll."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from experiments.adaptive import run_shape_factorial_campaign as cli
from experiments.adaptive.kauri_experiment import factorial_execution as execution
from experiments.adaptive.kauri_experiment import factorial_manifest as manifest_module
from experiments.adaptive.kauri_experiment import factorial_runtime as runtime_module

REPOSITORY = Path(__file__).resolve().parents[3]
V42_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v42.json"
)
V41_MANIFEST = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v41.json"
)
V41_SIX = (
    "6f9c5b53c465afd56d467421437dadd238928f7550c4e589a8a18988fd8441d5",
    "23c8be6ccd4059868b0822cfb0dd6269d17b164ed37241ac2e5082312df5e2ed",
    "f5e0cfcfe7e0e57b33358b4c0603acb9547dfc61900d3b47f5c0dbd033ffee08",
    "e52d8641add32d484ffa829b6e7f740fd22388a93930a6f345c2bea9fa466d4c",
    "0d74f500c32d9a210148a75ee8921b2a0d62737c17169bb3faa557848d69429d",
    "e27e4848510b4c92440edebc2a658a38a5d69464adab795219f09482af3154e6",
)
V42_SIX = (
    "c88ad70de33e8f6d6b822f9b6e3dc6b50d51154a901666e542f582826217c48e",
    "617d74fa3b1043924b75b49d6abe75608c58fdcc29387ec2328a6daa527e7e01",
    "e1cb55c74215d6b2fb93f0212d0ce42c699dfc9d4afb927b2986a853d7a48826",
    "4239c82b2d4c67676c30765357a7a27404d15865add450ed798cc7c89ca37ea0",
    "2c699550b483316e7309de6cf2f74262462277fcf113dcbc21ed29d27827a4c9",
    "7ac3945187626a337f2c2866784400ae012b879bbcffd311efe6cb4a30764458",
)
READINESS_CONTRACT = (
    "all_68_factorial_campaign_slots_and_ordered_s066_coverage_cycle1_"
    "authenticated_reporter_readiness_requires_one_outstanding_epoch1_schema_"
    "v2_aggregate_relay_attempt_start_and_reporter_local_commit_timeout_fact_"
    "per_canonical_responsive_degraded_actor_at_the_same_evidence_high_water_"
    "cutoff_before_epoch2_selection_with_final_materialized_actor_marker_arm_"
    "shared_commit_sample_rich_commit_and_observer_join_while_excluded_n7_is_"
    "schema_v1_ungated_and_excluded_repair_s037_retains_v6_one_per_actor_with_"
    "at_least_one_global_aggregate_v1"
)
RETENTION_OPTION = "--cycle-1-responsive-cross-commit-retention-readiness-gate"
ACTORS_OPTION = "--cycle-1-responsive-degraded-actors"
POLICY_OPTION = "--cycle-1-responsive-cross-commit-retention-admission-policy"
SELECTION_OPTION = "--cycle-1-selection-not-before-monotonic-ns"
ELIGIBILITY_OPTION = "--cycle-1-inherited-wait-exempt-eligibility-gate"
REPORTER_V2_CONFIG_LINE = "experiment-responsive-cross-commit-retention-v2 = true"
CAMPAIGN_SCOPE = "factorial_campaign_v1"
S066_SCOPE = "ordered_s066_coverage_v1"
S037_SCOPE = "excluded_repair_s037_v1"
AGGREGATE_POLICY = "aggregate_relay_per_actor_v1"
LEGACY_POLICY = "one_per_actor_with_global_aggregate_v1"


def _canonical(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def _candidate_v42_plan(monkeypatch: pytest.MonkeyPatch):
    payload = V42_MANIFEST.read_bytes()
    semantic_sha256 = hashlib.sha256(_canonical(json.loads(payload))).hexdigest()
    monkeypatch.setattr(manifest_module, "FROZEN_SEMANTIC_SHA256", semantic_sha256)
    manifest = manifest_module.parse_manifest_bytes(payload)
    plan = manifest_module.build_factorial_plan(manifest)
    monkeypatch.setattr(
        manifest_module, "FROZEN_MANIFEST_SHA256", manifest.manifest_sha256
    )
    monkeypatch.setattr(manifest_module, "FROZEN_PLAN_SHA256", plan.plan_sha256)
    return manifest, plan


def _coverage(plan):
    primary = next(slot for slot in plan.slots if slot.execution_ordinal == 1)
    repair = next(slot for slot in plan.slots if slot.execution_ordinal == 5)
    return execution.build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )


def _coverage_binding(version: str) -> execution.CoverageSmokeLaunchBinding:
    return execution.CoverageSmokeLaunchBinding(
        contract_payload=_canonical(
            {
                "manifest_id": f"shape-placement-factorial-{version}",
                "coverage_smoke_id": (
                    f"shape-placement-factorial-{version}-"
                    "excluded-n31-coverage-smoke-v1"
                ),
                "expected_slot_count": 2,
                "execution_schedule": [
                    {
                        "coverage_execution_ordinal": 1,
                        "source_campaign_execution_ordinal": 1,
                        "slot_id": "slot-066-n31-f5-b05-P",
                        "block_id": "n31-f5-b05",
                        "arm_code": "P",
                    },
                    {
                        "coverage_execution_ordinal": 2,
                        "source_campaign_execution_ordinal": 5,
                        "slot_id": "slot-037-n31-f2-b04-00",
                        "block_id": "n31-f2-b04",
                        "arm_code": "00",
                    },
                ],
            }
        ),
        ledger_prefix_payload=b"",
        predecessor_receipt_payload=None,
    )


def _policy(argv: tuple[str, ...]) -> str:
    assert argv.count(POLICY_OPTION) == 1
    return argv[argv.index(POLICY_OPTION) + 1]


def _secrets(replica_count: int) -> runtime_module.ManagerSecretMaterial:
    return runtime_module.ManagerSecretMaterial(
        manager_tls_private_key_der_hex="a1b2",
        manager_tls_certificate_der_hex="c3d4",
        issuer_private_key_hex="11" * 32,
        replica_tls_certificate_der_hex=tuple(
            f"{replica_id + 1:02x}" for replica_id in range(replica_count)
        ),
    )


def _identities(replica_count: int) -> execution.IdentityMaterial:
    return execution.IdentityMaterial(
        bls=tuple(
            {"pub": f"{replica_id + 1:064x}", "sec": f"{replica_id + 101:064x}"}
            for replica_id in range(replica_count)
        ),
        tls=tuple(
            {
                "crt": f"{replica_id + 201:064x}",
                "sec": f"{replica_id + 301:064x}",
                "cid": f"cid-{replica_id}",
            }
            for replica_id in range(replica_count + 1)
        ),
        issuer={"pub": f"{901:064x}", "sec": f"{902:064x}"},
    )


def _materialized_launch(slot, spec, root: Path):
    slot_directory = root / spec.slot_id
    execution._create_slot_directories(slot_directory, spec)
    for label in ("bls", "tls", "issuer"):
        execution._write_exclusive(
            slot_directory / f"runtime/{label}-identities.txt",
            f"{label}-identity-input\n".encode(),
        )
    identities = _identities(spec.replica_count)
    input_artifacts = execution.write_slot_configs(
        slot,
        spec,
        slot_directory=slot_directory,
        identities=identities,
    )
    materialized = execution.materialize_launch(
        slot,
        spec,
        slot_directory=slot_directory,
        binaries=execution.ExecutionBinaries(
            app=root / "bin/hotstuff-app",
            manager=root / "bin/adaptation-manager",
            keygen=root / "bin/hotstuff-keygen",
            tls_keygen=root / "bin/hotstuff-tls-keygen",
        ),
        identities=identities,
        input_artifacts=input_artifacts,
        shared_raw_clock_anchor_ns=7_000_000_000,
        redaction_key=b"r" * 32,
    )
    return materialized, input_artifacts, slot_directory


def test_v42_profile_is_one_lf_and_exact_three_path_delta() -> None:
    v42_payload = V42_MANIFEST.read_bytes()
    v41_payload = V41_MANIFEST.read_bytes()
    assert v42_payload.endswith(b"\n")
    assert not v42_payload.endswith(b"\n\n")
    assert v42_payload.count(b"shape-placement-factorial-v42") == 2
    v42 = json.loads(v42_payload)
    v41 = json.loads(v41_payload)
    assert v42.pop("manifest_id") == "shape-placement-factorial-v42"
    assert v41.pop("manifest_id") == "shape-placement-factorial-v41"
    assert v42["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v42"
    )
    assert v41["artifacts"].pop("results_root") == (  # type: ignore[index]
        "results/shape-placement-factorial-v41"
    )
    v42_responsive = v42["byzantine"]["responsive_degradation"]  # type: ignore[index]
    v41_responsive = v41["byzantine"]["responsive_degradation"]  # type: ignore[index]
    assert (
        v42_responsive.pop("causal_selection_reporter_retention_readiness_gate")
        == READINESS_CONTRACT
    )
    assert v42_responsive["excluded_repair_smoke_observation_contract"] == (
        manifest_module.EXCLUDED_REPAIR_SMOKE_OBSERVATION_CONTRACT_V6
    )
    assert v42 == v41


def test_v41_and_v42_are_historical() -> None:
    assert manifest_module.V41_MANIFEST_ID == "shape-placement-factorial-v41"
    assert (
        manifest_module.V41_MANIFEST_SHA256,
        manifest_module.V41_SEMANTIC_SHA256,
        manifest_module.V41_PLAN_SHA256,
        runtime_module.V41_RUNTIME_SHA256,
        runtime_module.V41_SMOKE_RUNTIME_SHA256,
        runtime_module.V41_COVERAGE_SMOKE_RUNTIME_SHA256,
    ) == V41_SIX
    assert manifest_module.V42_MANIFEST_ID == "shape-placement-factorial-v42"
    assert (
        manifest_module.V42_MANIFEST_SHA256,
        manifest_module.V42_SEMANTIC_SHA256,
        manifest_module.V42_PLAN_SHA256,
        runtime_module.V42_RUNTIME_SHA256,
        runtime_module.V42_SMOKE_RUNTIME_SHA256,
        runtime_module.V42_COVERAGE_SMOKE_RUNTIME_SHA256,
    ) == V42_SIX


def test_v41_six_identities_still_rehash_exactly() -> None:
    payload = V41_MANIFEST.read_bytes()
    manifest = manifest_module.load_frozen_manifest(V41_MANIFEST)
    plan = manifest_module.build_factorial_plan(manifest)
    runtime = runtime_module.build_factorial_runtime(plan)
    smoke = execution.build_n7_ps_smoke_slot(plan.slots[0])
    coverage = _coverage(plan)
    assert (
        hashlib.sha256(payload).hexdigest(),
        hashlib.sha256(_canonical(json.loads(payload))).hexdigest(),
        plan.plan_sha256,
        hashlib.sha256(runtime_module.canonical_runtime_bytes(runtime)).hexdigest(),
        hashlib.sha256(
            execution._canonical_json_bytes(smoke.runtime.as_document())
        ).hexdigest(),
        hashlib.sha256(
            execution._canonical_json_bytes(coverage.runtime.as_document())
        ).hexdigest(),
    ) == V41_SIX


def test_v42_retention_scope_is_exact_and_has_no_gate_leakage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, plan = _candidate_v42_plan(monkeypatch)
    campaign = runtime_module.build_factorial_runtime(plan)
    assert len(campaign.slots) == 68
    for spec in campaign.slots:
        contract = spec.cycle1_responsive_cross_commit_retention
        assert contract is not None
        assert contract.scope == CAMPAIGN_SCOPE
        assert contract.observation_schema_version == 2
        assert contract.responsive_degraded_actor_ids == (
            spec.tiered_cohorts.responsive_degraded_actor_ids  # type: ignore[union-attr]
        )
        assert contract.admission_policy == AGGREGATE_POLICY
        argv = spec.manager_argv_template.argv
        assert argv.count(RETENTION_OPTION) == 1
        assert argv.count(ACTORS_OPTION) == 1
        assert _policy(argv) == AGGREGATE_POLICY
        assert SELECTION_OPTION not in argv
        assert ELIGIBILITY_OPTION not in argv
        assert spec.main_config.lines.count(REPORTER_V2_CONFIG_LINE) == 1

    smoke = execution.build_n7_ps_smoke_slot(plan.slots[0])
    smoke_spec = smoke.runtime
    assert smoke_spec.cycle1_responsive_cross_commit_retention is None
    assert all(
        option not in smoke_spec.manager_argv_template.argv
        for option in (RETENTION_OPTION, ACTORS_OPTION, POLICY_OPTION)
    )
    assert REPORTER_V2_CONFIG_LINE not in smoke_spec.main_config.lines

    coverage = _coverage(plan)
    primary, repair = coverage.runtimes
    assert primary.cycle1_responsive_cross_commit_retention.as_document() == {
        "scope": S066_SCOPE,
        "observation_schema_version": 2,
        "responsive_degraded_actor_ids": (1, 2, 3, 5, 7, 8, 16),
        "admission_policy": AGGREGATE_POLICY,
    }
    assert repair.cycle1_responsive_cross_commit_retention.as_document() == {
        "scope": S037_SCOPE,
        "observation_schema_version": 2,
        "responsive_degraded_actor_ids": (1, 7, 8, 12, 16, 19, 20),
        "admission_policy": LEGACY_POLICY,
    }
    assert _policy(primary.manager_argv_template.argv) == AGGREGATE_POLICY
    assert _policy(repair.manager_argv_template.argv) == LEGACY_POLICY
    assert SELECTION_OPTION not in primary.manager_argv_template.argv
    assert ELIGIBILITY_OPTION not in primary.manager_argv_template.argv
    assert SELECTION_OPTION in repair.manager_argv_template.argv
    assert ELIGIBILITY_OPTION in repair.manager_argv_template.argv
    assert primary.main_config.lines.count(REPORTER_V2_CONFIG_LINE) == 1
    assert repair.main_config.lines.count(REPORTER_V2_CONFIG_LINE) == 1

    for spec in (campaign.slots[0], primary, repair):
        materialized = runtime_module.materialize_manager_argv(
            spec,
            tmp_path / spec.slot_id,
            _secrets(spec.replica_count),
            shared_raw_clock_anchor_ns=7_000_000_000,
        )
        assert _policy(materialized) == (
            spec.cycle1_responsive_cross_commit_retention.admission_policy
        )


def test_v42_receipts_seal_main_config_for_every_gated_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, plan = _candidate_v42_plan(monkeypatch)
    campaign = runtime_module.build_factorial_runtime(plan)
    coverage = _coverage(plan)
    campaign_spec = campaign.slots[0]
    campaign_slot = next(
        slot for slot in plan.slots if slot.slot_id == campaign_spec.slot_id
    )
    scoped = (
        (campaign_slot, campaign_spec),
        (coverage.slots[0], coverage.runtimes[0]),
        (coverage.slots[1], coverage.runtimes[1]),
    )

    for slot, spec in scoped:
        contract = spec.cycle1_responsive_cross_commit_retention
        assert contract is not None
        materialized, input_artifacts, slot_directory = _materialized_launch(
            slot,
            spec,
            tmp_path / contract.scope,
        )
        receipt = execution._launch_receipt(
            spec,
            materialized,
            anchor_ns=7_000_000_000,
            manifest_sha256="a" * 64,
            plan_sha256="b" * 64,
            runtime_sha256="c" * 64,
        )
        main_payload = (slot_directory / "runtime/main.conf").read_bytes()
        expected_sha256 = hashlib.sha256(main_payload).hexdigest()
        main_row = next(
            row
            for row in input_artifacts
            if row["relative_path"] == "runtime/main.conf"
        )
        assert main_row["sha256"] == expected_sha256
        assert receipt["main_config_sha256"] == expected_sha256
        assert receipt["cycle1_responsive_cross_commit_retention"] == (
            contract.as_document()
        )


def test_v42_repair_runner_terminal_passes_strictly_before_hard_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan = _candidate_v42_plan(monkeypatch)
    repair = _coverage(plan).runtimes[1]
    anchor_ns = 7_000_000_000
    terminal_ns = anchor_ns + 650 * 1_000_000_000 - 1
    history: list[dict[str, object]] = [
        {"sequence": 0, "state": "NOT_STARTED", "reason": None}
    ]
    states: list[tuple[str, dict[str, object]]] = []

    outcome, reason, recorded = execution._finalize_runner_terminal(
        repair,
        coverage_binding=_coverage_binding("v42"),
        shared_raw_clock_anchor_ns=anchor_ns,
        runtime_error=None,
        cleanup_error=None,
        raw_now_ns=lambda: terminal_ns,
        history=history,
        state_writer=lambda phase, **fields: states.append((phase, fields)),
    )

    assert recorded == terminal_ns
    assert outcome == "PASS"
    assert reason is None
    assert history[-1] == {"sequence": 1, "state": "PASS", "reason": None}
    assert states[-1][1]["outcome"] == "PASS"


def test_v41_repair_runner_terminal_path_remains_historical() -> None:
    v41 = manifest_module.load_frozen_manifest(V41_MANIFEST)
    v41_plan = manifest_module.build_factorial_plan(v41)
    repair = _coverage(v41_plan).runtimes[1]
    anchor_ns = 7_000_000_000
    execution._assert_v40_repair_runner_terminal_before_hard_deadline(
        repair,
        shared_raw_clock_anchor_ns=anchor_ns,
        runner_terminal_recorded_monotonic_ns=(anchor_ns + 650 * 1_000_000_000 - 1),
    )


def test_v42_repair_terminal_rejects_v41_path_alias_and_missing_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan = _candidate_v42_plan(monkeypatch)
    repair = _coverage(plan).runtimes[1]
    mutations = (
        replace(
            repair,
            result_path=(
                "results/shape-placement-factorial-v41-coverage-smoke/"
                "slot-037-n31-f2-b04-00"
            ),
        ),
        replace(repair, cycle1_responsive_cross_commit_retention=None),
    )
    for candidate in mutations:
        with pytest.raises(
            execution.FactorialExecutionError,
            match="exact v40/v41/v42/v43/v44/v45/v46 repair runtime",
        ):
            execution._assert_v40_repair_runner_terminal_before_hard_deadline(
                candidate,
                shared_raw_clock_anchor_ns=7_000_000_000,
                runner_terminal_recorded_monotonic_ns=8_000_000_000,
            )


def test_v42_retention_rejects_v41_campaign_injection() -> None:
    v41 = manifest_module.load_frozen_manifest(V41_MANIFEST)
    v41_plan = manifest_module.build_factorial_plan(v41)
    slot = v41_plan.slots[0]
    contract = runtime_module.Cycle1ResponsiveCrossCommitRetentionContract(
        scope=CAMPAIGN_SCOPE,
        observation_schema_version=2,
        responsive_degraded_actor_ids=slot.responsive_degraded_actor_ids,
        admission_policy=AGGREGATE_POLICY,
    )

    candidates = (
        slot,
        replace(
            slot,
            result_path=f"results/shape-placement-factorial-v42/{slot.slot_id}",
        ),
    )
    for candidate in candidates:
        with pytest.raises(
            manifest_module.FactorialManifestError,
            match="reporter-retention.*scope",
        ):
            runtime_module.build_slot_runtime(
                candidate,
                cycle1_responsive_cross_commit_retention=contract,
            )


def test_v42_retention_scopes_reject_result_path_and_scope_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan = _candidate_v42_plan(monkeypatch)
    campaign_runtime = runtime_module.build_factorial_runtime(plan)
    campaign_spec = campaign_runtime.slots[0]
    campaign_slot = next(
        slot for slot in plan.slots if slot.slot_id == campaign_spec.slot_id
    )
    coverage = _coverage(plan)
    s066_slot, s066_spec = coverage.slots[0], coverage.runtimes[0]
    campaign_contract = campaign_spec.cycle1_responsive_cross_commit_retention
    s066_contract = s066_spec.cycle1_responsive_cross_commit_retention
    assert campaign_contract is not None
    assert s066_contract is not None

    mutations = (
        (
            replace(
                campaign_slot,
                result_path=campaign_slot.result_path.replace("v42", "v41"),
            ),
            campaign_contract,
        ),
        (
            replace(
                s066_slot,
                result_path=s066_slot.result_path.replace("v42", "v41"),
            ),
            s066_contract,
        ),
        (
            s066_slot,
            replace(
                campaign_contract,
                responsive_degraded_actor_ids=(s066_slot.responsive_degraded_actor_ids),
            ),
        ),
    )
    for slot, contract in mutations:
        with pytest.raises(
            manifest_module.FactorialManifestError,
            match="reporter-retention.*scope",
        ):
            runtime_module.build_slot_runtime(
                slot,
                cycle1_responsive_cross_commit_retention=contract,
            )


def test_v42_is_validation_only_after_v43_rollover(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.DEFAULT_MANIFEST != V42_MANIFEST
    assert cli.main(["--manifest", str(V42_MANIFEST), "plan"]) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert "v1 through v45 are validation-only" in refusal["reason"]
