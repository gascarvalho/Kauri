"""Runtime and sealed-archive tests for the N31 signer-aware diagnosis."""

from __future__ import annotations

from copy import deepcopy
import importlib
import json
from pathlib import Path
from typing import Any

import pytest

DIAGNOSIS_PROFILE = (
    Path(__file__).parents[1] / "profiles" / "n31-f5-static-diagnosis-v1.json"
)
RUNTIME_PROFILE = (
    Path(__file__).parents[1] / "profiles" / "n31-f5-internal1-crash-shakedown-v1.json"
)
REVISION = "d" * 40
RUN_ID = "n31-runtime-test"
SELECTED_BLOCK = "2" * 64


def _runtime():
    return importlib.import_module(
        "experiments.adaptive.kauri_experiment.n31_static_diagnosis_runtime"
    )


def _diagnosis():
    return importlib.import_module(
        "experiments.adaptive.kauri_experiment.n31_static_diagnosis"
    )


def _runner():
    return importlib.import_module("experiments.adaptive.run_n31_static_diagnosis")


def _support():
    return importlib.import_module(
        "experiments.adaptive.tests.test_n31_static_diagnosis"
    )


@pytest.fixture(scope="module")
def profile():
    return _diagnosis().load_frozen_profile(DIAGNOSIS_PROFILE)


def _arm(profile: Any, name: str):
    return next(value for value in profile.arms if value.name == name)


def _actor_lines(
    profile: Any,
    arm_name: str,
    block_hash: str = SELECTED_BLOCK,
) -> str:
    if arm_name == _diagnosis().ARM_NAMES[0]:
        return (
            "ordinary actor output\n"
            "KAURI_FAULT false_report_positive_suppressed reporter=0 target=5 "
            f"epoch=0 tree=30 block={block_hash} "
            f"window={profile.diagnostic_window} monotonic_ns=5000\n"
            "KAURI_FAULT false_timeout_emitted reporter=0 target=5 "
            f"epoch=0 tree=30 block={block_hash} "
            f"window={profile.diagnostic_window} monotonic_ns=5600\n"
        )
    return (
        "ordinary actor output\n"
        "KAURI_FAULT direct_vote_omitted replica=5 parent=0 "
        f"epoch=0 tree=30 block={block_hash} "
        f"window={profile.diagnostic_window} monotonic_ns=5000\n"
    )


def _write_actor_log(
    tmp_path: Path,
    profile: Any,
    arm_name: str,
    block_hash: str = SELECTED_BLOCK,
) -> int:
    arm = _arm(profile, arm_name)
    path = tmp_path / "logs" / f"replica-{arm.actor_replica_id}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _actor_lines(profile, arm_name, block_hash)
    path.write_text(text, encoding="utf-8")
    return len("ordinary actor output\n".encode())


def _observation(
    profile: Any,
    *,
    observation_id: str,
    reporter_id: int,
    observed_replica_id: int,
    message_type: str,
    outcome: str,
    reporter_ns: int,
    signer_set: list[int],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "observation_id": observation_id,
        "reporter_id": reporter_id,
        "observed_replica_id": observed_replica_id,
        "configuration": {
            "epoch_number": 0,
            "tree_id": 30,
            "epoch_digest": profile.epoch_digest,
        },
        "block_hash": SELECTED_BLOCK,
        "expected_message_type": message_type,
        "outcome": outcome,
        "response_duration_us": 0,
        "deadline_duration_us": 500_000,
        "reporter_monotonic_ns": reporter_ns,
        "reporter_sequence": 1,
        "signer_set": signer_set,
    }


def _manager_event(
    observation: dict[str, object],
    *,
    sequence: int,
    timestamp: int,
) -> dict[str, object]:
    return {
        "event_schema_version": 1,
        "run_id": RUN_ID,
        "source_kind": "adaptation_manager",
        "source_id": "adaptive-manager",
        "source_instance": "manager-instance",
        "source_sequence": sequence,
        "source_monotonic_ns": timestamp,
        "event_type": "evidence.observation_accepted",
        "payload": {
            "ingestion_sequence": sequence,
            "observation": observation,
        },
    }


def _manager_pair(profile: Any, arm_name: str) -> list[dict[str, object]]:
    arm = _arm(profile, arm_name)
    witness = _observation(
        profile,
        observation_id="c" * 64,
        reporter_id=30,
        observed_replica_id=0,
        message_type="aggregate_relay",
        outcome="on_time",
        reporter_ns=5400 if arm_name == _diagnosis().ARM_NAMES[1] else 5500,
        signer_set=list(arm.witness_signer_set),
    )
    claim = _observation(
        profile,
        observation_id="b" * 64,
        reporter_id=0,
        observed_replica_id=5,
        message_type="direct_vote",
        outcome="timeout",
        reporter_ns=5500,
        signer_set=[],
    )
    # The witness deliberately reaches the manager first. Selection is not
    # permitted to infer causal order from ingestion order.
    return [
        _manager_event(witness, sequence=1, timestamp=6000),
        _manager_event(claim, sequence=2, timestamp=6100),
    ]


def _root_event(
    profile: Any,
    arm_name: str,
    *,
    timestamp: int = 6400,
    event_type: str = "aggregation.root_qc_published",
    accepted_signers: list[int] | None = None,
) -> dict[str, object]:
    if accepted_signers is None:
        accepted_signers = (
            list(range(21))
            if arm_name == _diagnosis().ARM_NAMES[0]
            else [replica for replica in range(22) if replica != 5]
        )
    return {
        "event_schema_version": 1,
        "run_id": RUN_ID,
        "source_kind": "replica",
        "source_id": "replica-30",
        "source_instance": "root-instance",
        "source_sequence": 3,
        "source_monotonic_ns": timestamp,
        "event_type": event_type,
        "payload": {
            "epoch_number": 0,
            "tree_id": 30,
            "epoch_digest": profile.epoch_digest,
            "block_hash": SELECTED_BLOCK,
            "context_generation": 1,
            "observer_replica": 30,
            "wait_exempt_signers": [],
            "accepted_signers": accepted_signers,
            "absent_direct_children": [],
            "missing_optional_signers": [],
            "required_branch_gaps": [],
            "root_signer_count": len(accepted_signers),
            "global_quorum": 21,
            "rejection_reason": None,
        },
    }


def test_preflight_binds_profiles_q21_and_actor_only_launches(
    monkeypatch: pytest.MonkeyPatch,
    profile: Any,
) -> None:
    module = _runtime()
    captured: dict[str, object] = {}

    def profiled_preflight(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "schema_version": 1,
            "verdict": "PASS",
            "profile_id": profile.runtime_profile_id,
            "profile_sha256": profile.runtime_profile_sha256,
            "revision": REVISION,
            "build_provenance": {"revision": REVISION},
            "epoch_zero_witness": {"epoch_digest": profile.epoch_digest},
        }

    monkeypatch.setattr(module.runtime, "preflight", profiled_preflight)
    result = module.preflight(
        diagnosis_profile_path=DIAGNOSIS_PROFILE,
        repository=Path(__file__).parents[3],
        app_binary=Path("/build/hotstuff-app"),
        manager_binary=Path("/build/adaptation-manager"),
        keygen_binary=Path("/build/hotstuff-keygen"),
        tls_keygen_binary=Path("/build/hotstuff-tls-keygen"),
        epoch_profile_digest_binary=Path("/build/epoch-profile-digest"),
        build_directory=Path("/build"),
        build_provenance_path=Path("/build/build-provenance.json"),
    )

    assert result["verdict"] == "PASS"
    assert result["revision"] == REVISION
    assert result["commit_witnesses"] == list(profile.commit_witnesses)
    assert set(result["launch_contracts"]) == set(_diagnosis().ARM_NAMES)
    for arm_name, contract in result["launch_contracts"].items():
        arm = _arm(profile, arm_name)
        active = [
            overlay["replica_id"]
            for overlay in contract["replica_overlays"]
            if overlay["argv"]
        ]
        assert active == [arm.actor_replica_id]
    assert captured["profile_path"].resolve() == RUNTIME_PROFILE.resolve()


@pytest.mark.parametrize("arm_name", _diagnosis().ARM_NAMES)
def test_actor_log_parsers_bind_one_exact_tree30_context(
    tmp_path: Path,
    profile: Any,
    arm_name: str,
) -> None:
    module = _runtime()
    arm = _arm(profile, arm_name)
    start = _write_actor_log(tmp_path, profile, arm_name)

    markers = module._find_markers(
        profile,
        arm,
        tmp_path,
        start_offset=start,
    )
    suppressions = module._find_false_positive_suppressions(
        profile,
        arm,
        tmp_path,
        start_offset=start,
    )

    assert len(markers) == 1
    assert markers[0]["block_hash"] == SELECTED_BLOCK
    assert markers[0]["source_id"] == f"replica-{arm.actor_replica_id}"
    assert markers[0]["configuration"] == f"0:30:{profile.epoch_digest}"
    assert len(suppressions) == (1 if arm_name == _diagnosis().ARM_NAMES[0] else 0)


def test_actor_marker_name_requires_an_exact_token_boundary(
    tmp_path: Path,
    profile: Any,
) -> None:
    module = _runtime()
    arm = _arm(profile, _diagnosis().ARM_NAMES[0])
    path = tmp_path / "logs" / "replica-0.log"
    path.parent.mkdir(parents=True)
    path.write_text(
        "KAURI_FAULT false_timeout_emitted_extra reporter=0 target=5 "
        f"epoch=0 tree=30 block={SELECTED_BLOCK} "
        f"window={profile.diagnostic_window} monotonic_ns=5600\n",
        encoding="utf-8",
    )

    assert (
        module._find_markers(
            profile,
            arm,
            tmp_path,
            start_offset=0,
        )
        == []
    )


def test_suppression_parser_rejects_a_reversed_terminal_offset(
    tmp_path: Path,
    profile: Any,
) -> None:
    module = _runtime()
    arm = _arm(profile, _diagnosis().ARM_NAMES[0])
    _write_actor_log(tmp_path, profile, arm.name)
    with pytest.raises(module.N31StaticDiagnosisRuntimeError, match="offset"):
        module._find_false_positive_suppressions(
            profile,
            arm,
            tmp_path,
            start_offset=10,
            end_offset=9,
        )


@pytest.mark.parametrize("arm_name", _diagnosis().ARM_NAMES)
def test_live_pair_is_receipt_order_independent_and_root_qc_bound(
    tmp_path: Path,
    profile: Any,
    arm_name: str,
) -> None:
    module = _runtime()
    arm = _arm(profile, arm_name)
    start = _write_actor_log(tmp_path, profile, arm_name)

    pair = module._live_signer_pair(
        profile,
        arm,
        tmp_path,
        start_offset=start,
        manager_events=_manager_pair(profile, arm_name),
        root_events=[_root_event(profile, arm_name)],
        boundary_max_ns=4030,
    )

    assert pair is not None
    marker, claim, witness = pair
    assert marker["block_hash"] == SELECTED_BLOCK
    assert claim["source_monotonic_ns"] == 6100
    assert witness["source_monotonic_ns"] == 6000
    assert (
        marker["false_positive_suppression"] is not None
        if (arm_name == _diagnosis().ARM_NAMES[0])
        else marker["false_positive_suppression"] is None
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("witness-after-qc", "after root QC"),
        ("suppression-after-witness", "suppression followed"),
        ("omission-compensation", "reappeared"),
    ),
)
def test_live_pair_rejects_impossible_or_compensating_chronology(
    tmp_path: Path,
    profile: Any,
    mutation: str,
    message: str,
) -> None:
    module = _runtime()
    arm_name = (
        _diagnosis().ARM_NAMES[0]
        if mutation == "suppression-after-witness"
        else _diagnosis().ARM_NAMES[1]
    )
    arm = _arm(profile, arm_name)
    start = _write_actor_log(tmp_path, profile, arm_name)
    manager_events = _manager_pair(profile, arm_name)
    root_events = [_root_event(profile, arm_name)]
    if mutation == "witness-after-qc":
        root_events[0]["source_monotonic_ns"] = 5300
    elif mutation == "suppression-after-witness":
        manager_events[0]["payload"]["observation"]["reporter_monotonic_ns"] = 4900
    else:
        progress = _root_event(
            profile,
            arm_name,
            timestamp=6200,
            event_type="aggregation.root_quorum_progress",
            accepted_signers=list(range(21)),
        )
        root_events.insert(0, progress)
    with pytest.raises(module.N31StaticDiagnosisRuntimeError, match=message):
        module._live_signer_pair(
            profile,
            arm,
            tmp_path,
            start_offset=start,
            manager_events=manager_events,
            root_events=root_events,
            boundary_max_ns=4030,
        )


def test_unrelated_root_event_is_filtered_before_selected_schema_validation(
    profile: Any,
) -> None:
    module = _runtime()
    arm_name = _diagnosis().ARM_NAMES[0]
    arm = _arm(profile, arm_name)
    unrelated = {
        "event_type": "aggregation.root_quorum_progress",
        "payload": {"epoch_number": 99},
    }
    selected = _root_event(profile, arm_name)

    assert (
        module._live_root_qc(
            profile,
            arm,
            [unrelated, selected],
            block_hash=SELECTED_BLOCK,
            boundary_max_ns=4030,
        )
        == selected
    )


@pytest.mark.parametrize(
    "mutation",
    ("mixed-signers", "auxiliary-list", "required-gap"),
)
def test_selected_root_payload_types_fail_closed_without_python_type_errors(
    profile: Any,
    mutation: str,
) -> None:
    module = _runtime()
    arm_name = _diagnosis().ARM_NAMES[0]
    arm = _arm(profile, arm_name)
    event = _root_event(profile, arm_name)
    if mutation == "mixed-signers":
        event["payload"]["accepted_signers"] = [0, {}]
        event["payload"]["root_signer_count"] = 2
    elif mutation == "auxiliary-list":
        event["payload"]["missing_optional_signers"] = ["5"]
    else:
        event["payload"]["required_branch_gaps"] = [
            {"direct_child": 0, "missing_required_signers": []}
        ]
    with pytest.raises(module.N31StaticDiagnosisRuntimeError, match="root"):
        module._live_root_qc(
            profile,
            arm,
            [event],
            block_hash=SELECTED_BLOCK,
            boundary_max_ns=4030,
        )


def test_live_later_commit_requires_replica2_full_parent_chain(profile: Any) -> None:
    module = _runtime()
    support = _support()
    evidence = support._valid_evidence(profile, _diagnosis().ARM_NAMES[0])
    profiled = module.load_runtime_profile(RUNTIME_PROFILE)
    baseline = {
        "block_height": 100,
        "block_hash": support.BASELINE_HASH,
    }

    later = module._find_later_ancestry_commit(
        profile,
        profiled,
        evidence["streams"],
        witnesses=profile.commit_witnesses,
        baseline=baseline,
        after_ns=6500,
    )
    assert later is not None
    assert later["block_hash"] == support.LATER_HASH

    broken = deepcopy(evidence["streams"])
    middle = next(
        event
        for event in broken["replica-2"]
        if event["event_type"] == "block.commit_observed"
        and event["payload"]["block_hash"] == support.MIDDLE_HASH
    )
    middle["payload"]["parent_hash"] = "e" * 64
    assert (
        module._find_later_ancestry_commit(
            profile,
            profiled,
            broken,
            witnesses=profile.commit_witnesses,
            baseline=baseline,
            after_ns=6500,
        )
        is None
    )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, values: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n"
            for value in values
        ),
        encoding="utf-8",
    )


def _epoch_zero_witness(profile: Any) -> dict[str, object]:
    members = list(profile.replica_ids)
    trees = [
        {
            "tree_id": root,
            "fanout": profile.fanout,
            "pipeline_stretch": profile.pipeline_stretch,
            "members_breadth_first": members[root:] + members[:root],
            "wait_exempt_leaves": [],
        }
        for root in profile.replica_ids
    ]
    return {
        "schema": "kauri-adaptive-v2-epoch-profile-digest-v1",
        "replica_count": 31,
        "fault_threshold": 10,
        "quorum": 21,
        "fanout": 5,
        "pipeline_stretch": 2,
        "membership": members,
        "epoch_zero": {
            "schema_version": 2,
            "epoch_number": 0,
            "previous_epoch_digest": "0" * 64,
            "membership_digest": (
                "107f6e39481091529f50db1c6e32a725" "18cf50a971b074506bed095cb09769d9"
            ),
            "activation_height": 0,
            "generation_seed": 0,
            "policy_version": "adaptive-v2-bootstrap",
            "evidence_snapshot_id": "adaptive-v2-bootstrap-epoch-zero",
            "evidence_cutoff": 0,
            "canonical_size_bytes": 2720,
            "epoch_digest": profile.epoch_digest,
            "tree_count": 31,
            "trees": trees,
        },
    }


def _build_provenance() -> dict[str, object]:
    metadata_names = (
        "cmake_cache",
        "compile_commands",
        "hotstuff_app_link",
        "adaptation_manager_link",
        "epoch_profile_digest_link",
        "hotstuff_keygen_link",
        "hotstuff_tls_keygen_link",
    )
    binary_names = (
        "app",
        "manager",
        "keygen",
        "tls_keygen",
        "epoch_profile_digest",
    )
    return {
        "schema_version": 1,
        "revision": REVISION,
        "repository": "/synthetic/kauri",
        "build_directory": "/synthetic/kauri/build-adaptive",
        "cmake_cache_sha256": "c" * 64,
        "build_command": ["cmake", "--build", "build-adaptive"],
        "build_metadata": {
            name: {
                "path": f"/synthetic/build/{name}",
                "sha256": f"{index + 1:x}" * 64,
                "size_bytes": 100 + index,
            }
            for index, name in enumerate(metadata_names)
        },
        "binaries": {
            name: {
                "path": f"/synthetic/bin/{name}",
                "sha256": f"{index + 8:x}" * 64,
                "size_bytes": 1000 + index,
            }
            for index, name in enumerate(binary_names)
        },
    }


def _trusted_receipt(
    provenance: dict[str, object] | None = None,
):
    module = _runtime()
    provenance = deepcopy(provenance) if provenance is not None else _build_provenance()
    document_sha256 = module._canonical_document_sha256(provenance)
    return module.TrustedProvenance(
        revision=REVISION,
        required_branch=module.runtime.REQUIRED_BRANCH,
        remote_tracking_ref=f"origin/{module.runtime.REQUIRED_BRANCH}",
        repository_clean=True,
        head_equals_remote=True,
        repository=str(provenance["repository"]),
        build_directory=str(provenance["build_directory"]),
        build_provenance_file_sha256=document_sha256,
        build_provenance_document_sha256=document_sha256,
        binaries=tuple(
            module.TrustedBinary(
                name=name,
                path=value["path"],
                size_bytes=value["size_bytes"],
                sha256=value["sha256"],
            )
            for name, value in sorted(provenance["binaries"].items())
        ),
    )


def _artifact(
    module: Any,
    run_directory: Path,
    *,
    kind: str,
    replica_id: int | None,
    relative: str,
) -> dict[str, object]:
    return {
        "kind": kind,
        "replica_id": replica_id,
        "path": relative,
        "sha256": module.runtime.sha256_file(run_directory / relative),
    }


def _reseal(module: Any, run_directory: Path) -> None:
    (run_directory / "evidence-seal.json").unlink(missing_ok=True)
    module.create_evidence_seal(run_directory)


def test_hashed_inventory_and_seal_reject_symlinked_parent_directory(
    tmp_path: Path,
) -> None:
    module = _runtime()
    run_directory = tmp_path / "run"
    external = tmp_path / "external"
    run_directory.mkdir()
    external.mkdir()
    (external / "replica.conf").write_text("external\n", encoding="utf-8")
    (run_directory / "config").symlink_to(external, target_is_directory=True)
    descriptor = {
        "kind": "replica_config",
        "replica_id": 0,
        "path": "config/replica.conf",
        "sha256": module.runtime.sha256_file(external / "replica.conf"),
    }

    with pytest.raises(module.N31StaticDiagnosisRuntimeError, match="symlink"):
        module._verify_hashed_file(
            run_directory,
            descriptor,
            expected_keys={"kind", "replica_id", "path", "sha256"},
            label="test artifact",
        )
    with pytest.raises(module.EvidenceSealError, match="symlink"):
        module.create_evidence_seal(run_directory)


def _preserved_run(tmp_path: Path, arm_name: str) -> Path:
    module = _runtime()
    support = _support()
    diagnosis = _diagnosis()
    profile = diagnosis.load_frozen_profile(DIAGNOSIS_PROFILE)
    profiled = module.load_runtime_profile(RUNTIME_PROFILE)
    evidence = deepcopy(support._valid_evidence(profile, arm_name))
    manifest = evidence["manifest"]
    streams = evidence["streams"]
    fault_plan = evidence["fault_plan"]
    journal = evidence["fault_journal"]
    actor_log = evidence["actor_log"]
    run_directory = tmp_path / str(manifest["run_id"])
    for directory in ("config", "runtime", "raw", "logs"):
        (run_directory / directory).mkdir(parents=True, exist_ok=True)

    (run_directory / "profile.json").write_bytes(DIAGNOSIS_PROFILE.read_bytes())
    (run_directory / "runtime-profile.json").write_bytes(RUNTIME_PROFILE.read_bytes())
    (run_directory / "fault-plan.json").write_bytes(fault_plan)
    for source_id, events in streams.items():
        _write_jsonl(run_directory / "raw" / f"{source_id}.jsonl", events)
    _write_jsonl(run_directory / "raw/fault-orchestrator.jsonl", journal)
    actor = _arm(profile, arm_name)
    actor_path = run_directory / "logs" / f"replica-{actor.actor_replica_id}.log"
    actor_path.write_bytes(actor_log)

    for name in (
        "bls-identities.txt",
        "tls-identities.txt",
        "issuer-identities.txt",
        "main.conf",
    ):
        (run_directory / "config" / name).write_text(
            f"synthetic {name}\n", encoding="utf-8"
        )
    for replica_id in profile.replica_ids:
        (run_directory / "config" / f"replica-{replica_id}.conf").write_text(
            f"replica = {replica_id}\n", encoding="utf-8"
        )
    for name, value in (
        ("initial-epoch.json", {"epoch_number": 0}),
        ("effective-runtime.json", {"replica_count": 31, "quorum": 21}),
        ("transition-request.json", {"disabled": True}),
    ):
        _write_json(run_directory / "runtime" / name, value)

    provenance = _build_provenance()
    epoch_witness = _epoch_zero_witness(profile)
    _write_json(run_directory / "runtime/build-provenance.json", provenance)
    _write_json(run_directory / "runtime/epoch-zero-witness.json", epoch_witness)
    launch_contracts = {
        arm_name_value: diagnosis.build_launch_contract(
            profile,
            arm=arm_name_value,
            kauri_revision=REVISION,
        )
        for arm_name_value in diagnosis.ARM_NAMES
    }
    selected_contract = launch_contracts[arm_name]
    _write_json(run_directory / "runtime/launch-contract.json", selected_contract)
    base_commands = module.runtime.replica_argvs(
        profiled,
        app_binary=Path(provenance["binaries"]["app"]["path"]),
        config_directory=run_directory / "config",
    )
    replica_commands = [
        [
            *base_commands[overlay["replica_id"]],
            *overlay["argv"],
        ]
        for overlay in selected_contract["replica_overlays"]
    ]
    _write_json(
        run_directory / "runtime/launch-arguments.json",
        {
            "schema_version": 1,
            "manager": module._expected_manager_launch(
                run_directory,
                profile,
                profiled,
                manifest,
                executable=provenance["binaries"]["manager"]["path"],
            ),
            "replicas": replica_commands,
        },
    )

    artifact_specs = [
        ("diagnosis_profile", None, "profile.json"),
        ("runtime_profile", None, "runtime-profile.json"),
        ("bls_identity_input", None, "config/bls-identities.txt"),
        ("tls_identity_input", None, "config/tls-identities.txt"),
        ("issuer_identity_input", None, "config/issuer-identities.txt"),
        ("main_config", None, "config/main.conf"),
        *(
            ("replica_config", replica_id, f"config/replica-{replica_id}.conf")
            for replica_id in profile.replica_ids
        ),
        ("initial_epoch", None, "runtime/initial-epoch.json"),
        ("effective_runtime", None, "runtime/effective-runtime.json"),
        ("transition_request", None, "runtime/transition-request.json"),
        ("launch_arguments", None, "runtime/launch-arguments.json"),
        ("build_provenance", None, "runtime/build-provenance.json"),
        ("epoch_zero_witness", None, "runtime/epoch-zero-witness.json"),
        ("launch_contract", None, "runtime/launch-contract.json"),
    ]
    runtime_artifacts = [
        _artifact(
            module,
            run_directory,
            kind=kind,
            replica_id=replica_id,
            relative=relative,
        )
        for kind, replica_id, relative in artifact_specs
    ]
    cleanup_by_name = {value["name"]: value for value in manifest["cleanup_ledger"]}
    ordered_sources = [
        *(f"replica-{replica}" for replica in profile.replica_ids),
        "adaptive-manager",
    ]
    sources = []
    for source_id in ordered_sources:
        cleanup = cleanup_by_name[source_id]
        relative = f"raw/{source_id}.jsonl"
        sources.append(
            {
                "source_kind": (
                    "replica"
                    if source_id.startswith("replica-")
                    else "adaptation_manager"
                ),
                "source_id": source_id,
                "source_instance": manifest["source_instances"][source_id],
                "pid": cleanup["pid"],
                "pgid": cleanup["pgid"],
                "path": relative,
                "sha256": module.runtime.sha256_file(run_directory / relative),
            }
        )
    profiled_preflight = {
        "schema_version": 1,
        "verdict": "PASS",
        "profile_id": profile.runtime_profile_id,
        "profile_sha256": profile.runtime_profile_sha256,
        "revision": REVISION,
        "required_port_count": 63,
        "fd_soft_limit": 1024,
        "free_disk_bytes": 1024**3,
        "clock": "CLOCK_MONOTONIC_RAW",
        "build_provenance": provenance,
        "epoch_zero_witness": epoch_witness,
        "executables": {
            name: {"path": value["path"], "sha256": value["sha256"]}
            for name, value in provenance["binaries"].items()
        },
    }
    manifest.update(
        {
            "preflight": {
                "schema_version": 1,
                "scenario": diagnosis.SCENARIO,
                "verdict": "PASS",
                "diagnosis_profile": {
                    "profile_id": profile.profile_id,
                    "sha256": profile.profile_sha256,
                },
                "runtime_profile": {
                    "profile_id": profile.runtime_profile_id,
                    "sha256": profile.runtime_profile_sha256,
                    "path": str(RUNTIME_PROFILE.resolve()),
                },
                "revision": REVISION,
                "commit_witnesses": list(profile.commit_witnesses),
                "launch_contracts": launch_contracts,
                "profiled_runtime": profiled_preflight,
            },
            "runtime_artifacts": runtime_artifacts,
            "sources": sources,
            "actor_log": {
                "path": actor_path.relative_to(run_directory).as_posix(),
                "sha256": module.runtime.sha256_file(actor_path),
            },
        }
    )
    _write_json(run_directory / "manifest.json", manifest)
    validation = diagnosis.validate_n31_static_diagnosis_run(
        profile,
        manifest=manifest,
        streams=streams,
        fault_plan=fault_plan,
        fault_journal=journal,
        actor_log=actor_log,
    )
    _write_json(run_directory / "validation.json", validation)
    module.create_evidence_seal(run_directory)
    return run_directory


@pytest.mark.parametrize("arm_name", _diagnosis().ARM_NAMES)
def test_preserved_run_reconstructs_pass_from_sealed_raw_sources(
    tmp_path: Path,
    arm_name: str,
) -> None:
    run_directory = _preserved_run(tmp_path, arm_name)

    result = _runtime().validate_preserved_run(
        run_directory,
        trusted_provenance=_trusted_receipt(),
    )

    assert result["verdict"] == "PASS"
    assert result["arm"] == arm_name
    assert result["figure_eligible"] is False
    assert result["evidence_ceiling"] == "harness_validation_only"
    assert len(result["evidence_tree_sha256"]) == 64
    assert len(result["evidence_seal_sha256"]) == 64


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("unsealed-raw", "evidence seal"),
        ("resealed-root-qc", "semantic evidence|root QC|leaf 5"),
        ("missing-actor-overlay", "actor overlay|launch|replica command"),
        ("forged-contract", "frozen profile"),
        ("forged-manager", "manager command"),
        ("forged-revision", "revision|trusted"),
        ("forged-binary-hashes", "trusted|executable|provenance"),
        ("forged-binary-path-argv", "trusted|executable|provenance"),
        ("zero-start", "process.started"),
        ("duplicate-cleanup", "cleanup inventory"),
        ("attempt", "attempt|retry"),
    ),
)
def test_preserved_validation_rejects_tamper_even_when_resealed(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    module = _runtime()
    run_directory = _preserved_run(tmp_path, _diagnosis().ARM_NAMES[1])
    manifest_path = run_directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    def update_artifact(relative: str) -> None:
        artifact = next(
            value
            for value in manifest["runtime_artifacts"]
            if value["path"] == relative
        )
        artifact["sha256"] = module.runtime.sha256_file(run_directory / relative)

    if mutation == "unsealed-raw":
        with (run_directory / "raw/replica-30.jsonl").open(
            "a", encoding="utf-8"
        ) as output:
            output.write("{}\n")
    elif mutation == "resealed-root-qc":
        path = run_directory / "raw/replica-30.jsonl"
        events = [json.loads(line) for line in path.read_text().splitlines()]
        qc = next(
            event
            for event in events
            if event["event_type"] == "aggregation.root_qc_published"
        )
        qc["payload"]["accepted_signers"][-1] = 5
        qc["payload"]["accepted_signers"].sort()
        _write_jsonl(path, events)
        descriptor = next(
            value for value in manifest["sources"] if value["source_id"] == "replica-30"
        )
        descriptor["sha256"] = module.runtime.sha256_file(path)
        _write_json(manifest_path, manifest)
        _reseal(module, run_directory)
    elif mutation == "missing-actor-overlay":
        path = run_directory / "runtime/launch-arguments.json"
        launch = json.loads(path.read_text(encoding="utf-8"))
        actor = _arm(
            _diagnosis().load_frozen_profile(DIAGNOSIS_PROFILE),
            str(manifest["arm"]),
        )
        overlay = manifest["preflight"]["launch_contracts"][manifest["arm"]][
            "replica_overlays"
        ][actor.actor_replica_id]["argv"]
        del launch["replicas"][actor.actor_replica_id][-len(overlay) :]
        _write_json(path, launch)
        artifact = next(
            value
            for value in manifest["runtime_artifacts"]
            if value["path"] == "runtime/launch-arguments.json"
        )
        artifact["sha256"] = module.runtime.sha256_file(path)
        _write_json(manifest_path, manifest)
        _reseal(module, run_directory)
    elif mutation == "forged-contract":
        contract = manifest["preflight"]["launch_contracts"][manifest["arm"]]
        contract["evidence_policy"]["figure_eligibility"] = "pilot_allowed"
        path = run_directory / "runtime/launch-contract.json"
        _write_json(path, contract)
        artifact = next(
            value
            for value in manifest["runtime_artifacts"]
            if value["path"] == "runtime/launch-contract.json"
        )
        artifact["sha256"] = module.runtime.sha256_file(path)
        _write_json(manifest_path, manifest)
        _reseal(module, run_directory)
    elif mutation == "forged-manager":
        path = run_directory / "runtime/launch-arguments.json"
        launch = json.loads(path.read_text(encoding="utf-8"))
        launch["manager"].extend(("--forged", "true"))
        _write_json(path, launch)
        artifact = next(
            value
            for value in manifest["runtime_artifacts"]
            if value["path"] == "runtime/launch-arguments.json"
        )
        artifact["sha256"] = module.runtime.sha256_file(path)
        _write_json(manifest_path, manifest)
        _reseal(module, run_directory)
    elif mutation == "forged-revision":
        forged_revision = "e" * 40
        profile = _diagnosis().load_frozen_profile(DIAGNOSIS_PROFILE)
        manifest["kauri_revision"] = forged_revision
        preflight = manifest["preflight"]
        preflight["revision"] = forged_revision
        profiled = preflight["profiled_runtime"]
        profiled["revision"] = forged_revision
        provenance = profiled["build_provenance"]
        provenance["revision"] = forged_revision
        preflight["launch_contracts"] = {
            arm_name: _diagnosis().build_launch_contract(
                profile,
                arm=arm_name,
                kauri_revision=forged_revision,
            )
            for arm_name in _diagnosis().ARM_NAMES
        }
        _write_json(
            run_directory / "runtime/build-provenance.json",
            provenance,
        )
        _write_json(
            run_directory / "runtime/launch-contract.json",
            preflight["launch_contracts"][manifest["arm"]],
        )
        update_artifact("runtime/build-provenance.json")
        update_artifact("runtime/launch-contract.json")
        _write_json(manifest_path, manifest)
        _reseal(module, run_directory)
    elif mutation == "forged-binary-hashes":
        profiled = manifest["preflight"]["profiled_runtime"]
        provenance = profiled["build_provenance"]
        for name, binary in provenance["binaries"].items():
            binary["sha256"] = "e" * 64
            profiled["executables"][name]["sha256"] = "e" * 64
        _write_json(
            run_directory / "runtime/build-provenance.json",
            provenance,
        )
        update_artifact("runtime/build-provenance.json")
        _write_json(manifest_path, manifest)
        _reseal(module, run_directory)
    elif mutation == "forged-binary-path-argv":
        forged_path = "/forged/hotstuff-app"
        profiled = manifest["preflight"]["profiled_runtime"]
        provenance = profiled["build_provenance"]
        provenance["binaries"]["app"]["path"] = forged_path
        profiled["executables"]["app"]["path"] = forged_path
        launch_path = run_directory / "runtime/launch-arguments.json"
        launch = json.loads(launch_path.read_text(encoding="utf-8"))
        for command in launch["replicas"]:
            command[0] = forged_path
        _write_json(
            run_directory / "runtime/build-provenance.json",
            provenance,
        )
        _write_json(launch_path, launch)
        update_artifact("runtime/build-provenance.json")
        update_artifact("runtime/launch-arguments.json")
        _write_json(manifest_path, manifest)
        _reseal(module, run_directory)
    elif mutation == "zero-start":
        source_id = "replica-29"
        path = run_directory / "raw" / f"{source_id}.jsonl"
        events = [json.loads(line) for line in path.read_text().splitlines()]
        events = [event for event in events if event["event_type"] != "process.started"]
        for sequence, event in enumerate(events, start=1):
            event["source_sequence"] = sequence
        _write_jsonl(path, events)
        descriptor = next(
            value for value in manifest["sources"] if value["source_id"] == source_id
        )
        descriptor["sha256"] = module.runtime.sha256_file(path)
        boundary = manifest["configuration_boundaries"]["diagnostic"]
        reference = next(
            value
            for value in boundary["replica_evidence"]
            if value["source_id"] == source_id
        )
        reference["source_sequence"] -= 1
        _write_json(manifest_path, manifest)
        _reseal(module, run_directory)
    elif mutation == "duplicate-cleanup":
        manifest["cleanup_ledger"].append(deepcopy(manifest["cleanup_ledger"][0]))
        _write_json(manifest_path, manifest)
        _reseal(module, run_directory)
    else:
        manifest["attempt"] = 2
        _write_json(manifest_path, manifest)
        _reseal(module, run_directory)

    with pytest.raises(module.N31StaticDiagnosisRuntimeError, match=message):
        module.validate_preserved_run(
            run_directory,
            trusted_provenance=_trusted_receipt(),
        )


@pytest.mark.parametrize("error", (TypeError("bad type"), KeyError("missing")))
def test_run_once_preserves_and_seals_malformed_internal_failure_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: Exception,
) -> None:
    module = _runtime()
    diagnosis = _diagnosis()
    profile = diagnosis.load_frozen_profile(DIAGNOSIS_PROFILE)
    repository = Path(__file__).parents[3].resolve()
    build_directory = (tmp_path / "build").resolve()
    binaries = {
        "app": (tmp_path / "hotstuff-app").resolve(),
        "manager": (tmp_path / "adaptation-manager").resolve(),
        "keygen": (tmp_path / "hotstuff-keygen").resolve(),
        "tls_keygen": (tmp_path / "hotstuff-tls-keygen").resolve(),
        "epoch_profile_digest": (tmp_path / "epoch-profile-digest").resolve(),
    }
    provenance = _build_provenance()
    provenance["repository"] = str(repository)
    provenance["build_directory"] = str(build_directory)
    for name, path in binaries.items():
        provenance["binaries"][name]["path"] = str(path)
    trusted = _trusted_receipt(provenance)
    provenance_path = build_directory / "build-provenance.json"
    _write_json(provenance_path, provenance)
    calls = 0

    def fake_preflight(**_kwargs: object) -> dict[str, object]:
        return {
            "revision": REVISION,
            "profiled_runtime": {
                "executables": {
                    binary.name: {
                        "path": binary.path,
                        "sha256": binary.sha256,
                    }
                    for binary in trusted.binaries
                },
                "build_provenance": provenance,
            },
            "launch_contracts": {
                arm_name: diagnosis.build_launch_contract(
                    profile,
                    arm=arm_name,
                    kauri_revision=REVISION,
                )
                for arm_name in diagnosis.ARM_NAMES
            },
        }

    def malformed_identity_generation(*_args: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr(module, "preflight", fake_preflight)
    monkeypatch.setattr(
        module.runtime,
        "generate_identities",
        malformed_identity_generation,
    )
    monkeypatch.setattr(module.runtime, "wait_ports_clear", lambda *_args: None)

    run_directory, verdict = module.run_once(
        diagnosis_profile_path=DIAGNOSIS_PROFILE,
        arm=diagnosis.ARM_NAMES[0],
        trusted_provenance=trusted,
        repository=repository,
        results_root=tmp_path / "results",
        app_binary=binaries["app"],
        manager_binary=binaries["manager"],
        keygen_binary=binaries["keygen"],
        tls_keygen_binary=binaries["tls_keygen"],
        epoch_profile_digest_binary=binaries["epoch_profile_digest"],
        build_directory=build_directory,
        build_provenance_path=provenance_path,
    )

    assert verdict == "INCOMPLETE"
    assert calls == 1
    validation = json.loads(
        (run_directory / "validation.json").read_text(encoding="utf-8")
    )
    assert validation["verdict"] == "INCOMPLETE"
    assert "bad type" in validation["error"] or "missing" in validation["error"]
    module.verify_evidence_seal(run_directory)


def _run_arguments(tmp_path: Path, *, arm: str | None = None) -> list[str]:
    values = [
        "run",
        "--profile",
        str(tmp_path / "profile.json"),
        "--repository",
        str(tmp_path / "repository"),
        "--results-root",
        str(tmp_path / "results"),
        "--build-directory",
        str(tmp_path / "repository/build-adaptive"),
        "--app-binary",
        str(tmp_path / "hotstuff-app"),
        "--manager-binary",
        str(tmp_path / "adaptation-manager"),
        "--keygen-binary",
        str(tmp_path / "hotstuff-keygen"),
        "--tls-keygen-binary",
        str(tmp_path / "hotstuff-tls-keygen"),
        "--epoch-profile-digest-binary",
        str(tmp_path / "epoch-profile-digest"),
        "--trusted-provenance",
        str(tmp_path / "trusted-provenance.json"),
    ]
    if arm is not None:
        values.extend(("--arm", arm))
    return values


def test_trusted_receipt_derivation_roundtrip_rechecks_repository(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _runtime()
    repository = (tmp_path / "repository").resolve()
    build_directory = (repository / "build-adaptive").resolve()
    build_directory.mkdir(parents=True)
    binaries = {
        "app": build_directory / "examples/hotstuff-app",
        "manager": build_directory / "examples/adaptation-manager",
        "keygen": build_directory / "hotstuff-keygen",
        "tls_keygen": build_directory / "hotstuff-tls-keygen",
        "epoch_profile_digest": build_directory / "examples/epoch-profile-digest",
    }
    provenance = _build_provenance()
    provenance["repository"] = str(repository)
    provenance["build_directory"] = str(build_directory)
    for name, path in binaries.items():
        provenance["binaries"][name]["path"] = str(path)
    provenance_path = build_directory / module.runtime.BUILD_PROVENANCE_FILENAME
    _write_json(provenance_path, provenance)
    repository_checks: list[Path] = []

    def verify_repository(path: Path) -> str:
        repository_checks.append(path)
        return REVISION

    def verify_provenance(**kwargs: object) -> dict[str, object]:
        assert kwargs["repository"] == repository
        assert kwargs["build_directory"] == build_directory
        assert kwargs["provenance_path"] == provenance_path
        assert kwargs["binaries"] == binaries
        return provenance

    monkeypatch.setattr(module.runtime, "verify_repository_state", verify_repository)
    monkeypatch.setattr(
        module.runtime,
        "verify_exact_build_provenance",
        verify_provenance,
    )
    receipt = module.derive_trusted_provenance(
        repository=repository,
        app_binary=binaries["app"],
        manager_binary=binaries["manager"],
        keygen_binary=binaries["keygen"],
        tls_keygen_binary=binaries["tls_keygen"],
        epoch_profile_digest_binary=binaries["epoch_profile_digest"],
        build_directory=build_directory,
        build_provenance_path=provenance_path,
    )
    assert repository_checks == [repository, repository]
    receipt_path = tmp_path / "trusted-provenance.json"
    assert module.write_trusted_provenance(receipt_path, receipt) == receipt.sha256
    assert module.load_trusted_provenance(receipt_path) == receipt


def test_cli_preflight_builds_once_without_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _runner()
    calls = {"build": 0, "preflight": 0}

    def prepare(**_kwargs: object) -> None:
        calls["build"] += 1

    def preflight(**_kwargs: object) -> dict[str, object]:
        calls["preflight"] += 1
        return {"verdict": "PASS"}

    monkeypatch.setattr(
        runner.profiled_fault_runtime, "prepare_exact_revision_build", prepare
    )
    monkeypatch.setattr(
        runner.runtime,
        "derive_trusted_provenance",
        lambda **_kwargs: _trusted_receipt(),
    )
    monkeypatch.setattr(runner.runtime, "preflight", preflight)
    argv = _run_arguments(tmp_path)
    argv[0] = "preflight"
    assert runner.main(argv) == 0
    assert calls == {"build": 1, "preflight": 1}


@pytest.mark.parametrize(("verdict", "exit_code"), (("PASS", 0), ("FAIL", 1)))
def test_cli_run_invokes_exactly_one_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    verdict: str,
    exit_code: int,
) -> None:
    runner = _runner()
    calls = {"build": 0, "run": 0}
    monkeypatch.setattr(
        runner.profiled_fault_runtime,
        "prepare_exact_revision_build",
        lambda **_kwargs: calls.__setitem__("build", calls["build"] + 1),
    )
    monkeypatch.setattr(
        runner.runtime,
        "derive_trusted_provenance",
        lambda **_kwargs: _trusted_receipt(),
    )

    def run_once(**_kwargs: object) -> tuple[Path, str]:
        calls["run"] += 1
        return tmp_path / "one-attempt", verdict

    monkeypatch.setattr(runner.runtime, "run_once", run_once)
    assert (
        runner.main(_run_arguments(tmp_path, arm=_diagnosis().ARM_NAMES[0]))
        == exit_code
    )
    assert calls == {"build": 1, "run": 1}


def test_cli_validate_is_source_blind_and_does_not_build(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _runner()
    called = {"validate": 0}
    trusted = _trusted_receipt()
    trusted_path = tmp_path / "trusted-provenance.json"
    runner.runtime.write_trusted_provenance(trusted_path, trusted)

    def reject_build(**_kwargs: object) -> None:
        raise AssertionError("validate must not build")

    def validate(
        path: Path,
        *,
        trusted_provenance: Any,
    ) -> dict[str, object]:
        called["validate"] += 1
        assert path == (tmp_path / "sealed").resolve()
        assert trusted_provenance == trusted
        return {"verdict": "PASS"}

    monkeypatch.setattr(
        runner.profiled_fault_runtime,
        "prepare_exact_revision_build",
        reject_build,
    )
    monkeypatch.setattr(
        runner.runtime,
        "derive_trusted_provenance",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("validate must not inspect repository or binaries")
        ),
    )
    monkeypatch.setattr(runner.runtime, "validate_preserved_run", validate)
    assert (
        runner.main(
            [
                "validate",
                "--run-directory",
                str(tmp_path / "sealed"),
                "--trusted-provenance",
                str(trusted_path),
            ]
        )
        == 0
    )
    assert called == {"validate": 1}


def test_cli_validate_rejects_receipt_inside_run_directory(tmp_path: Path) -> None:
    runner = _runner()
    run_directory = tmp_path / "sealed"
    assert (
        runner.main(
            [
                "validate",
                "--run-directory",
                str(run_directory),
                "--trusted-provenance",
                str(run_directory / "receipt.json"),
            ]
        )
        == 2
    )


def test_cli_rejection_is_exit_two_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _runner()
    calls = 0
    monkeypatch.setattr(
        runner.profiled_fault_runtime,
        "prepare_exact_revision_build",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        runner.runtime,
        "derive_trusted_provenance",
        lambda **_kwargs: _trusted_receipt(),
    )

    def reject(**_kwargs: object) -> tuple[Path, str]:
        nonlocal calls
        calls += 1
        raise runner.runtime.N31StaticDiagnosisRuntimeError("synthetic rejection")

    monkeypatch.setattr(runner.runtime, "run_once", reject)
    assert runner.main(_run_arguments(tmp_path, arm=_diagnosis().ARM_NAMES[1])) == 2
    assert calls == 1
