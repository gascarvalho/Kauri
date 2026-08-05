"""One-shot execution and evidence-boundary tests for SHAPE25."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import signal
from types import SimpleNamespace
from typing import Any

import pytest

from experiments.adaptive.kauri_experiment import factorial_execution as execution
from experiments.adaptive.kauri_experiment.factorial_manifest import (
    build_factorial_plan,
    load_frozen_manifest,
)
from experiments.adaptive.kauri_experiment.factorial_runtime import (
    build_factorial_runtime,
    canonical_runtime_bytes,
)
from experiments.adaptive.kauri_experiment.processes import (
    CleanupOutcome,
    ProcessRecord,
)


REPOSITORY = Path(__file__).resolve().parents[3]
MANIFEST = REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v8.json"


@pytest.fixture(scope="module")
def template_slot():
    return build_factorial_plan(load_frozen_manifest(MANIFEST)).slots[0]


def _make_executable(path: Path, payload: bytes = b"binary") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(0o700)
    return path


def _binaries(root: Path) -> execution.ExecutionBinaries:
    return execution.ExecutionBinaries(
        app=_make_executable(root / "hotstuff-app"),
        manager=_make_executable(root / "adaptation-manager"),
        keygen=_make_executable(root / "hotstuff-keygen"),
        tls_keygen=_make_executable(root / "hotstuff-tls-keygen"),
    )


def _build_provenance(
    root: Path,
    binaries: execution.ExecutionBinaries,
) -> dict[str, object]:
    epoch_profile_digest = _make_executable(root / "epoch-profile-digest")
    binary_paths = {
        **binaries.as_mapping(),
        "epoch_profile_digest": epoch_profile_digest,
    }
    metadata_paths = {
        name: _make_executable(root / "metadata" / name, f"{name}\n".encode())
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

    def row(path: Path) -> dict[str, object]:
        payload = path.read_bytes()
        return {
            "path": str(path.resolve()),
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    return {
        "revision": "c" * 40,
        "binaries": {name: row(path) for name, path in binary_paths.items()},
        "build_metadata": {
            name: row(path) for name, path in metadata_paths.items()
        },
    }


def _identity_material(count: int) -> execution.IdentityMaterial:
    return execution.IdentityMaterial(
        bls=tuple(
            {"pub": f"{replica + 1:064x}", "sec": f"{replica + 101:064x}"}
            for replica in range(count)
        ),
        tls=tuple(
            {
                "crt": f"{replica + 201:064x}",
                "sec": f"{replica + 301:064x}",
                "cid": f"cid-{replica}",
            }
            for replica in range(count + 1)
        ),
        issuer={"pub": f"{901:064x}", "sec": f"{902:064x}"},
    )


def _prepare_slot_directory(root: Path, spec: Any) -> Path:
    directory = root / spec.slot_id
    execution._create_slot_directories(directory, spec)
    for label in ("bls", "tls", "issuer"):
        execution._write_exclusive(
            directory / f"runtime/{label}-identities.txt",
            f"{label}-identity-input\n".encode(),
        )
    return directory


def _smoke_static_artifacts(smoke: execution.N7SmokeSlot) -> dict[str, bytes]:
    plan = build_factorial_plan(load_frozen_manifest(MANIFEST))
    return {
        "manifest.json": MANIFEST.read_bytes(),
        "plan.json": plan.canonical_bytes,
        "runtime.json": execution._canonical_json_bytes(smoke.runtime.as_document()),
    }


def _smoke_authorization(
    smoke: execution.N7SmokeSlot,
    preflight: execution.ExecutionPreflight,
) -> bytes:
    return execution.build_execution_authorization_receipt(
        scope="excluded_n7_smoke",
        approval_reference="test thesis-author approval",
        approved_utc="2026-08-04T00:00:00+00:00",
        kauri_revision=preflight.revision,
        slot_ids=(smoke.slot.slot_id,),
        result_root=Path(smoke.slot.result_path).parent.as_posix(),
        static_artifacts=_smoke_static_artifacts(smoke),
        build_provenance_sha256=hashlib.sha256(
            execution._canonical_json_bytes(preflight.build_provenance)
        ).hexdigest(),
    )


def _campaign_static_artifacts() -> tuple[Any, Any, dict[str, bytes]]:
    manifest = load_frozen_manifest(MANIFEST)
    plan = build_factorial_plan(manifest)
    runtime = build_factorial_runtime(plan)
    slot = plan.slots[0]
    runtime_slot = next(item for item in runtime.slots if item.slot_id == slot.slot_id)
    return (
        slot,
        runtime_slot,
        {
            "manifest.json": MANIFEST.read_bytes(),
            "plan.json": plan.canonical_bytes,
            "runtime.json": canonical_runtime_bytes(runtime),
        },
    )


def _campaign_launch_context(tmp_path: Path, execution_ordinal: int) -> dict[str, Any]:
    manifest = load_frozen_manifest(MANIFEST)
    plan = build_factorial_plan(manifest)
    runtime = build_factorial_runtime(plan)
    spec = next(
        item for item in runtime.slots if item.execution_ordinal == execution_ordinal
    )
    slot = next(item for item in plan.slots if item.slot_id == spec.slot_id)
    static_artifacts = {
        "manifest.json": MANIFEST.read_bytes(),
        "plan.json": plan.canonical_bytes,
        "runtime.json": canonical_runtime_bytes(runtime),
    }
    binaries = _binaries(tmp_path / "bin")
    provenance = _build_provenance(tmp_path / "build-inputs", binaries)
    root = tmp_path / Path(slot.result_path).parent
    preflight = execution.ExecutionPreflight(
        revision="c" * 40,
        repository=tmp_path,
        build_directory=tmp_path / "build-adaptive",
        result_root=root,
        slot_directory=root / slot.slot_id,
        free_bytes=runtime.minimum_free_bytes + 1,
        binaries=binaries,
        build_provenance=provenance,
    )
    authorization_payload = execution.build_execution_authorization_receipt(
        scope="shape25_campaign",
        approval_reference="test thesis-author approval",
        approved_utc="2026-08-04T00:00:00+00:00",
        kauri_revision=preflight.revision,
        slot_ids=tuple(item.slot_id for item in plan.slots),
        result_root=Path(slot.result_path).parent.as_posix(),
        static_artifacts=static_artifacts,
        build_provenance_sha256=hashlib.sha256(
            execution._canonical_json_bytes(provenance)
        ).hexdigest(),
    )
    authorization = json.loads(authorization_payload)
    contract = execution.build_campaign_execution_contract(
        runtime=runtime,
        static_artifacts=static_artifacts,
        authorization=authorization,
        authorization_payload=authorization_payload,
        build_provenance=provenance,
    )
    contract_payload = execution._canonical_json_bytes(contract)
    execution.preserve_build_evidence(
        root,
        provenance,
        initial_files={
            execution.CAMPAIGN_AUTHORIZATION_FILENAME: authorization_payload,
            execution.CAMPAIGN_CONTRACT_FILENAME: contract_payload,
        },
    )
    return {
        "plan": plan,
        "runtime": runtime,
        "slot": slot,
        "spec": spec,
        "static_artifacts": static_artifacts,
        "preflight": preflight,
        "authorization_payload": authorization_payload,
        "authorization": authorization,
        "contract_payload": contract_payload,
    }


def _campaign_ledger_row(
    context: dict[str, Any],
    execution_ordinal: int,
    state: str,
    monotonic_ns: int,
) -> dict[str, object]:
    runtime = context["runtime"]
    spec = next(
        item for item in runtime.slots if item.execution_ordinal == execution_ordinal
    )
    root = context["preflight"].result_root
    authorization = context["authorization"]
    common: dict[str, object] = {
        "schema_version": 1,
        "campaign_id": runtime.runtime_id,
        "manifest_sha256": hashlib.sha256(
            context["static_artifacts"]["manifest.json"]
        ).hexdigest(),
        "source_plan_sha256": hashlib.sha256(
            context["static_artifacts"]["plan.json"]
        ).hexdigest(),
        "runtime_sha256": hashlib.sha256(
            context["static_artifacts"]["runtime.json"]
        ).hexdigest(),
        "contract_sha256": hashlib.sha256(context["contract_payload"]).hexdigest(),
        "authorization_id": authorization["authorization_id"],
        "authorization_sha256": hashlib.sha256(
            context["authorization_payload"]
        ).hexdigest(),
        "kauri_revision": context["preflight"].revision,
        "execution_ordinal": execution_ordinal,
        "slot_id": spec.slot_id,
        "block_id": spec.block_id,
        "arm_code": spec.arm_code,
        "attempt_ordinal": 1,
        "automatic_retries": 0,
        "replacement_policy": "none",
        "state": state,
        "recorded_utc": "2026-08-04T00:00:00+00:00",
        "recorded_monotonic_ns": monotonic_ns,
        "slot_directory": str(root / spec.slot_id),
    }
    if state == "STARTED":
        return {
            **common,
            "preflight_revision": context["preflight"].revision,
            "preflight_free_bytes": context["preflight"].free_bytes,
            "build_provenance_sha256": hashlib.sha256(
                execution._canonical_json_bytes(
                    context["preflight"].build_provenance
                )
            ).hexdigest(),
        }
    return {
        **common,
        "execution_outcome": "PASS",
        "execution_reason": None,
        "launch_count": spec.replica_count + 1,
        "validation": {
            "outcome": "PASS",
            "reason": None,
            "integrity_valid": True,
            "campaign_member": True,
            "figure_eligible": True,
        },
    }


def test_n7_smoke_is_excluded_ps_fanout_two_with_k_two(template_slot) -> None:
    smoke = execution.build_n7_ps_smoke_slot(template_slot)
    runtime_payload = execution._canonical_json_bytes(smoke.runtime.as_document())

    assert smoke.slot.replica_count == 7
    assert smoke.slot.f == 2
    assert smoke.slot.q == 5
    assert smoke.slot.initial_fanout == 2
    assert smoke.slot.arm_code == "PS"
    assert smoke.slot.placement_adaptation is True
    assert smoke.slot.shape_adaptation is True
    assert len(smoke.slot.byzantine_actor_ids) == 1
    assert len(smoke.slot.responsive_degraded_actor_ids) == 1
    assert len(smoke.slot.fast_replica_ids) == smoke.slot.q == 5
    assert 0 in smoke.slot.fast_replica_ids
    assert smoke.slot.byzantine.max_omissions_per_proposal is None
    assert smoke.slot.max_omissions_per_proposal == smoke.slot.f == 2
    assert smoke.slot.maximum_omissions_per_proposal == smoke.slot.f == 2
    assert all(actor >= smoke.slot.q for actor in smoke.slot.byzantine_actor_ids)
    assert all(
        1 <= actor < smoke.slot.q
        for actor in smoke.slot.responsive_degraded_actor_ids
    )
    assert set(smoke.slot.byzantine_actor_ids).isdisjoint(
        smoke.slot.responsive_degraded_actor_ids
    )
    assert smoke.runtime.actor_ids == smoke.slot.byzantine_actor_ids
    assert smoke.runtime.tiered_cohorts is not None
    assert smoke.runtime.tiered_cohorts.responsive_degraded_actor_ids == (
        smoke.slot.responsive_degraded_actor_ids
    )
    assert smoke.actor_count_rule == "fixed_1_hard_actor_smoke_only"
    assert runtime_payload == execution._canonical_json_bytes(
        execution.build_n7_ps_smoke_slot(template_slot).runtime.as_document()
    )
    for process in smoke.runtime.replica_argv_templates:
        argv = process.argv
        option = "--experiment-byzantine-max-omissions-per-proposal"
        assert argv[argv.index(option) + 1] == "2"
        option = "--experiment-responsive-degraded-omission-actors"
        assert argv[argv.index(option) + 1] == ",".join(
            map(str, smoke.slot.responsive_degraded_actor_ids)
        )
        option = "--experiment-responsive-omission-period"
        assert argv[argv.index(option) + 1] == "32"
    manager = smoke.runtime.manager_argv_template.argv
    assert manager[manager.index("--required-nonresponsive") + 1] == "1"
    assert smoke.campaign_member is False
    assert smoke.figure_eligible is False
    assert smoke.denominator_contribution == 0


def test_static_artifacts_bind_exact_campaign_and_direct_smoke_documents(
    template_slot,
) -> None:
    campaign_slot, campaign_runtime, campaign_artifacts = (
        _campaign_static_artifacts()
    )
    execution._bind_static_artifacts(
        campaign_slot,
        campaign_runtime,
        campaign_artifacts,
        campaign_member=True,
    )

    smoke = execution.build_n7_ps_smoke_slot(template_slot)
    execution._bind_static_artifacts(
        smoke.slot,
        smoke.runtime,
        _smoke_static_artifacts(smoke),
        campaign_member=False,
    )


@pytest.mark.parametrize("artifact", ("manifest.json", "plan.json", "runtime.json"))
def test_static_artifacts_reject_empty_or_byte_drifted_documents(
    artifact: str,
    template_slot,
) -> None:
    smoke = execution.build_n7_ps_smoke_slot(template_slot)
    artifacts = _smoke_static_artifacts(smoke)
    artifacts[artifact] = b"{}\n"

    with pytest.raises(execution.FactorialExecutionError, match=artifact):
        execution._bind_static_artifacts(
            smoke.slot,
            smoke.runtime,
            artifacts,
            campaign_member=False,
        )


def test_static_artifacts_reject_supplied_slot_runtime_identity_drift(
    template_slot,
) -> None:
    smoke = execution.build_n7_ps_smoke_slot(template_slot)
    drifted = replace(
        smoke.runtime,
        scientific_seed=smoke.runtime.scientific_seed + 1,
    )

    with pytest.raises(execution.FactorialExecutionError, match="slot/runtime"):
        execution._bind_static_artifacts(
            smoke.slot,
            drifted,
            _smoke_static_artifacts(smoke),
            campaign_member=False,
        )


def test_preflight_rejects_collisions_before_any_write(tmp_path: Path, template_slot) -> None:
    repository = tmp_path / "Kauri"
    build = repository / "build-adaptive"
    binary_paths = {
        "app": _make_executable(build / "examples/hotstuff-app"),
        "manager": _make_executable(build / "examples/adaptation-manager"),
        "keygen": _make_executable(build / "hotstuff-keygen"),
        "tls_keygen": _make_executable(build / "hotstuff-tls-keygen"),
        "epoch_profile_digest": _make_executable(
            build / "examples/epoch-profile-digest"
        ),
    }
    result_root = repository / "results"
    result_root.mkdir(parents=True)
    collision = result_root / template_slot.slot_id
    collision.mkdir()

    with pytest.raises(execution.FactorialExecutionError, match="collision"):
        execution.verify_evidence_preflight(
            template_slot,
            repository=repository,
            build_directory=build,
            build_provenance_path=build / "provenance.json",
            result_root=result_root,
            minimum_free_bytes=1,
            verify_repository=lambda _repository: "a" * 40,
            verify_build=lambda **_kwargs: {"revision": "a" * 40},
            occupied_ports=lambda _ports: (),
            disk_usage=lambda _path: SimpleNamespace(free=10_000),
        )

    assert collision.exists()
    assert set(binary_paths) == {
        "app",
        "manager",
        "keygen",
        "tls_keygen",
        "epoch_profile_digest",
    }


def test_preflight_is_bound_to_clean_pushed_revision_and_all_ports(
    tmp_path: Path, template_slot
) -> None:
    repository = tmp_path / "Kauri"
    build = repository / "build-adaptive"
    for relative in (
        "examples/hotstuff-app",
        "examples/adaptation-manager",
        "hotstuff-keygen",
        "hotstuff-tls-keygen",
        "examples/epoch-profile-digest",
    ):
        _make_executable(build / relative)
    result_root = repository / "results"
    result_root.mkdir(parents=True)
    calls: dict[str, Any] = {}

    def verify_repository(path: Path) -> str:
        calls["repository"] = path
        return "b" * 40

    def verify_build(**kwargs: Any) -> dict[str, object]:
        calls["build"] = kwargs
        return {"revision": "b" * 40}

    def occupied(ports: Any) -> tuple[int, ...]:
        calls["ports"] = tuple(ports)
        return ()

    result = execution.verify_evidence_preflight(
        template_slot,
        repository=repository,
        build_directory=build,
        build_provenance_path=build / "provenance.json",
        result_root=result_root,
        minimum_free_bytes=9_999,
        verify_repository=verify_repository,
        verify_build=verify_build,
        occupied_ports=occupied,
        disk_usage=lambda _path: SimpleNamespace(free=10_000),
    )

    assert result.revision == "b" * 40
    assert calls["repository"] == repository.resolve()
    assert calls["build"]["repository"] == repository.resolve()
    assert calls["ports"] == execution.slot_ports(template_slot)
    assert not result.slot_directory.exists()


def test_materialization_writes_complete_main_and_replica_configs_before_argv_binding(
    tmp_path: Path, template_slot, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = execution.build_slot_runtime(template_slot)
    slot_directory = _prepare_slot_directory(tmp_path, spec)
    identities = _identity_material(spec.replica_count)
    binaries = _binaries(tmp_path / "bin")
    original_manager = execution.materialize_manager_argv
    original_replicas = execution.materialize_replica_argv
    checked: list[str] = []
    input_artifacts = execution.write_slot_configs(
        template_slot,
        spec,
        slot_directory=slot_directory,
        identities=identities,
    )

    def manager_wrapper(*args: Any, **kwargs: Any) -> Any:
        assert (slot_directory / "runtime/main.conf").is_file()
        assert all(
            (slot_directory / f"runtime/replica-{replica}.conf").is_file()
            for replica in range(spec.replica_count)
        )
        checked.append("manager")
        return original_manager(*args, **kwargs)

    def replica_wrapper(*args: Any, **kwargs: Any) -> Any:
        assert (slot_directory / "runtime/main.conf").is_file()
        checked.append("replicas")
        return original_replicas(*args, **kwargs)

    monkeypatch.setattr(execution, "materialize_manager_argv", manager_wrapper)
    monkeypatch.setattr(execution, "materialize_replica_argv", replica_wrapper)
    materialized = execution.materialize_launch(
        template_slot,
        spec,
        slot_directory=slot_directory,
        binaries=binaries,
        identities=identities,
        input_artifacts=input_artifacts,
        shared_raw_clock_anchor_ns=1_000_000_000,
        redaction_key=b"r" * 32,
    )

    assert checked == ["manager", "replicas"]
    main = (slot_directory / "runtime/main.conf").read_text()
    exact_lines = {
        "nworker = 2",
        "repnworker = 2",
        f"stat-period = {spec.fault_window.hard_timeout_s + 60}",
        "pace-maker = dummy",
        "proposer = 0",
        f"block-size = {template_slot.workload.block_size}",
        f"fan-out = {template_slot.initial_fanout}",
        f"piped_latency = {template_slot.workload.piped_latency_ms}",
        f"async_blocks = {template_slot.pipeline_stretch}",
        "base-timeout = 2.0",
        "prop-delay = 0.1",
        f"aggregation-timeout = {template_slot.common_timers.aggregation_timeout_ms / 1000:g}",
        f"leader-progress-timeout = {template_slot.common_timers.leader_progress_timeout_ms / 1000:g}",
        f"leader-activation-grace = {template_slot.common_timers.leader_activation_grace_ms / 1000:g}",
        "client-ip = 127.0.0.1",
        "tree-generation = default",
        f"tree-switch-period = {template_slot.workload.tree_switch_period_blocks}",
        "epoch-protocol-mode = adaptive_v2",
        "epoch-change-issuer-id = 1",
        f"epoch-change-issuer-public-key = {identities.issuer['pub']}",
        f"epoch-change-minimum-activation-delay = {template_slot.common_timers.activation_delay_blocks}",
        f"epoch-change-maximum-activation-delay = {template_slot.common_timers.activation_delay_blocks}",
        "epoch-change-maximum-block-extra-bytes = 4096",
        "epoch-change-maximum-ancestry-blocks = 128",
        f"epoch-manager-address = 127.0.0.1:{template_slot.ports.manager}",
        f"epoch-manager-tls-cert = {identities.tls[spec.replica_count]['crt']}",
        f"max-rep-msg = {4 << 20}",
    }
    assert exact_lines.issubset(set(main.splitlines()))
    replica_lines = [line for line in main.splitlines() if line.startswith("replica = ")]
    assert len(replica_lines) == spec.replica_count
    for replica_id, line in enumerate(replica_lines):
        assert (
            line
            == "replica = "
            f"127.0.0.1:{template_slot.ports.peer_base + replica_id};"
            f"{template_slot.ports.client_base + replica_id}, "
            f"{identities.bls[replica_id]['pub']}, {identities.tls[replica_id]['cid']}"
        )
        private = (slot_directory / f"runtime/replica-{replica_id}.conf").read_text()
        assert private == (
            f"privkey = {identities.bls[replica_id]['sec']}\n"
            f"tls-privkey = {identities.tls[replica_id]['sec']}\n"
            f"tls-cert = {identities.tls[replica_id]['crt']}\n"
            f"idx = {replica_id}\n"
        )
        assert (slot_directory / f"runtime/replica-{replica_id}.conf").stat().st_mode & 0o777 == 0o600
    assert materialized.manager_argv[0] == str(binaries.manager)
    assert materialized.redacted_manager_argv[0] == "adaptation-manager"
    assert all(process.argv[0] == str(binaries.app) for process in materialized.replica_argv)
    assert all(
        process.argv[0] == "hotstuff-app" for process in materialized.redacted_replica_argv
    )


def test_receipt_redacts_every_manager_secret(template_slot, tmp_path: Path) -> None:
    spec = execution.build_slot_runtime(template_slot)
    slot_directory = _prepare_slot_directory(tmp_path, spec)
    identities = _identity_material(spec.replica_count)
    input_artifacts = execution.write_slot_configs(
        template_slot,
        spec,
        slot_directory=slot_directory,
        identities=identities,
    )
    materialized = execution.materialize_launch(
        template_slot,
        spec,
        slot_directory=slot_directory,
        binaries=_binaries(tmp_path / "bin"),
        identities=identities,
        input_artifacts=input_artifacts,
        shared_raw_clock_anchor_ns=1_000,
        redaction_key=b"s" * 32,
    )
    receipt = execution._launch_receipt(
        spec,
        materialized,
        anchor_ns=1_000,
        manifest_sha256="1" * 64,
        plan_sha256="2" * 64,
        runtime_sha256="3" * 64,
    )
    payload = json.dumps(receipt, sort_keys=True)

    for row in identities.tls:
        assert row["sec"] not in payload
        assert row["crt"] not in payload
    assert identities.issuer["sec"] not in payload
    assert payload.count(f"hmac-sha256:{materialized.redaction_key_id}:") == (
        spec.replica_count + 3
    )
    assert set(receipt) == {
        "schema_version",
        "slot_id",
        "runtime_artifact_id",
        "manifest_sha256",
        "plan_sha256",
        "runtime_sha256",
        "execution_ordinal",
        "attempt_ordinal",
        "retry_of",
        "replacement_for",
        "shared_raw_clock_anchor_ns",
        "fault_window_start_ns",
        "fault_window_end_ns",
        "redaction_key_id",
        "manager_argv",
        "replica_argv",
    }


def test_observer_baseline_timeout_covers_remaining_prefault_delay(
    tmp_path: Path,
    template_slot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = execution.build_slot_runtime(template_slot)
    anchor_ns = 1_000_000_000
    now_ns = anchor_ns + 2 * execution.NANOSECONDS_PER_SECOND
    observed_timeouts: list[tuple[str, float]] = []

    class StopAfterBaselineTimeout(RuntimeError):
        pass

    def wait_until(
        description: str,
        _predicate: Any,
        *,
        phase_timeout_s: float,
        **_kwargs: Any,
    ) -> object:
        observed_timeouts.append((description, phase_timeout_s))
        if len(observed_timeouts) == 1:
            return now_ns
        raise StopAfterBaselineTimeout

    monkeypatch.setattr(execution, "_wait_until", wait_until)
    with pytest.raises(StopAfterBaselineTimeout):
        execution.observe_slot_phases(
            spec,
            tmp_path,
            (),
            shared_raw_clock_anchor_ns=anchor_ns,
            hard_deadline_ns=anchor_ns
            + spec.fault_window.hard_timeout_s
            * execution.NANOSECONDS_PER_SECOND,
            raw_now_ns=lambda: now_ns,
            sleep=lambda _seconds: None,
        )

    baseline_description, baseline_timeout_s = observed_timeouts[1]
    assert baseline_description.startswith("fixed pre-fault baseline")
    assert baseline_timeout_s == (
        spec.fault_window.start_after_prelaunch_anchor_s
        - 2
        + spec.fault_window.schedule_slack_s
    )


def test_observer_does_not_apply_convergence_deadline_before_manager_selection(
    tmp_path: Path,
    template_slot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = execution.build_slot_runtime(template_slot)
    anchor_ns = execution.NANOSECONDS_PER_SECOND
    hard_deadline_ns = anchor_ns + (
        spec.fault_window.hard_timeout_s * execution.NANOSECONDS_PER_SECOND
    )
    now_ns = anchor_ns + 2 * execution.NANOSECONDS_PER_SECOND
    transition_timeout_s: float | None = None

    class StopAtTransition(RuntimeError):
        pass

    baseline_event = execution._Event(
        source=spec.structured_events.commit_observer_id,
        relative_path=spec.structured_events.replica_output_relative_paths[0],
        line_number=1,
        value={
            "source_sequence": 1,
            "source_monotonic_ns": anchor_ns
            + (spec.fault_window.start_after_prelaunch_anchor_s - 1)
            * execution.NANOSECONDS_PER_SECOND,
            "event_type": "block.committed",
            "payload": {},
        },
        line_sha256="11" * 32,
    )
    baseline_common = {
        "identity": {
            "decision_proof": {
                "epoch_number": 0,
                "epoch_digest": "22" * 32,
            }
        }
    }

    def wait_until(
        description: str,
        _predicate: Any,
        *,
        phase_timeout_s: float,
        **_kwargs: Any,
    ) -> object:
        nonlocal now_ns, transition_timeout_s
        if description.startswith("all "):
            return anchor_ns + execution.NANOSECONDS_PER_SECOND
        if description.startswith("fixed pre-fault baseline"):
            return baseline_event, baseline_common
        if description.startswith("fixed fault-evidence window"):
            now_ns = anchor_ns + 255 * execution.NANOSECONDS_PER_SECOND
            return True
        transition_timeout_s = phase_timeout_s
        raise StopAtTransition

    monkeypatch.setattr(execution, "_wait_until", wait_until)
    with pytest.raises(StopAtTransition):
        execution.observe_slot_phases(
            spec,
            tmp_path,
            (),
            shared_raw_clock_anchor_ns=anchor_ns,
            hard_deadline_ns=hard_deadline_ns,
            raw_now_ns=lambda: now_ns,
            sleep=lambda _seconds: None,
        )

    assert transition_timeout_s == 245
    assert transition_timeout_s > (
        spec.fault_window.transition_convergence_deadline_s
        + spec.fault_window.schedule_slack_s
    )


def test_observer_caps_post_selection_barrier_at_convergence_plus_slack(
    tmp_path: Path,
    template_slot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = execution.build_slot_runtime(template_slot)
    anchor_ns = execution.NANOSECONDS_PER_SECOND
    now_ns = anchor_ns + 255 * execution.NANOSECONDS_PER_SECOND
    observed_timeout_s: float | None = None
    baseline_event = execution._Event(
        source=spec.structured_events.commit_observer_id,
        relative_path=spec.structured_events.replica_output_relative_paths[0],
        line_number=1,
        value={
            "source_sequence": 1,
            "source_monotonic_ns": anchor_ns
            + (spec.fault_window.start_after_prelaunch_anchor_s - 1)
            * execution.NANOSECONDS_PER_SECOND,
            "event_type": "block.committed",
            "payload": {},
        },
        line_sha256="11" * 32,
    )
    shape_event = execution._Event(
        source="adaptive-manager",
        relative_path=spec.structured_events.manager_output_relative_path,
        line_number=1,
        value={
            "source_sequence": 1,
            "source_monotonic_ns": now_ns,
            "event_type": "adaptive_v2_shape_decision",
            "payload": {"cycle_ordinal": 0},
        },
        line_sha256="33" * 32,
    )

    class StopAfterSelection(RuntimeError):
        pass

    def wait_until(
        description: str,
        _predicate: Any,
        *,
        phase_timeout_s: float,
        **_kwargs: Any,
    ) -> object:
        nonlocal observed_timeout_s
        if description.startswith("all "):
            return anchor_ns + execution.NANOSECONDS_PER_SECOND
        if description.startswith("fixed pre-fault baseline"):
            return baseline_event, {
                "identity": {
                    "decision_proof": {
                        "epoch_number": 0,
                        "epoch_digest": "22" * 32,
                    }
                }
            }
        if description.startswith("fixed fault-evidence window"):
            return True
        if description.startswith("manager shape selection for epoch-1"):
            return shape_event
        observed_timeout_s = phase_timeout_s
        raise StopAfterSelection

    monkeypatch.setattr(execution, "_wait_until", wait_until)
    with pytest.raises(StopAfterSelection):
        execution.observe_slot_phases(
            spec,
            tmp_path,
            (),
            shared_raw_clock_anchor_ns=anchor_ns,
            hard_deadline_ns=anchor_ns
            + spec.fault_window.hard_timeout_s
            * execution.NANOSECONDS_PER_SECOND,
            raw_now_ns=lambda: now_ns,
            sleep=lambda _seconds: None,
        )

    assert observed_timeout_s == (
        spec.fault_window.transition_convergence_deadline_s
        + spec.fault_window.schedule_slack_s
    )


@dataclass
class _FakeProcess:
    pid: int
    returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            raise TimeoutError
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


class _FakeRegistry:
    def __init__(self, **_kwargs: Any) -> None:
        self.records: list[ProcessRecord] = []
        self.cleanup_calls = 0

    def register(self, *, name: str, replica_id: int, process: _FakeProcess) -> ProcessRecord:
        record = ProcessRecord(name, replica_id, process.pid, process.pid, process)
        self.records.append(record)
        return record

    def cleanup(self, *, timeout_s: float) -> tuple[CleanupOutcome, ...]:
        del timeout_s
        self.cleanup_calls += 1
        outcomes: list[CleanupOutcome] = []
        for record in self.records:
            if record.process.poll() is None:
                record.process.returncode = 0
                outcomes.append(
                    CleanupOutcome(
                        record.name,
                        record.replica_id,
                        record.pid,
                        record.pgid,
                        2,
                        0,
                    )
                )
        return tuple(outcomes)


def _identity_command(command: Any, **_kwargs: Any) -> SimpleNamespace:
    binary = Path(command[0])
    assert binary.parent.name == "binaries"
    assert binary.parent.parent.name == execution.BUILD_EVIDENCE_DIRECTORY
    count = int(command[command.index("--num") + 1])
    if binary.name == "tls_keygen":
        rows = [
            f"crt:{value + 201:064x} sec:{value + 301:064x} cid:cid-{value}"
            for value in range(count)
        ]
    elif binary.name == "keygen" and command[command.index("--algo") + 1] == "bls":
        rows = [
            f"pub:{value + 1:064x} sec:{value + 101:064x}" for value in range(count)
        ]
    else:
        rows = [f"pub:{901:064x} sec:{902:064x}"]
    return SimpleNamespace(returncode=0, stdout="\n".join(rows) + "\n", stderr="")


def _fake_phases(spec: Any, *_args: Any, **kwargs: Any) -> dict[str, object]:
    width = spec.cutoff_contract.bucket_width_s * 1_000_000_000
    counts = (
        spec.cutoff_contract.baseline_bucket_count,
        spec.cutoff_contract.fault_evidence_bucket_count,
        spec.cutoff_contract.epoch1_stable_bucket_count,
        spec.cutoff_contract.epoch2_stable_bucket_count,
    )
    anchor = int(kwargs.get("shared_raw_clock_anchor_ns", 1_000))
    starts = tuple(
        anchor + offset * 1_000_000_000 for offset in (1, 40, 80, 120)
    )
    epoch2_end = starts[3] + counts[3] * width
    drain_ns = epoch2_end + 1_000_000_000
    configurations = (
        (0, "11" * 32),
        (0, "11" * 32),
        (1, "22" * 32),
        (2, "33" * 32),
    )
    phases = [
        {
            "phase": name,
            "start_monotonic_ns": start,
            "end_monotonic_ns": start + count * width,
            "bucket_count": count,
            "configuration": {
                "epoch_number": configuration[0],
                "epoch_digest": configuration[1],
            },
        }
        for name, start, count, configuration in zip(
            ("baseline", "fault_evidence", "epoch1_stable", "epoch2_stable"),
            starts,
            counts,
            configurations,
        )
    ]

    def qualification(phase: str, height: int) -> dict[str, object]:
        window = next(item for item in phases if item["phase"] == phase)
        configuration = window["configuration"]
        timestamp = int(window["start_monotonic_ns"]) + 100

        def reference(
            replica_id: int,
            event_type: str,
            sequence: int,
        ) -> dict[str, object]:
            return {
                "relative_path": (
                    spec.structured_events.replica_output_relative_paths[replica_id]
                ),
                "line_number": sequence,
                "source_id": f"replica-{replica_id}",
                "source_sequence": sequence,
                "source_monotonic_ns": timestamp,
                "event_type": event_type,
                "line_sha256": f"{replica_id + sequence:064x}",
            }

        observer_id = int(
            spec.structured_events.commit_observer_id.removeprefix("replica-")
        )
        return {
            "phase": phase,
            "common_commit": {
                "identity": {
                    "block_height": height,
                    "block_hash": f"{height:064x}",
                    "parent_hash": f"{height - 1:064x}",
                    "transaction_count": 1,
                    "decision_proof": {
                        "epoch_number": configuration["epoch_number"],
                        "tree_id": 0,
                        "epoch_digest": configuration["epoch_digest"],
                        "block_hash": f"{height:064x}",
                    },
                },
                "observer": reference(
                    observer_id,
                    "block.committed",
                    height * 10 + 1,
                ),
                "witnesses": [
                    reference(
                        replica_id,
                        "block.commit_observed",
                        height * 10 + 2,
                    )
                    for replica_id in range(spec.q)
                ],
                "common_monotonic_ns": timestamp,
            },
        }

    return {
        "schema_version": 1,
        "slot_id": spec.slot_id,
        "cutoff_rule": spec.cutoff_contract.actual_cutoff_validation_rule,
        "cutoffs": [
            {
                "name": "epoch2_drain_complete",
                "source_monotonic_ns": drain_ns,
            }
        ],
        "phases": phases,
        "phase_qualifications": [
            qualification("baseline", 1),
            qualification("epoch1_stable", 2),
            qualification("epoch2_stable", 3),
        ],
    }


def _event_line(
    spec: Any,
    *,
    source: str,
    instance: str,
    sequence: int,
    timestamp_ns: int,
    event_type: str,
    payload: dict[str, object],
) -> bytes:
    return execution._canonical_json_bytes(
        {
            "event_schema_version": 1,
            "run_id": spec.slot_id,
            "source_kind": (
                "adaptation_manager" if source == "adaptive-manager" else "replica"
            ),
            "source_id": source,
            "source_instance": instance,
            "source_sequence": sequence,
            "source_monotonic_ns": timestamp_ns,
            "event_type": event_type,
            "payload": payload,
        }
    )


def _event(
    spec: Any,
    *,
    replica_id: int,
    sequence: int,
    timestamp_ns: int,
    event_type: str,
    payload: dict[str, object],
) -> execution._Event:
    source = f"replica-{replica_id}"
    relative_path = spec.structured_events.replica_output_relative_paths[replica_id]
    encoded = _event_line(
        spec,
        source=source,
        instance=spec.structured_events.replica_source_instances[replica_id],
        sequence=sequence,
        timestamp_ns=timestamp_ns,
        event_type=event_type,
        payload=payload,
    )
    return execution._Event(
        source=source,
        relative_path=relative_path,
        line_number=sequence,
        value=json.loads(encoded),
        line_sha256=hashlib.sha256(encoded).hexdigest(),
    )


def test_epoch_activation_identity_uses_exact_native_flat_payload(
    template_slot,
) -> None:
    spec = execution.build_n7_ps_smoke_slot(template_slot).runtime
    activation = _event(
        spec,
        replica_id=0,
        sequence=1,
        timestamp_ns=56_459_065,
        event_type="epoch.activated",
        payload={
            "epoch_number": 1,
            "tree_id": 0,
            "epoch_digest": "22" * 32,
            "activation_height": 2_950,
        },
    )

    assert execution._epoch_activation_identity(activation) == (
        1,
        0,
        "22" * 32,
        2_950,
    )


@pytest.mark.parametrize(
    "payload",
    (
        {
            "configuration": {
                "epoch_number": 1,
                "tree_id": 0,
                "epoch_digest": "22" * 32,
            },
            "activation_height": 2_950,
        },
        {
            "epoch_number": 1,
            "tree_id": 0,
            "epoch_digest": "22" * 32,
            "activation_height": 2_950,
            "configuration": {},
        },
    ),
)
def test_epoch_activation_identity_rejects_noncanonical_payloads(
    template_slot,
    payload: dict[str, object],
) -> None:
    spec = execution.build_n7_ps_smoke_slot(template_slot).runtime
    activation = _event(
        spec,
        replica_id=0,
        sequence=1,
        timestamp_ns=56_459_065,
        event_type="epoch.activated",
        payload=payload,
    )

    with pytest.raises(
        execution.FactorialExecutionError,
        match="epoch activation payload is malformed",
    ):
        execution._epoch_activation_identity(activation)


def test_replica_activation_barrier_accepts_real_flat_streams(
    template_slot,
) -> None:
    spec = execution.build_n7_ps_smoke_slot(template_slot).runtime
    payload = {
        "epoch_number": 1,
        "tree_id": 0,
        "epoch_digest": "22" * 32,
        "activation_height": 2_950,
    }
    streams = {
        f"replica-{replica_id}": (
            _event(
                spec,
                replica_id=replica_id,
                sequence=1,
                timestamp_ns=56_459_065 + replica_id,
                event_type="epoch.activated",
                payload=payload,
            ),
        )
        for replica_id in range(spec.replica_count)
    }

    barrier = execution._replica_transition_barrier(
        streams,
        replica_count=spec.replica_count,
        event_type="epoch.activated",
        epoch=1,
    )

    assert barrier is not None
    assert barrier.source == f"replica-{spec.replica_count - 1}"


def test_replica_activation_barrier_rejects_disagreeing_tree_identity(
    template_slot,
) -> None:
    spec = execution.build_n7_ps_smoke_slot(template_slot).runtime
    streams: dict[str, tuple[execution._Event, ...]] = {}
    for replica_id in range(spec.replica_count):
        streams[f"replica-{replica_id}"] = (
            _event(
                spec,
                replica_id=replica_id,
                sequence=1,
                timestamp_ns=56_459_065 + replica_id,
                event_type="epoch.activated",
                payload={
                    "epoch_number": 1,
                    "tree_id": 1 if replica_id == spec.replica_count - 1 else 0,
                    "epoch_digest": "22" * 32,
                    "activation_height": 2_950,
                },
            ),
        )

    with pytest.raises(
        execution.FactorialExecutionError,
        match="replicas disagree on epoch.activated",
    ):
        execution._replica_transition_barrier(
            streams,
            replica_count=spec.replica_count,
            event_type="epoch.activated",
            epoch=1,
        )


def test_common_commit_proof_preserves_observer_and_exact_q_witnesses(
    template_slot,
) -> None:
    spec = execution.build_n7_ps_smoke_slot(template_slot).runtime
    identity = {
        "block_height": 17,
        "block_hash": "a" * 64,
        "parent_hash": "b" * 64,
        "transaction_count": 23,
    }
    streams: dict[str, list[execution._Event]] = {
        f"replica-{replica_id}": [
            _event(
                spec,
                replica_id=replica_id,
                sequence=1,
                timestamp_ns=100 + replica_id,
                event_type="block.commit_observed",
                payload=identity,
            )
        ]
        for replica_id in range(spec.q)
    }
    observer_id = int(
        spec.structured_events.commit_observer_id.removeprefix("replica-")
    )
    observer = _event(
        spec,
        replica_id=observer_id,
        sequence=2,
        timestamp_ns=200,
        event_type="block.committed",
        payload={
            **identity,
            "decision_proof": {
                "epoch_number": 0,
                "tree_id": 0,
                "epoch_digest": "11" * 32,
                "block_hash": identity["block_hash"],
            },
        },
    )
    streams[spec.structured_events.commit_observer_id].append(observer)

    proof = execution._find_common_commit(
        spec,
        streams,
        start_ns=1,
        end_ns=1_000,
    )

    assert proof is not None
    assert proof["identity"] == {
        **identity,
        "decision_proof": {
            "epoch_number": 0,
            "tree_id": 0,
            "epoch_digest": "11" * 32,
            "block_hash": identity["block_hash"],
        },
    }
    assert proof["observer"] == observer.reference()
    assert len(proof["witnesses"]) == spec.q
    assert [row["source_id"] for row in proof["witnesses"]] == [
        f"replica-{replica_id}" for replica_id in range(spec.q)
    ]
    assert proof["common_monotonic_ns"] == 200

    without_one_witness = dict(streams)
    without_one_witness[f"replica-{spec.q - 1}"] = []
    assert execution._find_common_commit(
        spec,
        without_one_witness,
        start_ns=1,
        end_ns=1_000,
    ) is None


def _write_cycle2_terminal(spec: Any, slot_directory: Path) -> None:
    path = slot_directory / spec.structured_events.manager_output_relative_path
    path.write_bytes(
        _event_line(
            spec,
            source="adaptive-manager",
            instance=spec.structured_events.manager_source_instance,
            sequence=1,
            timestamp_ns=350_000_000_000,
            event_type="adaptive_v2_session_terminal",
            payload={
                "cycle_ordinal": 1,
                "outcome": "advanced",
                "reason": "successor_converged",
            },
        )
    )


def _direct_preflight(
    tmp_path: Path,
    smoke: execution.N7SmokeSlot,
) -> execution.ExecutionPreflight:
    binaries = _binaries(tmp_path / "bin")
    root = tmp_path / Path(smoke.slot.result_path).parent
    provenance = _build_provenance(tmp_path / "build-inputs", binaries)
    preflight = execution.ExecutionPreflight(
        revision="c" * 40,
        repository=tmp_path,
        build_directory=tmp_path / "build-adaptive",
        result_root=root,
        slot_directory=root / smoke.slot.slot_id,
        free_bytes=10_000,
        binaries=binaries,
        build_provenance=provenance,
    )
    execution.preserve_build_evidence(root, provenance)
    return preflight


def test_build_evidence_is_preserved_once_and_reverified_before_launch(
    tmp_path: Path,
    template_slot,
) -> None:
    smoke = execution.build_n7_ps_smoke_slot(template_slot)
    preflight = _direct_preflight(tmp_path, smoke)
    evidence = preflight.result_root / "build-evidence"
    expected = {
        *(f"binaries/{name}" for name in preflight.build_provenance["binaries"]),
        *(
            f"build-metadata/{name}"
            for name in preflight.build_provenance["build_metadata"]
        ),
    }

    assert {
        path.relative_to(evidence).as_posix()
        for path in evidence.rglob("*")
        if path.is_file()
    } == expected
    execution.verify_preserved_build_evidence(
        preflight.result_root,
        preflight.build_provenance,
    )
    with pytest.raises(execution.FactorialExecutionError, match="already exists"):
        execution.preserve_build_evidence(
            preflight.result_root,
            preflight.build_provenance,
        )

    archived = evidence / "binaries/app"
    archived.chmod(0o700)
    archived.write_bytes(archived.read_bytes() + b"tampered")
    launches: list[tuple[str, ...]] = []
    with pytest.raises(execution.FactorialExecutionError, match="build evidence"):
        execution.execute_slot_once(
            smoke.slot,
            smoke.runtime,
            preflight=preflight,
            static_artifacts=_smoke_static_artifacts(smoke),
            authorization_receipt=_smoke_authorization(smoke, preflight),
            campaign_member=False,
            run_command=lambda command, **_kwargs: launches.append(tuple(command)),
        )
    assert launches == []


def test_build_evidence_copy_failure_leaves_no_claimed_root_or_staging_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binaries = _binaries(tmp_path / "bin")
    provenance = _build_provenance(tmp_path / "build-inputs", binaries)
    result_root = tmp_path / "results" / "atomic-publication"
    original_copy = execution._copy_build_evidence_file
    copy_count = 0

    def fail_second_copy(*args: Any, **kwargs: Any) -> None:
        nonlocal copy_count
        copy_count += 1
        if copy_count == 2:
            raise execution.FactorialExecutionError("forced staged-copy failure")
        original_copy(*args, **kwargs)

    monkeypatch.setattr(execution, "_copy_build_evidence_file", fail_second_copy)

    with pytest.raises(execution.FactorialExecutionError, match="forced staged-copy"):
        execution.preserve_build_evidence(
            result_root,
            provenance,
            initial_files={"authorized-contract.json": b"{}\n"},
        )

    assert not result_root.exists()
    assert result_root.parent.is_dir()
    assert tuple(
        result_root.parent.glob(
            f".{result_root.name}.build-evidence-staging-*"
        )
    ) == ()


def test_build_evidence_publication_never_replaces_a_concurrent_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binaries = _binaries(tmp_path / "bin")
    provenance = _build_provenance(tmp_path / "build-inputs", binaries)
    result_root = tmp_path / "results" / "exclusive-publication"
    original_verify = execution.verify_preserved_build_evidence

    def create_collision(root: Path, value: Any) -> None:
        original_verify(root, value)
        result_root.mkdir()
        (result_root / "concurrent-owner.txt").write_text("preserve me\n")

    monkeypatch.setattr(execution, "verify_preserved_build_evidence", create_collision)

    with pytest.raises(execution.FactorialExecutionError, match="appeared during staging"):
        execution.preserve_build_evidence(result_root, provenance)

    assert (result_root / "concurrent-owner.txt").read_text() == "preserve me\n"
    assert not (result_root / execution.BUILD_EVIDENCE_DIRECTORY).exists()


def test_post_claim_move_failure_preserves_the_complete_envelope_across_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binaries = _binaries(tmp_path / "bin")
    provenance = _build_provenance(tmp_path / "build-inputs", binaries)
    result_root = tmp_path / "results" / "claimed-publication"
    initial_files = {
        "campaign-authorization.json": b'{"authorized":true}\n',
        "campaign-execution-contract.json": b'{"contract":true}\n',
    }
    original_rename = execution.os.rename
    move_count = 0

    def fail_second_move(source: Any, destination: Any) -> None:
        nonlocal move_count
        move_count += 1
        if move_count == 2:
            raise OSError("forced post-claim move failure")
        original_rename(source, destination)

    monkeypatch.setattr(execution.os, "rename", fail_second_move)

    with pytest.raises(
        execution.FactorialExecutionError,
        match="unmoved staged evidence preserved",
    ):
        execution.preserve_build_evidence(
            result_root,
            provenance,
            initial_files=initial_files,
        )

    staging_roots = tuple(
        result_root.parent.glob(
            f".{result_root.name}.build-evidence-staging-*"
        )
    )
    assert result_root.is_dir()
    assert len(staging_roots) == 1
    published = {path.name for path in result_root.iterdir()}
    preserved = {path.name for path in staging_roots[0].iterdir()}
    assert published.isdisjoint(preserved)
    assert published | preserved == {
        execution.BUILD_EVIDENCE_DIRECTORY,
        *initial_files,
    }


def test_cycle2_native_terminal_authorizes_only_zero_manager_exit(
    tmp_path: Path,
    template_slot,
) -> None:
    smoke = execution.build_n7_ps_smoke_slot(template_slot)
    slot_directory = tmp_path / smoke.slot.slot_id
    (slot_directory / "raw").mkdir(parents=True)
    _write_cycle2_terminal(smoke.runtime, slot_directory)
    clean_manager = _FakeProcess(9_001, returncode=0)
    record = ProcessRecord(
        "adaptive-manager",
        execution.MANAGER_REPLICA_ID,
        clean_manager.pid,
        clean_manager.pid,
        clean_manager,
    )
    expected_clean_exits: set[str] = set()

    def terminal() -> object | None:
        streams = execution.read_event_streams(
            smoke.runtime,
            slot_directory,
            allow_partial=False,
        )
        event = execution._successful_manager_terminal(
            streams,
            cycle_ordinal=1,
        )
        if event is not None:
            expected_clean_exits.add("adaptive-manager")
        return event

    observed = execution._wait_until(
        "cycle-2 terminal",
        terminal,
        phase_timeout_s=1,
        hard_deadline_ns=10_000,
        records=(record,),
        expected_clean_exits=expected_clean_exits,
        raw_now_ns=lambda: 1,
        sleep=lambda _seconds: None,
        poll_interval_s=0,
    )
    assert isinstance(observed, execution._Event)

    clean_manager.returncode = 7
    with pytest.raises(execution.IncompleteFactorialSlot, match="adaptive-manager=7"):
        execution._assert_process_health(
            (record,),
            expected_clean_exits=expected_clean_exits,
        )

    clean_manager.returncode = 0
    with pytest.raises(execution.IncompleteFactorialSlot, match="adaptive-manager=0"):
        execution._assert_process_health((record,), expected_clean_exits=())


def test_wait_health_freshly_authorizes_manager_exit_after_stale_predicate_snapshot(
    tmp_path: Path,
    template_slot,
) -> None:
    smoke = execution.build_n7_ps_smoke_slot(template_slot)
    slot_directory = tmp_path / smoke.slot.slot_id
    (slot_directory / "raw").mkdir(parents=True)
    predicate_saw_terminal = False

    class ExitAfterPredicateSnapshot(_FakeProcess):
        def poll(self) -> int | None:
            assert not predicate_saw_terminal
            _write_cycle2_terminal(smoke.runtime, slot_directory)
            self.returncode = 0
            return self.returncode

    manager = ExitAfterPredicateSnapshot(9_002)
    record = ProcessRecord(
        "adaptive-manager",
        execution.MANAGER_REPLICA_ID,
        manager.pid,
        manager.pid,
        manager,
    )

    def predicate() -> object:
        nonlocal predicate_saw_terminal
        snapshot = execution.read_event_streams(
            smoke.runtime,
            slot_directory,
            allow_partial=True,
        )
        predicate_saw_terminal = (
            execution._successful_manager_terminal(snapshot, cycle_ordinal=1)
            is not None
        )
        return "ready"

    result = execution._wait_until(
        "cycle-2 terminal race",
        predicate,
        phase_timeout_s=1,
        hard_deadline_ns=10_000,
        records=(record,),
        clean_exit_authorizer=lambda exited: execution._authorize_manager_clean_exit(
            smoke.runtime,
            slot_directory,
            exited,
        ),
        raw_now_ns=lambda: 1,
        sleep=lambda _seconds: None,
        poll_interval_s=0,
    )

    assert result == "ready"
    assert manager.returncode == 0


def test_fresh_manager_exit_authorization_rejects_missing_terminal(
    tmp_path: Path,
    template_slot,
) -> None:
    smoke = execution.build_n7_ps_smoke_slot(template_slot)
    slot_directory = tmp_path / smoke.slot.slot_id
    (slot_directory / "raw").mkdir(parents=True)
    manager = _FakeProcess(9_003, returncode=0)
    record = ProcessRecord(
        "adaptive-manager",
        execution.MANAGER_REPLICA_ID,
        manager.pid,
        manager.pid,
        manager,
    )

    with pytest.raises(execution.IncompleteFactorialSlot, match="adaptive-manager=0"):
        execution._assert_process_health(
            (record,),
            clean_exit_authorizer=lambda exited: execution._authorize_manager_clean_exit(
                smoke.runtime,
                slot_directory,
                exited,
            ),
        )


def test_fresh_manager_exit_authorization_rejects_partial_terminal(
    tmp_path: Path,
    template_slot,
) -> None:
    smoke = execution.build_n7_ps_smoke_slot(template_slot)
    slot_directory = tmp_path / smoke.slot.slot_id
    manager_path = slot_directory / smoke.runtime.structured_events.manager_output_relative_path
    manager_path.parent.mkdir(parents=True)
    manager_path.write_bytes(b'{"event_schema_version":1')
    manager = _FakeProcess(9_004, returncode=0)
    record = ProcessRecord(
        "adaptive-manager",
        execution.MANAGER_REPLICA_ID,
        manager.pid,
        manager.pid,
        manager,
    )

    with pytest.raises(execution.FactorialExecutionError, match="incomplete final JSONL"):
        execution._assert_process_health(
            (record,),
            clean_exit_authorizer=lambda exited: execution._authorize_manager_clean_exit(
                smoke.runtime,
                slot_directory,
                exited,
            ),
        )


@pytest.mark.parametrize(
    ("terminal_payload", "error_match"),
    (
        (
            {
                "cycle_ordinal": 0,
                "outcome": "advanced",
                "reason": "successor_converged",
            },
            "adaptive-manager=0",
        ),
        (
            {
                "cycle_ordinal": 1,
                "outcome": "incomplete",
                "reason": "successor_converged",
            },
            "cycle 1 terminated without convergence",
        ),
        (
            {
                "cycle_ordinal": 1,
                "outcome": "advanced",
                "reason": "successor_timeout",
            },
            "cycle 1 terminated without convergence",
        ),
    ),
)
def test_fresh_manager_exit_authorization_rejects_wrong_terminal(
    tmp_path: Path,
    template_slot,
    terminal_payload: dict[str, object],
    error_match: str,
) -> None:
    smoke = execution.build_n7_ps_smoke_slot(template_slot)
    slot_directory = tmp_path / smoke.slot.slot_id
    manager_path = slot_directory / smoke.runtime.structured_events.manager_output_relative_path
    manager_path.parent.mkdir(parents=True)
    manager_path.write_bytes(
        _event_line(
            smoke.runtime,
            source="adaptive-manager",
            instance=smoke.runtime.structured_events.manager_source_instance,
            sequence=1,
            timestamp_ns=350_000_000_000,
            event_type="adaptive_v2_session_terminal",
            payload=terminal_payload,
        )
    )
    manager = _FakeProcess(9_005, returncode=0)
    record = ProcessRecord(
        "adaptive-manager",
        execution.MANAGER_REPLICA_ID,
        manager.pid,
        manager.pid,
        manager,
    )

    with pytest.raises(
        execution.IncompleteFactorialSlot,
        match=error_match,
    ):
        execution._assert_process_health(
            (record,),
            clean_exit_authorizer=lambda exited: execution._authorize_manager_clean_exit(
                smoke.runtime,
                slot_directory,
                exited,
            ),
        )


def test_fresh_manager_exit_authorization_never_applies_to_other_process(
    tmp_path: Path,
    template_slot,
) -> None:
    smoke = execution.build_n7_ps_smoke_slot(template_slot)
    slot_directory = tmp_path / smoke.slot.slot_id
    (slot_directory / "raw").mkdir(parents=True)
    _write_cycle2_terminal(smoke.runtime, slot_directory)
    replica = _FakeProcess(9_006, returncode=0)
    record = ProcessRecord("replica-0", 0, replica.pid, replica.pid, replica)

    with pytest.raises(execution.IncompleteFactorialSlot, match="replica-0=0"):
        execution._assert_process_health(
            (record,),
            clean_exit_authorizer=lambda exited: execution._authorize_manager_clean_exit(
                smoke.runtime,
                slot_directory,
                exited,
            ),
        )


def test_process_health_never_authorizes_nonzero_exit() -> None:
    manager = _FakeProcess(9_007, returncode=7)
    record = ProcessRecord(
        "adaptive-manager",
        execution.MANAGER_REPLICA_ID,
        manager.pid,
        manager.pid,
        manager,
    )
    authorization_calls = 0

    def authorize(_record: ProcessRecord) -> bool:
        nonlocal authorization_calls
        authorization_calls += 1
        return True

    with pytest.raises(execution.IncompleteFactorialSlot, match="adaptive-manager=7"):
        execution._assert_process_health(
            (record,),
            clean_exit_authorizer=authorize,
        )

    assert authorization_calls == 0


@pytest.mark.parametrize(
    ("now_ns", "accepted"),
    ((999, True), (1_000, False), (1_001, False)),
)
def test_wait_until_enforces_the_hard_deadline_before_accepting_success(
    now_ns: int,
    accepted: bool,
) -> None:
    arguments = dict(
        description="strict hard-bound result",
        predicate=lambda: "ready",
        phase_timeout_s=10,
        hard_deadline_ns=1_000,
        records=(),
        raw_now_ns=lambda: now_ns,
        sleep=lambda _seconds: None,
        poll_interval_s=0,
    )

    if accepted:
        assert execution._wait_until(**arguments) == "ready"
    else:
        with pytest.raises(
            execution.IncompleteFactorialSlot,
            match="hard deadline expired",
        ):
            execution._wait_until(**arguments)


@pytest.mark.parametrize(
    ("result_now_ns", "accepted"),
    (
        (execution.NANOSECONDS_PER_SECOND - 1, True),
        (execution.NANOSECONDS_PER_SECOND, False),
        (execution.NANOSECONDS_PER_SECOND + 1, False),
    ),
)
def test_wait_until_enforces_the_phase_deadline_before_accepting_success(
    result_now_ns: int,
    accepted: bool,
) -> None:
    clock = iter((0, result_now_ns))
    arguments = dict(
        description="strict phase-bound result",
        predicate=lambda: "ready",
        phase_timeout_s=1,
        hard_deadline_ns=10 * execution.NANOSECONDS_PER_SECOND,
        records=(),
        raw_now_ns=lambda: next(clock),
        sleep=lambda _seconds: None,
        poll_interval_s=0,
    )

    if accepted:
        assert execution._wait_until(**arguments) == "ready"
    else:
        with pytest.raises(
            execution.IncompleteFactorialSlot,
            match="timed out waiting",
        ):
            execution._wait_until(**arguments)


def test_epoch2_stable_and_drain_must_finish_strictly_before_fault_end(
    template_slot,
) -> None:
    spec = execution.build_n7_ps_smoke_slot(template_slot).runtime
    anchor_ns = 1_000
    phases = _fake_phases(
        spec,
        shared_raw_clock_anchor_ns=anchor_ns,
    )
    execution._assert_epoch2_completion_within_fault_window(
        spec,
        phases,
        shared_raw_clock_anchor_ns=anchor_ns,
    )

    fault_end = anchor_ns + (
        spec.fault_window.start_after_prelaunch_anchor_s
        + spec.fault_window.duration_s
    ) * execution.NANOSECONDS_PER_SECOND
    at_boundary = json.loads(json.dumps(phases))
    at_boundary["cutoffs"][0]["source_monotonic_ns"] = fault_end
    with pytest.raises(execution.IncompleteFactorialSlot, match="fault window"):
        execution._assert_epoch2_completion_within_fault_window(
            spec,
            at_boundary,
            shared_raw_clock_anchor_ns=anchor_ns,
        )

    stable_at_boundary = json.loads(json.dumps(phases))
    next(
        phase
        for phase in stable_at_boundary["phases"]
        if phase["phase"] == "epoch2_stable"
    )["end_monotonic_ns"] = fault_end
    with pytest.raises(execution.IncompleteFactorialSlot, match="fault window"):
        execution._assert_epoch2_completion_within_fault_window(
            spec,
            stable_at_boundary,
            shared_raw_clock_anchor_ns=anchor_ns,
        )


def test_execute_slot_launches_manager_then_each_replica_once_and_cleans_up(
    tmp_path: Path, template_slot
) -> None:
    smoke = execution.build_n7_ps_smoke_slot(template_slot)
    preflight = _direct_preflight(tmp_path, smoke)
    registries: list[_FakeRegistry] = []
    commands: list[tuple[str, ...]] = []
    next_pid = 10_000

    def registry_factory(**kwargs: Any) -> _FakeRegistry:
        del kwargs
        value = _FakeRegistry()
        registries.append(value)
        return value

    def popen(command: Any, **_kwargs: Any) -> _FakeProcess:
        nonlocal next_pid
        commands.append(tuple(command))
        next_pid += 1
        return _FakeProcess(next_pid)

    clock_values: list[int] = []
    clock = 10_000_000

    def now() -> int:
        nonlocal clock
        clock += 1_000
        clock_values.append(clock)
        return clock

    result = execution.execute_slot_once(
        smoke.slot,
        smoke.runtime,
        preflight=preflight,
        static_artifacts=_smoke_static_artifacts(smoke),
        authorization_receipt=_smoke_authorization(smoke, preflight),
        campaign_member=False,
        raw_now_ns=now,
        wall_now=lambda: "2026-08-04T00:00:00+00:00",
        run_command=_identity_command,
        popen_factory=popen,
        registry_factory=registry_factory,
        observer=_fake_phases,
        sleep=lambda _seconds: None,
        wait_ports_clear=lambda _ports, _timeout: None,
    )

    assert result.outcome == "PASS"
    assert result.launch_count == smoke.slot.replica_count + 1
    assert len(commands) == smoke.slot.replica_count + 1
    archived = execution._preserved_execution_binaries(preflight.result_root)
    assert commands[0][0] == str(archived.manager)
    assert all(command[0] == str(archived.app) for command in commands[1:])
    assert registries[0].cleanup_calls == 1
    assert len(registries[0].records) == smoke.slot.replica_count + 1
    assert all(record.pid == record.pgid for record in registries[0].records)
    receipt = json.loads((result.slot_directory / "slot.json").read_text())
    start = receipt["fault_window_start_ns"]
    anchor = receipt["shared_raw_clock_anchor_ns"]
    assert start == anchor + (
        smoke.runtime.fault_window.start_after_prelaunch_anchor_s * 1_000_000_000
    )
    assert anchor in clock_values
    phase_cutoffs = json.loads(
        (result.slot_directory / "phase-cutoffs.json").read_text()
    )
    assert [row["phase"] for row in phase_cutoffs["phase_qualifications"]] == [
        "baseline",
        "epoch1_stable",
        "epoch2_stable",
    ]
    assert all(
        len(row["common_commit"]["witnesses"]) == smoke.runtime.q
        for row in phase_cutoffs["phase_qualifications"]
    )
    assert (result.slot_directory / "raw/process/adaptive-manager.stdout.log").is_file()
    assert (result.slot_directory / "raw/process/adaptive-manager.stderr.log").is_file()
    assert (
        result.slot_directory / "raw/process/adaptive-manager.stdout.log"
    ).resolve() != (
        result.slot_directory / "raw/process/adaptive-manager.stderr.log"
    ).resolve()
    provenance = json.loads(
        (result.slot_directory / "runtime/execution-provenance.json").read_text()
    )
    authorization_bytes = (
        result.slot_directory / "execution-authorization.json"
    ).read_bytes()
    authorization = json.loads(authorization_bytes)
    assert authorization["scope"] == "excluded_n7_smoke"
    assert authorization["authorized_by"] == "thesis_author"
    expected_redaction_key = execution._derive_redaction_key(
        authorization_bytes,
        smoke.slot.slot_id,
    )
    assert receipt["redaction_key_id"] == hashlib.sha256(
        expected_redaction_key
    ).hexdigest()[:16]
    assert provenance["execution_authorization_id"] == authorization[
        "authorization_id"
    ]
    assert provenance["execution_authorization_sha256"] == hashlib.sha256(
        authorization_bytes
    ).hexdigest()
    assert "launch_vector_hmac_sha256" not in provenance
    assert "launch_vector_hmac_key_persisted" not in provenance
    binding = provenance["launch_binding"]
    assert binding["algorithm"] == "sha256"
    assert binding["sha256"] == hashlib.sha256(
        execution._canonical_json_bytes(binding["payload"])
    ).hexdigest()
    assert binding["payload"]["redacted_receipt_sha256"] == hashlib.sha256(
        execution._canonical_json_bytes(receipt)
    ).hexdigest()
    assert binding["payload"]["input_hashes"] == [
        {
            "relative_path": row["relative_path"],
            "sha256": row["sha256"],
        }
        for row in provenance["input_artifacts"]
    ]
    assert binding["payload"]["executables"] == {
        name: {"path": row["path"], "sha256": row["sha256"]}
        for name, row in sorted(provenance["binaries"].items())
    }
    provenance_payload = json.dumps(provenance, sort_keys=True)
    identities = _identity_material(smoke.runtime.replica_count)
    assert all(row["sec"] not in provenance_payload for row in identities.bls)
    assert all(row["sec"] not in provenance_payload for row in identities.tls)
    assert identities.issuer["sec"] not in provenance_payload
    outcome = json.loads((result.slot_directory / "outcome.json").read_text())
    assert outcome["history"] == [
        {"sequence": 0, "state": "NOT_STARTED", "reason": None},
        {"sequence": 1, "state": "PASS", "reason": None},
    ]
    assert set(outcome["sealed_files"]) == {
        str(path.relative_to(result.slot_directory))
        for path in result.slot_directory.rglob("*")
        if path.is_file() and path.name != "outcome.json"
    }
    from experiments.adaptive.kauri_experiment import (
        factorial_validation as independent_validation,
    )

    for relative_path in (
        "cleanup-ledger.json",
        "execution-authorization.json",
        "outcome.json",
        "phase-cutoffs.json",
        "slot.json",
        "throughput.json",
        "runtime/exact-build-provenance.json",
        "runtime/execution-provenance.json",
    ):
        payload = (result.slot_directory / relative_path).read_bytes()
        assert payload == execution._canonical_json_bytes(json.loads(payload))
        assert independent_validation._read_json(
            result.slot_directory, relative_path
        ) is not None
    for line in (result.slot_directory / "runner-state.jsonl").read_bytes().splitlines(
        keepends=True
    ):
        assert line == execution._canonical_json_bytes(json.loads(line))


def test_execution_authorization_is_required_and_exact(
    tmp_path: Path, template_slot
) -> None:
    smoke = execution.build_n7_ps_smoke_slot(template_slot)
    preflight = _direct_preflight(tmp_path, smoke)
    static_artifacts = _smoke_static_artifacts(smoke)

    with pytest.raises(execution.FactorialExecutionError, match="authorization"):
        execution._bind_execution_authorization(
            b"",
            slot=smoke.slot,
            preflight=preflight,
            static_artifacts=static_artifacts,
            campaign_member=False,
        )

    wrong_revision = execution.build_execution_authorization_receipt(
        scope="excluded_n7_smoke",
        approval_reference="test thesis-author approval",
        approved_utc="2026-08-04T00:00:00+00:00",
        kauri_revision="d" * 40,
        slot_ids=(smoke.slot.slot_id,),
        result_root=Path(smoke.slot.result_path).parent.as_posix(),
        static_artifacts=static_artifacts,
        build_provenance_sha256=hashlib.sha256(
            execution._canonical_json_bytes(preflight.build_provenance)
        ).hexdigest(),
    )
    with pytest.raises(execution.FactorialExecutionError, match="not exact"):
        execution._bind_execution_authorization(
            wrong_revision,
            slot=smoke.slot,
            preflight=preflight,
            static_artifacts=static_artifacts,
            campaign_member=False,
        )

    exact = _smoke_authorization(smoke, preflight)
    relocated_root = preflight.repository / "results/relocated-smoke-attempt"
    relocated = replace(
        preflight,
        result_root=relocated_root,
        slot_directory=relocated_root / smoke.slot.slot_id,
    )
    with pytest.raises(execution.FactorialExecutionError, match="not exact"):
        execution._bind_execution_authorization(
            exact,
            slot=smoke.slot,
            preflight=relocated,
            static_artifacts=static_artifacts,
            campaign_member=False,
        )

    with pytest.raises(
        execution.FactorialExecutionError,
        match="canonical repository-relative results path",
    ):
        execution.build_execution_authorization_receipt(
            scope="excluded_n7_smoke",
            approval_reference="test thesis-author approval",
            approved_utc="2026-08-04T00:00:00+00:00",
            kauri_revision=preflight.revision,
            slot_ids=(smoke.slot.slot_id,),
            result_root="results/../relocated-smoke-attempt",
            static_artifacts=static_artifacts,
            build_provenance_sha256=hashlib.sha256(
                execution._canonical_json_bytes(preflight.build_provenance)
            ).hexdigest(),
        )


def test_campaign_execute_rejects_ordinal_68_before_ordinal_1(tmp_path: Path) -> None:
    context = _campaign_launch_context(tmp_path, 68)
    ledger = context["preflight"].result_root / execution.CAMPAIGN_LEDGER_FILENAME
    ledger.write_bytes(
        execution._canonical_json_bytes(
            _campaign_ledger_row(context, 68, "STARTED", 1)
        )
    )
    launches: list[tuple[str, ...]] = []

    with pytest.raises(
        execution.FactorialExecutionError,
        match="exact next-slot prefix",
    ):
        execution.execute_slot_once(
            context["slot"],
            context["spec"],
            preflight=context["preflight"],
            static_artifacts=context["static_artifacts"],
            authorization_receipt=context["authorization_payload"],
            campaign_member=True,
            run_command=lambda command, **_kwargs: launches.append(tuple(command)),
        )

    assert launches == []
    assert not context["preflight"].slot_directory.exists()


def test_campaign_launch_guard_accepts_only_the_exact_passed_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _campaign_launch_context(tmp_path / "first", 1)
    first_ledger = (
        first["preflight"].result_root / execution.CAMPAIGN_LEDGER_FILENAME
    )
    first_ledger.write_bytes(
        execution._canonical_json_bytes(
            _campaign_ledger_row(first, 1, "STARTED", 1)
        )
    )
    execution._validate_campaign_launch_order(
        spec=first["spec"],
        preflight=first["preflight"],
        static_artifacts=first["static_artifacts"],
        authorization_receipt=first["authorization_payload"],
        authorization=first["authorization"],
    )

    second = _campaign_launch_context(tmp_path / "second", 2)
    prior_spec = next(
        item for item in second["runtime"].slots if item.execution_ordinal == 1
    )
    prior_directory = second["preflight"].result_root / prior_spec.slot_id
    (prior_directory / "runtime").mkdir(parents=True)
    (prior_directory / "execution-authorization.json").write_bytes(
        second["authorization_payload"]
    )
    (prior_directory / "runtime/exact-build-provenance.json").write_bytes(
        execution._canonical_json_bytes(second["preflight"].build_provenance)
    )
    from experiments.adaptive.kauri_experiment import factorial_validation

    monkeypatch.setattr(
        factorial_validation,
        "validate_slot",
        lambda _path: SimpleNamespace(
            slot_id=prior_spec.slot_id,
            outcome="PASS",
            reason=None,
            integrity_valid=True,
            campaign_member=True,
            figure_eligible=True,
        ),
    )
    rows = [
        _campaign_ledger_row(second, 1, "STARTED", 1),
        _campaign_ledger_row(second, 1, "TERMINAL", 2),
        _campaign_ledger_row(second, 2, "STARTED", 3),
    ]
    second_ledger = (
        second["preflight"].result_root / execution.CAMPAIGN_LEDGER_FILENAME
    )
    second_ledger.write_bytes(
        b"".join(execution._canonical_json_bytes(row) for row in rows)
    )
    execution._validate_campaign_launch_order(
        spec=second["spec"],
        preflight=second["preflight"],
        static_artifacts=second["static_artifacts"],
        authorization_receipt=second["authorization_payload"],
        authorization=second["authorization"],
    )

    rejected_rows = [dict(row) for row in rows]
    rejected_rows[1]["execution_outcome"] = "INCOMPLETE"
    rejected_rows[1]["execution_reason"] = "forced failure"
    second_ledger.write_bytes(
        b"".join(execution._canonical_json_bytes(row) for row in rejected_rows)
    )
    with pytest.raises(
        execution.FactorialExecutionError,
        match="non-PASS terminal attempt",
    ):
        execution._validate_campaign_launch_order(
            spec=second["spec"],
            preflight=second["preflight"],
            static_artifacts=second["static_artifacts"],
            authorization_receipt=second["authorization_payload"],
            authorization=second["authorization"],
        )


def test_campaign_launch_guard_rejects_finalized_and_typed_drift(
    tmp_path: Path,
) -> None:
    context = _campaign_launch_context(tmp_path, 1)
    root = context["preflight"].result_root
    ledger = root / execution.CAMPAIGN_LEDGER_FILENAME
    started = _campaign_ledger_row(context, 1, "STARTED", 1)

    typed_drift = dict(started)
    typed_drift["schema_version"] = True
    ledger.write_bytes(execution._canonical_json_bytes(typed_drift))
    with pytest.raises(
        execution.FactorialExecutionError,
        match="identity/order binding drifted",
    ):
        execution._validate_campaign_launch_order(
            spec=context["spec"],
            preflight=context["preflight"],
            static_artifacts=context["static_artifacts"],
            authorization_receipt=context["authorization_payload"],
            authorization=context["authorization"],
        )

    ledger.write_bytes(execution._canonical_json_bytes(started))
    summary = root / execution.CAMPAIGN_SUMMARY_FILENAME
    summary.write_bytes(execution._canonical_json_bytes({"finalized": True}))
    with pytest.raises(
        execution.FactorialExecutionError,
        match="finalized campaign",
    ):
        execution._validate_campaign_launch_order(
            spec=context["spec"],
            preflight=context["preflight"],
            static_artifacts=context["static_artifacts"],
            authorization_receipt=context["authorization_payload"],
            authorization=context["authorization"],
        )

    summary.unlink()
    contract_path = root / execution.CAMPAIGN_CONTRACT_FILENAME
    typed_contract = json.loads(context["contract_payload"])
    typed_contract["outcome_dependent_order"] = 0
    contract_path.write_bytes(execution._canonical_json_bytes(typed_contract))
    with pytest.raises(
        execution.FactorialExecutionError,
        match="exact frozen schedule",
    ):
        execution._validate_campaign_launch_order(
            spec=context["spec"],
            preflight=context["preflight"],
            static_artifacts=context["static_artifacts"],
            authorization_receipt=context["authorization_payload"],
            authorization=context["authorization"],
        )


def test_redaction_key_derivation_is_domain_separated_and_reproducible() -> None:
    receipt = b'{"authorization_id":"test"}\n'

    key = execution._derive_redaction_key(receipt, "smoke-n7-f2-PS")

    assert key.hex() == (
        "0a13b2c0bec487ca83079721468c94b2a"
        "688df0d09b6376110145bdc98b9c988"
    )
    assert key == execution._derive_redaction_key(receipt, "smoke-n7-f2-PS")
    assert key != execution._derive_redaction_key(receipt + b" ", "smoke-n7-f2-PS")
    assert key != execution._derive_redaction_key(receipt, "smoke-n7-f2-PS-other")


@pytest.mark.parametrize(
    ("write_terminal", "manager_returncode", "expected_outcome", "classification"),
    (
        (True, 0, "PASS", "expected_clean_exit"),
        (True, 7, "INCOMPLETE", "unexpected_precleanup_exit"),
        (False, 0, "INCOMPLETE", "unexpected_precleanup_exit"),
    ),
)
def test_cleanup_classifies_only_terminal_authorized_zero_manager_exit(
    tmp_path: Path,
    template_slot,
    write_terminal: bool,
    manager_returncode: int,
    expected_outcome: str,
    classification: str,
) -> None:
    smoke = execution.build_n7_ps_smoke_slot(template_slot)
    preflight = _direct_preflight(tmp_path, smoke)
    registry = _FakeRegistry()
    next_pid = 25_000

    def popen(_command: Any, **_kwargs: Any) -> _FakeProcess:
        nonlocal next_pid
        next_pid += 1
        return _FakeProcess(next_pid)

    def observer(
        spec: Any,
        slot_directory: Path,
        records: list[ProcessRecord],
        **_kwargs: Any,
    ) -> dict[str, object]:
        if write_terminal:
            _write_cycle2_terminal(spec, slot_directory)
        records[0].process.returncode = manager_returncode
        return _fake_phases(spec)

    clock = 70_000

    def now() -> int:
        nonlocal clock
        clock += 1
        return clock

    result = execution.execute_slot_once(
        smoke.slot,
        smoke.runtime,
        preflight=preflight,
        static_artifacts=_smoke_static_artifacts(smoke),
        authorization_receipt=_smoke_authorization(smoke, preflight),
        campaign_member=False,
        raw_now_ns=now,
        wall_now=lambda: "2026-08-04T00:00:00+00:00",
        run_command=_identity_command,
        popen_factory=popen,
        registry_factory=lambda **_kwargs: registry,
        observer=observer,
        sleep=lambda _seconds: None,
        wait_ports_clear=lambda _ports, _timeout: None,
    )

    assert result.outcome == expected_outcome
    manager_row = next(
        row for row in result.cleanup_ledger if row["name"] == "adaptive-manager"
    )
    assert manager_row["classification"] == classification
    if classification == "expected_clean_exit":
        assert manager_row["exit_authorization"]["event_type"] == (
            "adaptive_v2_session_terminal"
        )
    else:
        assert manager_row["exit_authorization"] is None


@pytest.mark.parametrize(
    ("signal_number", "returncode", "expected_classification"),
    (
        (int(signal.SIGINT), 0, "expected_cleanup"),
        (int(signal.SIGINT), -int(signal.SIGINT), "expected_cleanup"),
        (int(signal.SIGTERM), 0, "expected_cleanup"),
        (int(signal.SIGTERM), -int(signal.SIGTERM), "expected_cleanup"),
        (int(signal.SIGKILL), -int(signal.SIGKILL), "expected_cleanup"),
        (int(signal.SIGKILL), 0, "unexpected_cleanup_exit"),
        (int(signal.SIGTERM), 7, "unexpected_cleanup_exit"),
        (99, -99, "unexpected_cleanup_exit"),
    ),
)
def test_cleanup_requires_a_credible_signal_returncode_pair(
    signal_number: int,
    returncode: int,
    expected_classification: str,
) -> None:
    process = _FakeProcess(pid=31_000, returncode=returncode)
    record = ProcessRecord("replica-0", 0, process.pid, process.pid, process)
    outcome = CleanupOutcome(
        name=record.name,
        replica_id=record.replica_id,
        pid=record.pid,
        pgid=record.pgid,
        signal_number=signal_number,
        returncode=returncode,
    )

    rows = execution._cleanup_ledger(
        (record,),
        (outcome,),
        cleanup_started_ns=32_000,
    )

    assert rows[0]["classification"] == expected_classification


def test_bad_cleanup_returncode_makes_the_attempt_incomplete(
    tmp_path: Path,
    template_slot,
) -> None:
    smoke = execution.build_n7_ps_smoke_slot(template_slot)
    preflight = _direct_preflight(tmp_path, smoke)
    next_pid = 33_000

    class BadCleanupRegistry(_FakeRegistry):
        def cleanup(self, *, timeout_s: float) -> tuple[CleanupOutcome, ...]:
            del timeout_s
            self.cleanup_calls += 1
            rows: list[CleanupOutcome] = []
            for record in self.records:
                record.process.returncode = 7
                rows.append(
                    CleanupOutcome(
                        name=record.name,
                        replica_id=record.replica_id,
                        pid=record.pid,
                        pgid=record.pgid,
                        signal_number=int(signal.SIGTERM),
                        returncode=7,
                    )
                )
            return tuple(rows)

    registry = BadCleanupRegistry()

    def popen(_command: Any, **_kwargs: Any) -> _FakeProcess:
        nonlocal next_pid
        next_pid += 1
        return _FakeProcess(next_pid)

    result = execution.execute_slot_once(
        smoke.slot,
        smoke.runtime,
        preflight=preflight,
        static_artifacts=_smoke_static_artifacts(smoke),
        authorization_receipt=_smoke_authorization(smoke, preflight),
        campaign_member=False,
        raw_now_ns=lambda: 34_000,
        wall_now=lambda: "2026-08-04T00:00:00+00:00",
        run_command=_identity_command,
        popen_factory=popen,
        registry_factory=lambda **_kwargs: registry,
        observer=_fake_phases,
        sleep=lambda _seconds: None,
        wait_ports_clear=lambda _ports, _timeout: None,
    )

    assert result.outcome == "INCOMPLETE"
    assert result.reason == "cleanup ledger contains a non-expected process exit"
    assert {row["classification"] for row in result.cleanup_ledger} == {
        "unexpected_cleanup_exit"
    }


def test_final_throughput_rereads_complete_streams_only_after_cleanup(
    tmp_path: Path,
    template_slot,
) -> None:
    smoke = execution.build_n7_ps_smoke_slot(template_slot)
    preflight = _direct_preflight(tmp_path, smoke)
    phase_document: dict[str, object] = {}
    next_pid = 27_000

    class CompletingRegistry(_FakeRegistry):
        def cleanup(self, *, timeout_s: float) -> tuple[CleanupOutcome, ...]:
            outcomes = super().cleanup(timeout_s=timeout_s)
            baseline = next(
                phase
                for phase in phase_document["phases"]  # type: ignore[index]
                if phase["phase"] == "baseline"
            )
            observer_path = (
                preflight.slot_directory
                / smoke.runtime.structured_events.replica_output_relative_paths[0]
            )
            observer_path.write_bytes(
                _event_line(
                    smoke.runtime,
                    source="replica-0",
                    instance=smoke.runtime.structured_events.replica_source_instances[0],
                    sequence=1,
                    timestamp_ns=int(baseline["start_monotonic_ns"]) + 1,
                    event_type="block.committed",
                    payload={
                        "block_height": 1,
                        "block_hash": "1" * 64,
                        "parent_hash": "0" * 64,
                        "transaction_count": 11,
                        "decision_proof": {
                            "epoch_number": 0,
                            "tree_id": 0,
                            "epoch_digest": "11" * 32,
                            "block_hash": "1" * 64,
                        },
                    },
                )
            )
            return outcomes

    registry = CompletingRegistry()

    def popen(_command: Any, **_kwargs: Any) -> _FakeProcess:
        nonlocal next_pid
        next_pid += 1
        return _FakeProcess(next_pid)

    def observer(
        spec: Any,
        slot_directory: Path,
        _records: list[ProcessRecord],
        **kwargs: Any,
    ) -> dict[str, object]:
        nonlocal phase_document
        phase_document = _fake_phases(spec, **kwargs)
        path = slot_directory / spec.structured_events.replica_output_relative_paths[0]
        path.write_bytes(b'{"event_schema_version":1')
        return phase_document

    clock = 80_000

    def now() -> int:
        nonlocal clock
        clock += 1
        return clock

    result = execution.execute_slot_once(
        smoke.slot,
        smoke.runtime,
        preflight=preflight,
        static_artifacts=_smoke_static_artifacts(smoke),
        authorization_receipt=_smoke_authorization(smoke, preflight),
        campaign_member=False,
        raw_now_ns=now,
        wall_now=lambda: "2026-08-04T00:00:00+00:00",
        run_command=_identity_command,
        popen_factory=popen,
        registry_factory=lambda **_kwargs: registry,
        observer=observer,
        sleep=lambda _seconds: None,
        wait_ports_clear=lambda _ports, _timeout: None,
    )

    assert result.outcome == "PASS"
    throughput = json.loads((result.slot_directory / "throughput.json").read_text())
    baseline = next(
        phase for phase in throughput["phases"] if phase["phase"] == "baseline"
    )
    assert baseline["transactions"] == 11


def test_cleanup_exception_still_persists_every_registered_process(
    tmp_path: Path,
    template_slot,
) -> None:
    smoke = execution.build_n7_ps_smoke_slot(template_slot)
    preflight = _direct_preflight(tmp_path, smoke)
    next_pid = 29_000

    class RaisingRegistry(_FakeRegistry):
        def cleanup(self, *, timeout_s: float) -> tuple[CleanupOutcome, ...]:
            del timeout_s
            self.cleanup_calls += 1
            raise RuntimeError("forced cleanup failure")

    registry = RaisingRegistry()

    def popen(_command: Any, **_kwargs: Any) -> _FakeProcess:
        nonlocal next_pid
        next_pid += 1
        return _FakeProcess(next_pid)

    result = execution.execute_slot_once(
        smoke.slot,
        smoke.runtime,
        preflight=preflight,
        static_artifacts=_smoke_static_artifacts(smoke),
        authorization_receipt=_smoke_authorization(smoke, preflight),
        campaign_member=False,
        raw_now_ns=lambda: 90_000,
        wall_now=lambda: "2026-08-04T00:00:00+00:00",
        run_command=_identity_command,
        popen_factory=popen,
        registry_factory=lambda **_kwargs: registry,
        observer=_fake_phases,
        sleep=lambda _seconds: None,
        wait_ports_clear=lambda _ports, _timeout: None,
    )

    assert result.outcome == "INCOMPLETE"
    assert result.reason == "forced cleanup failure"
    assert len(result.cleanup_ledger) == smoke.runtime.replica_count + 1
    assert {row["classification"] for row in result.cleanup_ledger} == {
        "cleanup_incomplete"
    }
    ledger = json.loads(
        (result.slot_directory / "cleanup-ledger.json").read_text()
    )
    assert len(ledger["processes"]) == smoke.runtime.replica_count + 1


def test_incomplete_attempt_is_preserved_without_retry(
    tmp_path: Path, template_slot
) -> None:
    smoke = execution.build_n7_ps_smoke_slot(template_slot)
    preflight = _direct_preflight(tmp_path, smoke)
    commands: list[tuple[str, ...]] = []
    registry = _FakeRegistry()
    next_pid = 20_000

    def popen(command: Any, **_kwargs: Any) -> _FakeProcess:
        nonlocal next_pid
        commands.append(tuple(command))
        next_pid += 1
        return _FakeProcess(next_pid)

    def incomplete(*_args: Any, **_kwargs: Any) -> Any:
        raise execution.IncompleteFactorialSlot("missing exact epoch-2 activation")

    clock = 50_000

    def now() -> int:
        nonlocal clock
        clock += 1
        return clock

    result = execution.execute_slot_once(
        smoke.slot,
        smoke.runtime,
        preflight=preflight,
        static_artifacts=_smoke_static_artifacts(smoke),
        authorization_receipt=_smoke_authorization(smoke, preflight),
        campaign_member=False,
        raw_now_ns=now,
        wall_now=lambda: "2026-08-04T00:00:00+00:00",
        run_command=_identity_command,
        popen_factory=popen,
        registry_factory=lambda **_kwargs: registry,
        observer=incomplete,
        sleep=lambda _seconds: None,
        wait_ports_clear=lambda _ports, _timeout: None,
    )

    assert result.outcome == "INCOMPLETE"
    assert result.reason == "missing exact epoch-2 activation"
    assert len(commands) == smoke.slot.replica_count + 1
    assert registry.cleanup_calls == 1
    outcome = json.loads((result.slot_directory / "outcome.json").read_text())
    assert outcome["history"][0] == {
        "sequence": 0,
        "state": "NOT_STARTED",
        "reason": None,
    }
    assert outcome["history"][1] == {
        "sequence": 1,
        "state": "INCOMPLETE",
        "reason": "missing exact epoch-2 activation",
    }
    with pytest.raises(execution.FactorialExecutionError, match="collision"):
        execution.execute_slot_once(
            smoke.slot,
            smoke.runtime,
            preflight=preflight,
            static_artifacts=_smoke_static_artifacts(smoke),
            authorization_receipt=_smoke_authorization(smoke, preflight),
            campaign_member=False,
        )
    assert len(commands) == smoke.slot.replica_count + 1


def test_monotonic_raw_clock_has_no_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(execution.time, "CLOCK_MONOTONIC_RAW", raising=False)
    with pytest.raises(execution.FactorialExecutionError, match="CLOCK_MONOTONIC_RAW"):
        execution.monotonic_raw_ns()


def test_spawn_opens_distinct_exclusive_streams(tmp_path: Path) -> None:
    registry = _FakeRegistry()
    process = _FakeProcess(33_333)
    spawned = execution.spawn_exclusive_owned_process(
        registry,  # type: ignore[arg-type]
        name="replica-0",
        replica_id=0,
        command=("hotstuff-app",),
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
        working_directory=tmp_path,
        popen_factory=lambda *_args, **_kwargs: process,
    )
    spawned.stdout.write(b"stdout\n")
    spawned.stderr.write(b"stderr\n")
    spawned.stdout.close()
    spawned.stderr.close()

    assert (tmp_path / "stdout.log").read_bytes() == b"stdout\n"
    assert (tmp_path / "stderr.log").read_bytes() == b"stderr\n"
    with pytest.raises(FileExistsError):
        execution.spawn_exclusive_owned_process(
            registry,  # type: ignore[arg-type]
            name="replica-1",
            replica_id=1,
            command=("hotstuff-app",),
            stdout_path=tmp_path / "stdout.log",
            stderr_path=tmp_path / "stderr-1.log",
            working_directory=tmp_path,
            popen_factory=lambda *_args, **_kwargs: _FakeProcess(33_334),
        )
