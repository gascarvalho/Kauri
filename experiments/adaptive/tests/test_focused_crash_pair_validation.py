"""Red-first source-blind validation contracts for focused crash pairs."""

from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import asdict, is_dataclass
import hashlib
import importlib
import inspect
import json
from pathlib import Path
import shutil
from typing import Any, Mapping

import pytest

from experiments.adaptive.tests import test_run_n31_crash_pair_campaign as fixture
from experiments.adaptive.tests import test_n31_crash_pair_contract as native_fixture
from experiments.adaptive.tests import test_focused_crash_pair_runtime as runtime_fixture


VALIDATION = "experiments.adaptive.kauri_experiment.focused_crash_pair_validation"


def _validation() -> Any:
    return importlib.import_module(VALIDATION)


def _document(value: object) -> dict[str, Any]:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    assert isinstance(value, dict)
    return value


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _reseal(path: Path) -> None:
    (path / "evidence-seal.json").unlink(missing_ok=True)
    fixture._archive().create_evidence_seal(path)


def _issuer_public_key(private_key: int) -> str:
    point = native_fixture.factorial_validation._secp256k1_multiply(
        private_key,
        (
            native_fixture.factorial_validation._SECP256K1_GX,
            native_fixture.factorial_validation._SECP256K1_GY,
        ),
    )
    assert point is not None
    return ("02" if point[1] % 2 == 0 else "03") + f"{point[0]:064x}"


def _resign_native_bundle(decoded: object, private_key: int) -> tuple[bytes, object]:
    trees = [asdict(tree) for tree in decoded.trees]  # type: ignore[attr-defined]
    canonical = native_fixture._epoch_canonical_bytes(
        decoded.epoch_number,  # type: ignore[attr-defined]
        decoded.previous_epoch_digest,  # type: ignore[attr-defined]
        decoded.generation_seed,  # type: ignore[attr-defined]
        decoded.policy_version,  # type: ignore[attr-defined]
        trees,
        evidence_snapshot_id=decoded.evidence_snapshot_id,  # type: ignore[attr-defined]
        evidence_cutoff=decoded.evidence_cutoff,  # type: ignore[attr-defined]
    )
    successor_digest = hashlib.sha256(canonical).hexdigest()
    command = decoded.command  # type: ignore[attr-defined]
    signing_bytes = b"".join(
        (
            b"kauri-authorized-epoch-change-v1",
            native_fixture._u(1, 4),
            native_fixture._u(2, 1),
            native_fixture._u(command.issuer_id, 4),
            native_fixture._u(decoded.epoch_number, 4),  # type: ignore[attr-defined]
            bytes.fromhex(decoded.previous_epoch_digest),  # type: ignore[attr-defined]
            bytes.fromhex(successor_digest),
            native_fixture._u(command.activation_delay_blocks, 8),
        )
    )
    nonce = decoded.epoch_number + 17  # type: ignore[attr-defined]
    point = native_fixture.factorial_validation._secp256k1_multiply(
        nonce,
        (
            native_fixture.factorial_validation._SECP256K1_GX,
            native_fixture.factorial_validation._SECP256K1_GY,
        ),
    )
    assert point is not None
    order = native_fixture.factorial_validation._SECP256K1_ORDER
    r = point[0] % order
    z = int.from_bytes(hashlib.sha256(signing_bytes).digest(), "big")
    s = (pow(nonce, -1, order) * (z + r * private_key)) % order
    if s > order // 2:
        s = order - s
    signed_command = signing_bytes + r.to_bytes(32, "big") + s.to_bytes(32, "big")
    definition = b"".join(
        (
            native_fixture._u(2, 4),
            native_fixture._u(2, 1),
            native_fixture._u(6, 1),
            bytes.fromhex(successor_digest),
            canonical[len(b"kauri-epoch-definition-v2") :],
        )
    )
    wire = b"".join(
        (
            b"kauri-adaptive-v2-epoch-change-bundle-v1",
            native_fixture._u(1, 4),
            native_fixture._u(2, 1),
            native_fixture._component(signed_command),
            native_fixture._component(definition),
        )
    )
    public_key = _issuer_public_key(private_key)
    rebound = native_fixture.factorial_validation.decode_epoch_change_bundle(
        wire,
        issuer_public_key=public_key,
    )
    assert rebound.epoch_digest == decoded.epoch_digest  # type: ignore[attr-defined]
    return wire, rebound


def _resign_child_with_independent_issuer(directory: Path) -> str:
    private_key = 2
    public_key = _issuer_public_key(private_key)
    rebound: dict[int, object] = {}
    for epoch in (1, 2):
        path = directory / "raw" / f"epoch{epoch}.bundle"
        decoded = native_fixture.factorial_validation.decode_epoch_change_bundle(
            path.read_bytes(),
            issuer_public_key=native_fixture.ISSUER_PUBLIC_KEY,
        )
        wire, rebound[epoch] = _resign_native_bundle(decoded, private_key)
        path.write_bytes(wire)
    (directory / "raw" / "issuer-public-key.txt").write_text(
        public_key + "\n",
        encoding="ascii",
    )
    events = _load_events(directory)
    for event in events:
        if event["event_type"] != "epoch.command_committed":
            continue
        payload = event["payload"]
        epoch = int(payload["successor_epoch_number"])
        payload = fixture._command_payload(
            rebound[epoch], int(payload["command_block_height"])
        )
        event["payload"] = payload
    _write_events(directory, events)
    for relative, key in (
        ("runtime/launch-arguments.json", "manager_argv"),
        ("runtime/manager-observed-argv.json", "argv"),
    ):
        path = directory / relative
        document = json.loads(path.read_text(encoding="utf-8"))
        arguments = document[key]
        arguments[arguments.index("--issuer-private-key") + 1] = f"{private_key:064x}"
        _write_json(path, document)
    manager_input_path = directory / "runtime" / "manager-input.json"
    manager_input = json.loads(manager_input_path.read_text(encoding="utf-8"))
    for key in ("requested_argv", "observed_argv"):
        arguments = manager_input[key]
        arguments[arguments.index("--issuer-private-key") + 1] = f"{private_key:064x}"
    _write_json(manager_input_path, manager_input)
    _reseal(directory)
    return public_key


def _complete_child(child: dict[str, object]) -> None:
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    (directory / "evidence-seal.json").unlink(missing_ok=True)
    profile = json.loads(runtime_fixture.N31_PROFILE.read_text(encoding="utf-8"))
    proof_source = runtime_fixture._topology_proof_path(
        runtime_fixture.N31_PROFILE,
        profile,
    )
    proof_relative = Path(profile["topology"]["proof_path"])
    proof_path = directory / proof_relative
    proof_path.parent.mkdir(parents=True, exist_ok=True)
    proof_path.write_bytes(proof_source.read_bytes())
    profile["topology"]["proof_sha256"] = hashlib.sha256(
        proof_path.read_bytes()
    ).hexdigest()
    _write_json(directory / "profile.json", profile)
    profile_sha256 = runtime_fixture._canonical_profile_sha256(profile)
    build_sha256 = "b" * 64
    pair_seed = 41_719 + int(str(child["pair_id"]).split("-")[-1])
    receipt = json.loads((directory / "raw" / "fault-receipt.json").read_text())
    _write_json(directory / "fault-plan.json", receipt["fault_plan"])
    _write_json(
        directory / "manifest.json",
        {
            "schema_version": 1,
            "profile_sha256": profile_sha256,
            "build_sha256": build_sha256,
            "pair_id": child["pair_id"],
            "pair_seed": pair_seed,
            "slot_id": child["slot_id"],
        },
    )
    authorization_request = {
        "schema_version": 1,
        "profile_sha256": profile_sha256,
        "topology_proof_sha256": profile["topology"]["proof_sha256"],
        "pair_id": child["pair_id"],
        "slot_id": child["slot_id"],
        "automatic_retries": 0,
        "replacement_policy": "none",
    }
    request_sha256 = hashlib.sha256(
        fixture._canonical(authorization_request)
    ).hexdigest()
    _write_json(
        directory / "preflight.json",
        {
            **authorization_request,
            "request_sha256": request_sha256,
            "execution_authorized": False,
            "launch_permitted": False,
        },
    )
    _write_json(
        directory / "authorization.json",
        {
            **authorization_request,
            "request_sha256": request_sha256,
            "approval_reference": "thesis-author-approved-focused-run",
            "approved_utc": "2026-08-12T12:00:00+00:00",
        },
    )
    _write_json(
        directory / "pair-receipt.json",
        {
            "schema_version": 1,
            "pair_id": child["pair_id"],
            "slot_id": child["slot_id"],
            "automatic_retries": 0,
            "replacement_policy": "none",
        },
    )
    _write_json(
        directory / "runner-outcome.json",
        {"verdict": "UNTRUSTED", "source": "runner"},
    )
    _write_json(
        directory / "runtime" / "build-provenance.json",
        {"revision": "a" * 40, "build_sha256": build_sha256},
    )
    _write_json(
        directory / "runtime" / "effective-runtime.json",
        {"profile_sha256": profile_sha256, "pair_seed": pair_seed},
    )
    manager_argv, manager_input = native_fixture._safe_manager_boundary()
    _write_json(
        directory / "runtime" / "launch-arguments.json",
        {"manager_argv": list(manager_argv)},
    )
    _write_json(
        directory / "runtime" / "manager-observed-argv.json",
        {"argv": list(manager_argv)},
    )
    _write_json(directory / "runtime" / "manager-input.json", manager_input)
    events = [
        json.loads(line)
        for line in (directory / "raw" / "events.jsonl").read_text().splitlines()
    ]
    run_id = str(events[0]["run_id"])
    manager_instance = f"{run_id}-adaptive-manager"
    if child["arm"] == "adaptive":
        epoch0_manager_events, _replay_input, epoch0_replay = (
            native_fixture._epoch1_replay_binding(4)
        )
        assert epoch0_replay["snapshot_id"] == native_fixture.ADAPTIVE_E1_SNAPSHOT_ID
        epoch1_manager_events = deepcopy(native_fixture._accepted_ranking_evidence())
        ranking = native_fixture._native_ranking_snapshot(epoch1_manager_events)
        manager_events = [*epoch0_manager_events, *epoch1_manager_events]
        epoch1 = native_fixture._native_epoch_chain()[1]
        epoch2_wire, epoch2 = native_fixture._encode_native_epoch_bundle(
            2,
            epoch1.epoch_digest,
            native_fixture.NATIVE_SNAPSHOT_SEED,
            native_fixture.NATIVE_PLACEMENT_POLICY,
            native_fixture.E2_TREES,
            evidence_snapshot_id=str(ranking["snapshot_id"]),
            evidence_cutoff=native_fixture.EVIDENCE_CUTOFF,
        )
        (directory / "raw" / "epoch2.bundle").write_bytes(epoch2_wire)
        manager_base_ns = 12_000_000_000
        for event in events:
            payload = event["payload"]
            if not isinstance(payload, dict):
                continue
            if event["event_type"] == "epoch.command_committed" and payload.get(
                "successor_epoch_number"
            ) == 2:
                event["payload"] = fixture._command_payload(epoch2, 11)
            elif event["event_type"] == "epoch.activated" and payload.get(
                "epoch_number"
            ) == 2:
                payload["epoch_digest"] = epoch2.epoch_digest
            elif event["event_type"] == "block.committed":
                proof = payload.get("decision_proof")
                if isinstance(proof, dict) and proof.get("epoch_number") == 2:
                    proof["epoch_digest"] = epoch2.epoch_digest
    else:
        manager_events, _replay_input, replay = native_fixture._epoch1_replay_binding(3)
        control_wire, control_epoch1, _adaptive_wire, _adaptive_epoch1 = (
            native_fixture._independent_epoch1_bundles(verify_replay=True)
        )
        assert replay["snapshot_id"] == native_fixture.CONTROL_E1_SNAPSHOT_ID
        (directory / "raw" / "epoch1.bundle").write_bytes(control_wire)
        manager_base_ns = 8_000_000_000
        for event in events:
            payload = event["payload"]
            if not isinstance(payload, dict):
                continue
            if event["event_type"] == "epoch.command_committed" and payload.get(
                "successor_epoch_number"
            ) == 1:
                event["payload"] = fixture._command_payload(control_epoch1, 4)
            elif event["event_type"] == "epoch.activated" and payload.get(
                "epoch_number"
            ) == 1:
                payload["epoch_digest"] = control_epoch1.epoch_digest
            elif event["event_type"] == "block.committed":
                proof = payload.get("decision_proof")
                if isinstance(proof, dict) and proof.get("epoch_number") == 1:
                    proof["epoch_digest"] = control_epoch1.epoch_digest
    for sequence, event in enumerate(manager_events, start=1):
        event["run_id"] = run_id
        event["source_instance"] = manager_instance
        event["source_sequence"] = sequence
        event["source_monotonic_ns"] = manager_base_ns + sequence
    assert [event["source_sequence"] for event in manager_events] == list(
        range(1, len(manager_events) + 1)
    )
    events = [
        event for event in events if event["source_kind"] != "adaptation_manager"
    ] + manager_events
    for source_kind, name in (
        ("replica", "replica-events.jsonl"),
        ("adaptation_manager", "adaptive-manager-events.jsonl"),
        ("client", "client-events.jsonl"),
    ):
        selected = [event for event in events if event["source_kind"] == source_kind]
        (directory / "raw" / name).write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in selected),
            encoding="utf-8",
        )
    (directory / "raw" / "events.jsonl").unlink()
    _write_json(
        directory / "runtime" / "source-inventory.json",
        {
            "sources": sorted(
                {
                    (event["source_kind"], event["source_id"])
                    for event in events
                }
            )
        },
    )
    _write_json(directory / "derived" / "phase-windows.json", {"phases": []})
    _write_json(directory / "derived" / "throughput.json", {"rows": []})
    _write_json(directory / "cleanup.json", {"complete": True})
    seal = fixture._archive().create_evidence_seal(directory)
    required = {
        "profile.json",
        proof_relative.as_posix(),
        "preflight.json",
        "authorization.json",
        "pair-receipt.json",
        "fault-plan.json",
        "manifest.json",
        "runner-outcome.json",
        "runtime/build-provenance.json",
        "runtime/effective-runtime.json",
        "runtime/launch-arguments.json",
        "runtime/manager-observed-argv.json",
        "runtime/manager-input.json",
        "runtime/source-inventory.json",
        "raw/fault-receipt.json",
        "raw/replica-events.jsonl",
        "raw/adaptive-manager-events.jsonl",
        "raw/client-events.jsonl",
        "raw/epoch1.bundle",
        "raw/issuer-public-key.txt",
        "derived/phase-windows.json",
        "derived/throughput.json",
        "cleanup.json",
        "evidence-seal.json",
    }
    if child["arm"] == "adaptive":
        required.add("raw/epoch2.bundle")
    actual = {
        str(path.relative_to(directory))
        for path in directory.rglob("*")
        if path.is_file()
    }
    assert actual == required
    child["child_tree_sha256"] = seal.tree_sha256
    child["child_seal_sha256"] = seal.seal_sha256


def _trusted_provenance(directory: Path) -> dict[str, object]:
    profile = json.loads((directory / "profile.json").read_text(encoding="utf-8"))
    build = json.loads(
        (directory / "runtime" / "build-provenance.json").read_text(
            encoding="utf-8"
        )
    )
    seal = fixture._archive().verify_evidence_seal(directory)
    return {
        "schema_version": 1,
        "revision": build["revision"],
        "build_sha256": build["build_sha256"],
        "profile_sha256": runtime_fixture._canonical_profile_sha256(profile),
        "topology_proof_sha256": profile["topology"]["proof_sha256"],
        "evidence_tree_sha256": seal.tree_sha256,
        "evidence_seal_sha256": seal.seal_sha256,
    }


def _aggregate_trusted_provenance(
    root: Path,
    children: list[Mapping[str, object]],
) -> dict[str, object]:
    entries: dict[str, object] = {}
    for child in children:
        directory = child["sealed_child_directory"]
        assert isinstance(directory, Path)
        relative = str(directory.relative_to(root))
        provenance = _trusted_provenance(directory)
        entries[relative] = {
            "tree_sha256": provenance["evidence_tree_sha256"],
            "seal_sha256": provenance["evidence_seal_sha256"],
            "provenance": provenance,
        }
    return {"schema_version": 1, "children": entries}


@pytest.mark.parametrize(
    ("profile_path", "expected"),
    (
        (
            runtime_fixture.N7_PROFILE,
            {
                "members": tuple(range(7)),
                "quorum": 5,
                "targets": (0, 1),
                "survivors": (2, 3, 4, 5, 6),
                "authoritative_source_id": "replica-2",
                "fault_target_count": 2,
                "control_transition_count": 1,
                "adaptive_transition_count": 2,
            },
        ),
        (
            runtime_fixture.N31_PROFILE,
            {
                "members": tuple(range(31)),
                "quorum": 21,
                "targets": (22, 23, 24),
                "survivors": tuple((*range(22), *range(25, 31))),
                "authoritative_source_id": "replica-0",
                "fault_target_count": 3,
                "control_transition_count": 1,
                "adaptive_transition_count": 2,
            },
        ),
    ),
)
def test_validator_contract_is_derived_from_each_focused_profile(
    profile_path: Path,
    expected: Mapping[str, object],
    tmp_path: Path,
) -> None:
    validation = _validation()
    root = tmp_path / profile_path.stem
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    proof_source = runtime_fixture._topology_proof_path(profile_path, profile)
    proof_path = root / profile["topology"]["proof_path"]
    proof_path.parent.mkdir(parents=True, exist_ok=True)
    proof_path.write_bytes(proof_source.read_bytes())
    _write_json(root / "profile.json", profile)
    contract = _document(validation.validation_contract_from_profile(root))
    assert tuple(contract["members"]) == expected["members"]
    assert contract["quorum"] == expected["quorum"]
    assert tuple(contract["targets"]) == expected["targets"]
    assert tuple(contract["survivors"]) == expected["survivors"]
    assert contract["authoritative_source_id"] == expected[
        "authoritative_source_id"
    ]
    assert contract["fault_target_count"] == expected["fault_target_count"]
    assert contract["manager_blinding_target_count"] == expected[
        "fault_target_count"
    ]
    assert contract["control_transition_count"] == expected[
        "control_transition_count"
    ]
    assert contract["adaptive_transition_count"] == expected[
        "adaptive_transition_count"
    ]


def test_validator_uses_no_n31_specific_fault_or_blinding_helper() -> None:
    source = inspect.getsource(_validation())
    assert "from . import factorial_validation, n31_crash_pair" not in source
    assert "n31_crash_pair." not in source


def test_ranking_reconstruction_calls_native_replay_with_minimum_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validation = _validation()
    events = native_fixture._accepted_ranking_evidence()
    _epoch1_wire, epoch1, _epoch2_wire, _epoch2 = native_fixture._native_epoch_chain()
    replay_calls: list[dict[str, object]] = []
    original_replay = validation.factorial_validation.replay_native_adaptation_snapshot

    def replay(
        source_events: object,
        **kwargs: object,
    ) -> object:
        replay_calls.append({"events": source_events, **kwargs})
        return original_replay(source_events, **kwargs)

    monkeypatch.setattr(
        validation.factorial_validation,
        "replay_native_adaptation_snapshot",
        replay,
    )
    result = _document(
        validation.reconstruct_focused_ranking(
            events,
            membership_replica_ids=tuple(range(31)),
            predecessor_epoch_number=1,
            predecessor_epoch_digest=epoch1.epoch_digest,
            baseline_evidence_cutoff=31,
            current_evidence_cutoff=96,
            policy=native_fixture.NATIVE_RESPONSIVENESS_POLICY,
            seed=native_fixture.NATIVE_SNAPSHOT_SEED,
            suffix_only=True,
        )
    )
    assert len(replay_calls) == 1
    assert replay_calls[0]["policy"]["minimum_attempts"] >= 2
    assert result["ranked_ids"] == list(native_fixture.RANKED_SURVIVORS)

    insufficient = deepcopy(native_fixture.NATIVE_RESPONSIVENESS_POLICY)
    insufficient["minimum_attempts"] = 4
    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation.reconstruct_focused_ranking(
            events,
            membership_replica_ids=tuple(range(31)),
            predecessor_epoch_number=1,
            predecessor_epoch_digest=epoch1.epoch_digest,
            baseline_evidence_cutoff=31,
            current_evidence_cutoff=96,
            policy=insufficient,
            seed=native_fixture.NATIVE_SNAPSHOT_SEED,
            suffix_only=True,
        )


def test_trusted_provenance_is_exact_and_bound_to_the_sealed_child(
    tmp_path: Path,
) -> None:
    validation = _validation()
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    trusted = _trusted_provenance(directory)
    assert validation.validate_sealed_arm(
        directory,
        trusted_provenance=trusted,
    )["verdict"] == "PASS"
    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation.validate_sealed_arm(
            directory,
            trusted_provenance=object(),
        )

    mutations = [{}]
    for field in (
        "revision",
        "build_sha256",
        "profile_sha256",
        "topology_proof_sha256",
        "evidence_tree_sha256",
        "evidence_seal_sha256",
    ):
        changed = dict(trusted)
        changed[field] = "0" * len(str(changed[field]))
        mutations.append(changed)
    for changed in mutations:
        with pytest.raises(validation.FocusedCrashPairValidationError):
            validation.validate_sealed_arm(
                directory,
                trusted_provenance=changed,
            )


def test_validator_imports_no_runner_or_runtime_verdict_logic() -> None:
    module = _validation()
    tree = ast.parse(inspect.getsource(module))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    forbidden = {
        "experiments.adaptive.kauri_experiment.focused_crash_pair_runtime",
        "experiments.adaptive.run_focused_n31_crash_pair",
    }
    assert imported.isdisjoint(forbidden)
    source = inspect.getsource(module)
    assert "runner-outcome.json" not in source
    assert "runner_verdict" not in source


def test_sealed_arm_is_reconstructed_from_raw_before_fault_truth_join(
    tmp_path: Path,
) -> None:
    validation = _validation()
    runner = fixture._runner()
    plan = fixture._plan(runner)
    children = fixture._children(plan, tmp_path)
    for child in children:
        _complete_child(child)
    control = next(child for child in children if child["arm"] == "control")
    adaptive = next(child for child in children if child["arm"] == "adaptive")

    for child in (control, adaptive):
        directory = child["sealed_child_directory"]
        assert isinstance(directory, Path)
        (directory / "evidence-seal.json").unlink()
        _write_json(
            directory / "runner-outcome.json",
            {
                "verdict": "PASS",
                "phase_predicates": "poison-if-consumed",
                "configured_truth": [22, 23, 24],
            },
        )
        fixture._archive().create_evidence_seal(directory)

    control_result = _document(
        validation.validate_sealed_arm(
            control["sealed_child_directory"],
            trusted_provenance=_trusted_provenance(
                control["sealed_child_directory"]  # type: ignore[arg-type]
            ),
        )
    )
    adaptive_result = _document(
        validation.validate_sealed_arm(
            adaptive["sealed_child_directory"],
            trusted_provenance=_trusted_provenance(
                adaptive["sealed_child_directory"]  # type: ignore[arg-type]
            ),
        )
    )
    assert control_result["verdict"] == adaptive_result["verdict"] == "PASS"
    assert control_result["source_blind_reconstruction"] is True
    assert control_result["fault_receipt_joined_after_reconstruction"] is True
    assert control_result["epoch2_present"] is False
    assert adaptive_result["epoch2_present"] is True
    assert adaptive_result["ranking_reconstructed_from_raw"] is True

    directory = adaptive["sealed_child_directory"]
    assert isinstance(directory, Path)
    receipt_path = directory / "raw" / "fault-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["fault_plan"]["actions"][0]["replica_id"] = 21
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    _reseal(directory)
    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation.validate_sealed_arm(
            directory,
            trusted_provenance=_trusted_provenance(directory),
        )


def test_fake_runtime_flow_reaches_valid_nonempty_sealed_arm_artifacts(
    tmp_path: Path,
) -> None:
    runtime = runtime_fixture._runtime()
    validation = _validation()
    plan = fixture._plan(fixture._runner())
    children = [
        child
        for child in fixture._children(plan, tmp_path)
        if child["pair_id"] == "pair-01"
    ]
    assert {child["arm"] for child in children} == {"control", "adaptive"}
    profile = runtime_fixture.SimpleNamespace(
        replica_ids=tuple(range(31)),
        quorum=21,
        target_replica_ids=(22, 23, 24),
        issuer_public_key=native_fixture.ISSUER_PUBLIC_KEY,
    )
    issuer_by_arm: dict[str, str] = {}
    for child in children:
        arm = "C" if child["arm"] == "control" else "A"
        snapshots = runtime_fixture._arm_snapshots(31, arm)
        snapshots["baseline"] = {
            **snapshots["baseline"],
            "authoritative_commit_count": 1,
        }
        hooks, calls = runtime_fixture._arm_hooks(runtime, snapshots)
        trace: list[object] = ["readiness"]
        assert snapshots["baseline"]["authoritative_commit_count"] == 1
        outcome = _document(
            runtime._drive_arm_state_machine(
                profile,
                arm,
                str(child["pair_id"]),
                hooks,
            )
        )
        trace.extend(calls)
        trace.append("cleanup")
        assert trace[0:3] == [
            "readiness",
            ("stable", "baseline"),
            ("fault", "fault"),
        ]
        assert calls.count(("fault", "fault")) == 1
        assert ("evidence", "nonresponse") in calls
        assert ("commands", "commands1") in calls
        assert ("activations", "activations1") in calls
        assert ("commit", "commit1") in calls
        if arm == "C":
            assert ("request", "epoch2") not in calls
            assert outcome["epoch2_present"] is False
        else:
            assert calls.index(("ranking", "ranking")) < calls.index(
                ("request", "epoch2")
            )
            assert outcome["epoch2_present"] is True
        assert trace[-1] == "cleanup"
        issuer_by_arm[arm] = profile.issuer_public_key

        directory = child["sealed_child_directory"]
        assert isinstance(directory, Path)
        combined = directory / "raw" / "events.jsonl"
        events = [json.loads(line) for line in combined.read_text().splitlines()]
        events.append(
            {
                "event_schema_version": 1,
                "event_type": "process.started",
                "payload": {"exit_status": None},
                "run_id": events[0]["run_id"],
                "source_id": "client-0",
                "source_instance": f"{events[0]['run_id']}-client-0",
                "source_kind": "client",
                "source_monotonic_ns": 1,
                "source_sequence": 1,
            }
        )
        combined.write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
            encoding="utf-8",
        )
        _complete_child(child)
        for path in directory.rglob("*"):
            if path.is_file():
                assert path.stat().st_size > 0, path
        fixture._archive().verify_evidence_seal(directory)
        assert validation.validate_sealed_arm(
            directory,
            trusted_provenance=_trusted_provenance(directory),
        )["verdict"] == "PASS"
    assert set(issuer_by_arm.values()) == {native_fixture.ISSUER_PUBLIC_KEY}


def _drive_raw_evidence_source(
    directory: Path,
    *,
    timeout_s: float = 1.0,
) -> dict[str, object]:
    runtime = runtime_fixture._runtime()
    source = runtime.FocusedRawEvidenceSource(
        run_directory=directory,
        poll_interval_s=0,
        timeout_s=timeout_s,
    )
    fault_calls: list[object] = []
    hooks = source.arm_runtime_hooks(
        inject_atomic_fault_batch=lambda: (
            fault_calls.append("fault"),
            source.atomic_fault_outcome(),
        )[1]
    )
    result = _document(
        runtime._drive_arm_state_machine(
            source.state_machine_profile(),
            source.arm(),
            source.pair_id(),
            hooks,
        )
    )
    assert fault_calls == ["fault"]
    return result


def _raw_source(directory: Path) -> object:
    runtime = runtime_fixture._runtime()
    return runtime.FocusedRawEvidenceSource(
        run_directory=directory,
        poll_interval_s=0,
        timeout_s=1,
    )


def _add_epoch2_common_commit_witnesses(directory: Path) -> None:
    events = _load_events(directory)
    epoch2_commit = next(
        event
        for event in events
        if event["event_type"] == "block.committed"
        and event["payload"]["decision_proof"]["epoch_number"] == 2
    )
    identity = {
        key: epoch2_commit["payload"][key]
        for key in (
            "block_height",
            "block_hash",
            "parent_hash",
            "transaction_count",
            "commit_batch_index",
        )
    }
    for replica in native_fixture.SURVIVORS[: native_fixture.Q]:
        source_id = f"replica-{replica}"
        source_events = [
            event for event in events if event["source_id"] == source_id
        ]
        events.append(
            {
                "event_schema_version": 1,
                "event_type": "block.commit_observed",
                "payload": dict(identity),
                "run_id": epoch2_commit["run_id"],
                "source_id": source_id,
                "source_instance": source_events[0]["source_instance"],
                "source_kind": "replica",
                "source_monotonic_ns": 16_500_000_000 + replica,
                "source_sequence": max(
                    int(event["source_sequence"]) for event in source_events
                )
                + 1,
            }
        )
    replica_zero_events = [
        event for event in events if event["source_id"] == "replica-0"
    ]
    events.append(
        {
            **deepcopy(epoch2_commit),
            "payload": {
                **deepcopy(epoch2_commit["payload"]),
                "block_height": 5,
                "block_hash": f"{5:064x}",
                "parent_hash": identity["block_hash"],
                "transaction_count": 500,
                "decision_proof": {
                    **deepcopy(epoch2_commit["payload"]["decision_proof"]),
                    "block_hash": f"{5:064x}",
                },
            },
            "source_monotonic_ns": 17_000_000_000,
            "source_sequence": max(
                int(event["source_sequence"]) for event in replica_zero_events
            )
            + 1,
        }
    )
    _write_events(directory, events)
    _write_source_inventory(directory, events)


def _add_complete_ready_barrier(
    directory: Path, *, include_client: bool = True
) -> list[dict[str, object]]:
    events = _load_events(directory)
    run_id = str(events[0]["run_id"])
    by_source = {
        str(event["source_id"]): str(event["source_instance"])
        for event in events
    }
    expected = [*(f"replica-{replica}" for replica in range(31)), "adaptive-manager"]
    if include_client:
        expected.append("client-0")
    augmented: list[dict[str, object]] = []
    for source_id in expected:
        source_kind = (
            "replica"
            if source_id.startswith("replica-")
            else "adaptation_manager" if source_id == "adaptive-manager" else "client"
        )
        instance = by_source.get(source_id, f"{run_id}-{source_id}")
        for sequence, event_type in enumerate(("process.started", "process.ready"), start=1):
            augmented.append(
                {
                    "event_schema_version": 1,
                    "event_type": event_type,
                    "payload": {"exit_status": None},
                    "run_id": run_id,
                    "source_id": source_id,
                    "source_instance": instance,
                    "source_kind": source_kind,
                    "source_monotonic_ns": sequence,
                    "source_sequence": sequence,
                }
            )
        source_events = [event for event in events if event["source_id"] == source_id]
        for sequence, event in enumerate(source_events, start=3):
            event["source_sequence"] = sequence
            augmented.append(event)
    _write_events(directory, augmented)
    _write_source_inventory(directory, augmented)
    return augmented


def _add_complete_stable_phase_windows(directory: Path) -> None:
    """Rebase the raw fixture onto three causal 30-second commit windows."""

    events = _load_events(directory)
    second = 1_000_000_000
    commits_by_epoch = {
        epoch: sorted(
            (
                event
                for event in events
                if event["event_type"] == "block.committed"
                and event["payload"]["decision_proof"]["epoch_number"] == epoch
            ),
            key=lambda event: int(event["payload"]["block_height"]),
        )
        for epoch in (0, 1, 2)
    }
    assert [len(commits_by_epoch[epoch]) for epoch in (0, 1, 2)] == [2, 1, 2]

    for event, timestamp in zip(
        commits_by_epoch[0], (1 * second, 31 * second), strict=True
    ):
        event["source_monotonic_ns"] = timestamp

    epoch1_common = commits_by_epoch[1][0]
    epoch1_common["source_monotonic_ns"] = 50 * second
    epoch1_stable = deepcopy(epoch1_common)
    epoch1_stable["source_monotonic_ns"] = 80 * second
    epoch1_stable["payload"].update(
        {
            "block_height": 4,
            "block_hash": f"{4:064x}",
            "parent_hash": f"{3:064x}",
        }
    )
    epoch1_stable["payload"]["decision_proof"]["block_hash"] = f"{4:064x}"
    events.append(epoch1_stable)

    epoch2_common, epoch2_late = commits_by_epoch[2]
    old_epoch2_identity = {
        key: epoch2_common["payload"][key]
        for key in (
            "block_height",
            "block_hash",
            "parent_hash",
            "transaction_count",
            "commit_batch_index",
        )
    }
    new_epoch2_identity = {
        **old_epoch2_identity,
        "block_height": 5,
        "block_hash": f"{5:064x}",
        "parent_hash": f"{4:064x}",
    }
    epoch2_common["payload"].update(new_epoch2_identity)
    epoch2_common["payload"]["decision_proof"]["block_hash"] = f"{5:064x}"
    epoch2_common["source_monotonic_ns"] = 96 * second
    for event in events:
        if (
            event["event_type"] == "block.commit_observed"
            and event["payload"] == old_epoch2_identity
        ):
            event["payload"] = dict(new_epoch2_identity)
            replica = int(str(event["source_id"]).removeprefix("replica-"))
            event["source_monotonic_ns"] = 96 * second + replica
    epoch2_late["payload"].update(
        {
            "block_height": 6,
            "block_hash": f"{6:064x}",
            "parent_hash": f"{5:064x}",
        }
    )
    epoch2_late["payload"]["decision_proof"]["block_hash"] = f"{6:064x}"
    epoch2_late["source_monotonic_ns"] = 126 * second

    for event in events:
        payload = event["payload"]
        if event["source_kind"] == "adaptation_manager":
            if event["event_type"] == "adaptive_v2_evidence_snapshot":
                predecessor = int(payload["predecessor_epoch_number"])
                event["source_monotonic_ns"] = (47 if predecessor == 0 else 92) * second
            elif event["event_type"] == "evidence.observation_accepted":
                observation = payload["observation"]
                predecessor = int(observation["configuration"]["epoch_number"])
                base = 34 if predecessor == 0 else 81
                event["source_monotonic_ns"] = (
                    base * second
                    + int(payload["ingestion_sequence"]) * (second // 10)
                )
            elif event["event_type"] == "process.started":
                sequence = int(event["source_sequence"])
                if sequence == 1:
                    event["source_monotonic_ns"] = 1
                else:
                    event["source_monotonic_ns"] = (
                        (33 * second + second // 2)
                        if sequence == 3
                        else (80 * second + second // 2)
                    )
        elif event["event_type"] == "epoch.command_committed":
            epoch = int(payload["successor_epoch_number"])
            event["source_monotonic_ns"] = (48 if epoch == 1 else 94) * second
        elif event["event_type"] == "epoch.activated":
            epoch = int(payload["epoch_number"])
            event["source_monotonic_ns"] = (49 if epoch == 1 else 95) * second
        elif (
            event["event_type"] == "block.commit_observed"
            and payload["block_height"] == 3
        ):
            replica = int(str(event["source_id"]).removeprefix("replica-"))
            event["source_monotonic_ns"] = 50 * second + replica

    receipt_path = directory / "raw" / "fault-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    outcomes_by_fault: dict[str, dict[str, object]] = {}
    for ordinal, outcome in enumerate(receipt["sigkill_outcomes"]):
        outcome["requested_monotonic_ns"] = 32 * second + ordinal
        outcome["confirmed_monotonic_ns"] = 33 * second + ordinal
        outcomes_by_fault[str(outcome["fault_id"])] = deepcopy(outcome)
    for ordinal, row in enumerate(receipt["fault_journal"]):
        terminal = row["lifecycle"] == "terminal"
        row["source_monotonic_ns"] = (
            (33 * second + second // 2) if terminal else (31 * second + second // 2)
        ) + ordinal
        if terminal:
            row["outcome"] = {
                **outcomes_by_fault[str(row["fault_id"])],
                "status": "succeeded",
            }
    _write_json(receipt_path, receipt)

    by_source: dict[str, list[dict[str, object]]] = {}
    for event in events:
        by_source.setdefault(str(event["source_id"]), []).append(event)
    for source_events in by_source.values():
        source_events.sort(
            key=lambda event: (
                int(event["source_monotonic_ns"]),
                int(event["source_sequence"]),
            )
        )
        for sequence, event in enumerate(source_events, start=1):
            event["source_sequence"] = sequence
    events.sort(
        key=lambda event: (
            str(event["source_kind"]),
            str(event["source_id"]),
            str(event["source_instance"]),
            int(event["source_sequence"]),
        )
    )
    _write_events(directory, events)
    _write_source_inventory(directory, events)


@pytest.mark.parametrize(
    "mutation",
    (
        "missing-ready",
        "short-baseline",
        "short-containment",
        "short-late",
        "fabricated-nonresponse-time",
        "fabricated-request-time",
    ),
)
def test_raw_source_barriers_are_event_derived_and_span_configured_windows(
    mutation: str,
    tmp_path: Path,
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    events = _add_complete_ready_barrier(directory)
    source = _raw_source(directory)
    assert source.poll("readiness") == {"ready": True}
    if mutation == "missing-ready":
        events = [
            event
            for event in events
            if not (
                event["source_id"] == "replica-30"
                and event["event_type"] == "process.ready"
            )
        ]
        _write_events(directory, events)
        with pytest.raises(runtime_fixture._runtime().FocusedCrashPairRuntimeError):
            source.poll("readiness")
    elif mutation.startswith("short-"):
        phase = mutation.removeprefix("short-")
        assert source.poll(phase) is None
    else:
        snapshot_name = (
            "nonresponse" if mutation == "fabricated-nonresponse-time" else "epoch1"
        )
        snapshot = source.poll(snapshot_name)
        assert snapshot is not None
        assert snapshot["source_monotonic_ns"] in {
            event["source_monotonic_ns"] for event in events
        }


@pytest.mark.parametrize(
    "mutation",
    (
        "zero-ready",
        "spoofed-kind",
        "no-ready-baseline",
        "no-ready-containment",
        "no-ready-late",
        "no-ready-nonresponse",
    ),
)
def test_raw_readiness_and_phase_gates_require_native_authenticated_sources(
    mutation: str,
    tmp_path: Path,
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    if mutation == "no-ready-late":
        _add_epoch2_common_commit_witnesses(directory)
    source = _raw_source(directory)
    if mutation == "zero-ready":
        assert source.poll("readiness") is None
    elif mutation.startswith("no-ready-") and mutation != "no-ready-nonresponse":
        assert source.poll(mutation.removeprefix("no-ready-")) is None
    elif mutation == "no-ready-nonresponse":
        assert source.poll("nonresponse") is None
    else:
        events = _add_complete_ready_barrier(directory, include_client=False)
        for event in events:
            if event["source_id"] != "replica-30":
                continue
            if event["event_type"] in {"process.started", "process.ready"}:
                event["source_kind"] = "client"
            else:
                event["source_sequence"] -= 2  # type: ignore[operator]
        run_id = str(events[0]["run_id"])
        events.extend(
            {
                "event_schema_version": 1,
                "event_type": event_type,
                "payload": {"exit_status": None},
                "run_id": run_id,
                "source_id": "client-0",
                "source_instance": f"{run_id}-client-0",
                "source_kind": "client",
                "source_monotonic_ns": sequence,
                "source_sequence": sequence,
            }
            for sequence, event_type in enumerate(
                ("process.started", "process.ready"), start=1
            )
        )
        _write_events(directory, events)
        with pytest.raises(runtime_fixture._runtime().FocusedCrashPairRuntimeError):
            source.poll("readiness")


def test_raw_readiness_accepts_exact_native_emitters_without_client_lifecycle(
    tmp_path: Path,
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    _add_complete_ready_barrier(directory, include_client=False)
    assert _raw_source(directory).poll("readiness") == {"ready": True}


def test_epoch_request_timestamp_is_exact_manager_bundle_production_event(
    tmp_path: Path,
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    events = _load_events(directory)
    audit = next(
        event
        for event in events
        if event["source_kind"] == "adaptation_manager"
        and event["event_type"] == "adaptive_v2_evidence_snapshot"
        and event["payload"]["predecessor_epoch_number"] == 1
    )
    request = _raw_source(directory).poll("epoch2")
    assert request is not None
    assert request["source_monotonic_ns"] == audit["source_monotonic_ns"]


@pytest.mark.parametrize("mutation", (None, "identity", "quorum"))
def test_multiple_authoritative_common_commits_select_latest_q_witnessed_identity(
    mutation: str | None,
    tmp_path: Path,
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    _add_epoch2_common_commit_witnesses(directory)
    source = _raw_source(directory)
    assert source._common_commit(source._events(), 2)["block_height"] == 4
    events = _load_events(directory)
    later = next(
        event
        for event in events
        if event["event_type"] == "block.committed"
        and event["payload"]["block_height"] == 5
    )
    identity = {
        key: later["payload"][key]
        for key in (
            "block_height",
            "block_hash",
            "parent_hash",
            "transaction_count",
            "commit_batch_index",
        )
    }
    for replica in native_fixture.SURVIVORS[: native_fixture.Q]:
        source_id = f"replica-{replica}"
        rows = [event for event in events if event["source_id"] == source_id]
        events.append(
            {
                **deepcopy(rows[-1]),
                "event_type": "block.commit_observed",
                "payload": dict(identity),
                "source_monotonic_ns": 17_500_000_000 + replica,
                "source_sequence": max(int(row["source_sequence"]) for row in rows) + 1,
            }
        )
    if mutation == "identity":
        events[-1]["payload"]["block_hash"] = "f" * 64
    elif mutation == "quorum":
        events.pop()
    _write_events(directory, events)
    selected = source._common_commit(source._events(), 2)
    assert selected["block_height"] == (5 if mutation is None else 4)


def test_raw_common_commit_joins_exact_authoritative_identity_for_each_epoch(
    tmp_path: Path,
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    source = _raw_source(directory)
    events = source._events()
    epoch1 = source._common_commit(events, 1)
    assert {
        key: epoch1[key]
        for key in ("block_height", "block_hash", "parent_hash", "transaction_count")
    } == {
        "block_height": 3,
        "block_hash": f"{3:064x}",
        "parent_hash": f"{2:064x}",
        "transaction_count": 450,
    }
    with pytest.raises(runtime_fixture._runtime().FocusedCrashPairRuntimeError):
        source._common_commit(
            [
                event
                for event in events
                if not (
                    event["event_type"] == "block.commit_observed"
                    and event["source_id"] == "replica-0"
                )
            ],
            1,
        )
    with pytest.raises(runtime_fixture._runtime().FocusedCrashPairRuntimeError):
        source._common_commit(events, 2)


@pytest.mark.parametrize("layer", ("runtime", "sealed-validator"))
def test_common_commit_witness_requires_replica_source_kind_and_instance(
    layer: str,
    tmp_path: Path,
) -> None:
    runtime = runtime_fixture._runtime()
    validation = _validation()
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    source = _raw_source(directory)
    assert source._common_commit(source._events(), 1)["block_height"] == 3
    assert validation.validate_sealed_arm(
        directory,
        trusted_provenance=_trusted_provenance(directory),
    )["verdict"] == "PASS"

    _reclassify_common_commit_witness_as_client(directory)
    if layer == "runtime":
        with pytest.raises(runtime.FocusedCrashPairRuntimeError):
            source._common_commit(source._events(), 1)
    else:
        _reseal(directory)
        with pytest.raises(validation.FocusedCrashPairValidationError):
            validation.validate_sealed_arm(
                directory,
                trusted_provenance=_trusted_provenance(directory),
            )


def test_target_exit_is_exempt_only_after_its_exact_confirmed_sigkill(
    tmp_path: Path,
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    events = _load_events(directory)
    receipt = json.loads((directory / "raw" / "fault-receipt.json").read_text())
    confirmation = next(
        row["confirmed_monotonic_ns"]
        for row in receipt["sigkill_outcomes"]
        if row["replica_id"] == 22
    )
    events.append(
        {
            "event_schema_version": 1,
            "event_type": "process.exited",
            "payload": {"exit_status": -9},
            "run_id": events[0]["run_id"],
            "source_id": "replica-22",
            "source_instance": f"{events[0]['run_id']}-replica-22",
            "source_kind": "replica",
            "source_monotonic_ns": confirmation - 1,
            "source_sequence": 1,
        }
    )
    _write_events(directory, events)
    assert _raw_source(directory).unexpected_exit_ids() == (22,)


def test_target_process_record_exit_requires_exact_confirmed_sigkill_receipt(
    tmp_path: Path,
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    receipt_path = directory / "raw" / "fault-receipt.json"
    process = runtime_fixture._FakeProcess(20_022)
    process.returncode = -9
    record = runtime_fixture.SimpleNamespace(replica_id=22, process=process)
    runtime = runtime_fixture._runtime()

    confirmed = runtime.FocusedRawEvidenceSource(
        run_directory=directory,
        poll_interval_s=0,
        timeout_s=1,
        process_records=(record,),
    )
    assert confirmed.unexpected_exit_ids() == ()

    receipt_path.unlink()
    unconfirmed = runtime.FocusedRawEvidenceSource(
        run_directory=directory,
        poll_interval_s=0,
        timeout_s=1,
        process_records=(record,),
    )
    assert unconfirmed.unexpected_exit_ids() == (22,)


@pytest.mark.parametrize(
    "mutation",
    (
        "missing-survivor",
        "stale-sequence",
        "wrong-issuer",
        "stale-replay-cutoff",
        "target-post-sigkill",
        "unexpected-exit",
        "timeout",
    ),
)
def test_raw_envelope_evidence_source_rejects_runtime_graph_drift(
    mutation: str,
    tmp_path: Path,
) -> None:
    runtime = runtime_fixture._runtime()
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    _add_complete_ready_barrier(directory, include_client=False)
    _add_epoch2_common_commit_witnesses(directory)
    _add_complete_stable_phase_windows(directory)
    baseline = _drive_raw_evidence_source(directory)
    assert baseline["epoch2_present"] is True
    events = _load_events(directory)
    if mutation == "missing-survivor":
        index = next(
            index
            for index, event in enumerate(events)
            if event["event_type"] == "epoch.command_committed"
            and event["source_id"] == "replica-27"
        )
        events.pop(index)
    elif mutation == "stale-sequence":
        source_events = [
            event for event in events if event["source_id"] == "adaptive-manager"
        ]
        source_events[1]["source_sequence"] = source_events[0]["source_sequence"]
    elif mutation == "wrong-issuer":
        (directory / "raw" / "issuer-public-key.txt").write_text(
            "03" + "0" * 64 + "\n",
            encoding="ascii",
        )
    elif mutation == "stale-replay-cutoff":
        audit = next(
            event
            for event in events
            if event["event_type"] == "adaptive_v2_evidence_snapshot"
        )
        audit["payload"]["current_cutoff"] -= 1  # type: ignore[index,operator]
    elif mutation in {"target-post-sigkill", "unexpected-exit"}:
        receipt = json.loads(
            (directory / "raw" / "fault-receipt.json").read_text(encoding="utf-8")
        )
        timestamp = max(
            row["confirmed_monotonic_ns"] for row in receipt["sigkill_outcomes"]
        ) + 1
        replica = 22 if mutation == "target-post-sigkill" else 27
        events.append(
            {
                "event_schema_version": 1,
                "event_type": (
                    "process.ready"
                    if mutation == "target-post-sigkill"
                    else "process.exited"
                ),
                "payload": (
                    {"exit_status": None}
                    if mutation == "target-post-sigkill"
                    else {"exit_status": 1}
                ),
                "run_id": events[0]["run_id"],
                "source_id": f"replica-{replica}",
                "source_instance": f"{events[0]['run_id']}-replica-{replica}",
                "source_kind": "replica",
                "source_monotonic_ns": timestamp,
                "source_sequence": 1,
            }
        )
    elif mutation == "timeout":
        epoch2_commit = next(
            event
            for event in events
            if event["event_type"] == "block.committed"
            and event["payload"]["decision_proof"]["epoch_number"] == 2
        )
        identity = {
            key: epoch2_commit["payload"][key]
            for key in (
                "block_height",
                "block_hash",
                "parent_hash",
                "transaction_count",
                "commit_batch_index",
            )
        }
        index = next(
            index
            for index, event in enumerate(events)
            if event["event_type"] == "block.commit_observed"
            and event["payload"] == identity
        )
        events.pop(index)
    if mutation != "wrong-issuer":
        _write_events(directory, events)
        _write_source_inventory(directory, events)
    _reseal(directory)
    with pytest.raises(runtime.FocusedCrashPairRuntimeError):
        _drive_raw_evidence_source(
            directory,
            timeout_s=0 if mutation == "timeout" else 1.0,
        )


def _copy_child(child: Mapping[str, object], destination: Path) -> dict[str, object]:
    source = child["sealed_child_directory"]
    assert isinstance(source, Path)
    shutil.copytree(source, destination)
    copied = dict(child)
    copied["sealed_child_directory"] = destination
    return copied


def _load_events(directory: Path) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for name in (
        "replica-events.jsonl",
        "adaptive-manager-events.jsonl",
        "client-events.jsonl",
    ):
        events.extend(
            json.loads(line)
            for line in (directory / "raw" / name).read_text().splitlines()
        )
    return events


def _write_events(directory: Path, events: list[dict[str, object]]) -> None:
    for source_kind, name in (
        ("replica", "replica-events.jsonl"),
        ("adaptation_manager", "adaptive-manager-events.jsonl"),
        ("client", "client-events.jsonl"),
    ):
        selected = [event for event in events if event["source_kind"] == source_kind]
        (directory / "raw" / name).write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in selected),
            encoding="utf-8",
        )


def _write_source_inventory(
    directory: Path,
    events: list[dict[str, object]],
) -> None:
    _write_json(
        directory / "runtime" / "source-inventory.json",
        {
            "sources": sorted(
                {
                    (event["source_kind"], event["source_id"])
                    for event in events
                }
            )
        },
    )


def _reclassify_common_commit_witness_as_client(directory: Path) -> None:
    events = _load_events(directory)
    commit = next(
        event
        for event in events
        if event["event_type"] == "block.committed"
        and event["payload"]["decision_proof"]["epoch_number"] == 1
    )
    identity = {
        key: commit["payload"][key]
        for key in (
            "block_height",
            "block_hash",
            "parent_hash",
            "transaction_count",
            "commit_batch_index",
        )
    }
    witness = next(
        event
        for event in events
        if event["event_type"] == "block.commit_observed"
        and event["payload"] == identity
    )
    original_instance = str(witness["source_instance"])
    source_id = str(witness["source_id"])
    witness["source_kind"] = "client"
    witness["source_instance"] = f"{original_instance}-client-spoof"
    witness["source_sequence"] = 1
    remaining = [
        event
        for event in events
        if event is not witness
        and event["source_kind"] == "replica"
        and event["source_id"] == source_id
        and event["source_instance"] == original_instance
    ]
    remaining.sort(key=lambda event: int(event["source_sequence"]))
    for sequence, event in enumerate(remaining, start=1):
        event["source_sequence"] = sequence
    _write_events(directory, events)
    _write_source_inventory(directory, events)


@pytest.mark.parametrize(
    ("mutation", "accepted"),
    (
        ("absolute-time", True),
        ("nonunit-height", True),
        ("precrash-target-log", True),
        ("postconfirm-target-log", False),
    ),
)
def test_commit_reconstruction_uses_profile_windows_and_fault_lifetime(
    mutation: str,
    accepted: bool,
    tmp_path: Path,
) -> None:
    validation = _validation()
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    baseline = _document(
        validation.validate_sealed_arm(
            directory,
            trusted_provenance=_trusted_provenance(directory),
        )
    )
    assert baseline["verdict"] == "PASS"
    events = _load_events(directory)

    if mutation == "absolute-time":
        offset = 1_000_000_000_000
        epoch0_audit_sequence = next(
            event["source_sequence"]
            for event in events
            if event["event_type"] == "adaptive_v2_evidence_snapshot"
            and event["payload"]["predecessor_epoch_number"] == 0
        )
        for event in events:
            shift_event = (
                event["source_kind"] != "adaptation_manager"
                or event["source_sequence"] > epoch0_audit_sequence
            )
            if shift_event:
                event["source_monotonic_ns"] += offset  # type: ignore[operator]
            observation = event["payload"].get("observation")  # type: ignore[union-attr]
            if (
                isinstance(observation, dict)
                and observation["configuration"]["epoch_number"] == 1
            ):
                observation["reporter_monotonic_ns"] += offset
        accepted = [
            event
            for event in events
            if event["source_kind"] == "adaptation_manager"
            and event["event_type"] == "evidence.observation_accepted"
            and event["payload"]["observation"]["configuration"]["epoch_number"]
            == 1
        ]
        records = []
        for event in accepted:
            payload = event["payload"]
            observation = payload["observation"]
            configuration = observation["configuration"]
            records.append(
                validation.factorial_validation._EvidenceRecord(
                    ingestion_sequence=payload["ingestion_sequence"],
                    acceptance_monotonic_ns=event["source_monotonic_ns"],
                    observation_id=observation["observation_id"],
                    reporter_id=observation["reporter_id"],
                    target_id=observation["observed_replica_id"],
                    epoch_number=configuration["epoch_number"],
                    tree_id=configuration["tree_id"],
                    epoch_digest=configuration["epoch_digest"],
                    block_hash=observation["block_hash"],
                    message_type=observation["expected_message_type"],
                    outcome=observation["outcome"],
                    response_duration_us=observation["response_duration_us"],
                    deadline_duration_us=observation["deadline_duration_us"],
                    reporter_monotonic_ns=observation["reporter_monotonic_ns"],
                    reporter_sequence=observation["reporter_sequence"],
                    signer_set=tuple(observation["signer_set"]),
                    acceptance_source_sequence=event["source_sequence"],
                    schema_version=observation["schema_version"],
                )
            )
        audit = next(
            event
            for event in events
            if event["event_type"] == "adaptive_v2_evidence_snapshot"
            and event["payload"]["predecessor_epoch_number"] == 1
        )
        audit_payload = audit["payload"]
        full_snapshot_id = validation.factorial_validation._snapshot_id(
            records,
            replica_count=native_fixture.N,
            epoch_number=1,
            epoch_digest=native_fixture.E1_DIGEST,
            cutoff=native_fixture.EVIDENCE_CUTOFF,
            policy=native_fixture.NATIVE_RESPONSIVENESS_POLICY,
            seed=native_fixture.NATIVE_SNAPSHOT_SEED,
        )
        suffix_records = validation.factorial_validation._snapshot_records(
            records,
            baseline_cutoff=native_fixture.BASELINE_EVIDENCE_CUTOFF,
            current_cutoff=native_fixture.EVIDENCE_CUTOFF,
            suffix_only=True,
        )
        selected_snapshot_id = validation.factorial_validation._snapshot_id(
            suffix_records,
            replica_count=native_fixture.N,
            epoch_number=1,
            epoch_digest=native_fixture.E1_DIGEST,
            cutoff=native_fixture.EVIDENCE_CUTOFF,
            policy=native_fixture.NATIVE_RESPONSIVENESS_POLICY,
            seed=native_fixture.NATIVE_SNAPSHOT_SEED,
        )
        audit_payload["full_prefix_snapshot_id"] = full_snapshot_id
        audit_payload["evidence_snapshot_id"] = selected_snapshot_id
        epoch2_wire, epoch2 = native_fixture._encode_native_epoch_bundle(
            2,
            native_fixture.E1_DIGEST,
            native_fixture.NATIVE_SNAPSHOT_SEED,
            native_fixture.NATIVE_PLACEMENT_POLICY,
            native_fixture.E2_TREES,
            evidence_snapshot_id=selected_snapshot_id,
            evidence_cutoff=native_fixture.EVIDENCE_CUTOFF,
        )
        (directory / "raw" / "epoch2.bundle").write_bytes(epoch2_wire)
        for event in events:
            payload = event["payload"]
            if event["event_type"] == "epoch.command_committed" and payload.get(
                "successor_epoch_number"
            ) == 2:
                event["payload"] = fixture._command_payload(epoch2, 11)
            elif event["event_type"] == "epoch.activated" and payload.get(
                "epoch_number"
            ) == 2:
                payload["epoch_digest"] = epoch2.epoch_digest
            elif event["event_type"] == "block.committed":
                proof = payload.get("decision_proof")
                if isinstance(proof, dict) and proof.get("epoch_number") == 2:
                    proof["epoch_digest"] = epoch2.epoch_digest
        phases = deepcopy(baseline["scientific_measurements"]["phases"])
        for phase in phases:
            phase["start_ns"] += offset
            phase["end_ns"] += offset
        _write_json(directory / "derived" / "phase-windows.json", {"phases": phases})
    elif mutation == "nonunit-height":
        for event in events:
            if event["event_type"] in {"block.committed", "block.commit_observed"}:
                event["payload"]["block_height"] += 100  # type: ignore[index,operator]
    else:
        receipt = json.loads(
            (directory / "raw" / "fault-receipt.json").read_text(encoding="utf-8")
        )
        confirmation = receipt["sigkill_outcomes"][0]["confirmed_monotonic_ns"]
        events.append(
            {
                "event_schema_version": 1,
                "event_type": "process.started",
                "payload": {"exit_status": None},
                "run_id": events[0]["run_id"],
                "source_id": "replica-22",
                "source_instance": f"{events[0]['run_id']}-replica-22",
                "source_kind": "replica",
                "source_monotonic_ns": confirmation - 1 if accepted else confirmation + 1,
                "source_sequence": 1,
            }
        )
    _write_events(directory, events)
    _write_source_inventory(directory, events)
    _reseal(directory)
    trusted = _trusted_provenance(directory)
    if accepted:
        assert validation.validate_sealed_arm(
            directory,
            trusted_provenance=trusted,
        )["verdict"] == "PASS"
    else:
        with pytest.raises(validation.FocusedCrashPairValidationError):
            validation.validate_sealed_arm(
                directory,
                trusted_provenance=trusted,
            )


def test_commit_reconstruction_accepts_all_unique_authoritative_commits(
    tmp_path: Path,
) -> None:
    validation = _validation()
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    assert validation.validate_sealed_arm(
        directory,
        trusted_provenance=_trusted_provenance(directory),
    )["verdict"] == "PASS"
    events = _load_events(directory)
    commits = [event for event in events if event["event_type"] == "block.committed"]
    last = max(commits, key=lambda event: event["payload"]["block_height"])
    extra = deepcopy(last)
    extra_payload = extra["payload"]
    extra_payload["block_height"] += 1
    extra_payload["parent_hash"] = last["payload"]["block_hash"]
    extra_payload["block_hash"] = "f" * 64
    extra_payload["decision_proof"]["block_hash"] = extra_payload["block_hash"]
    source_events = [event for event in events if event["source_id"] == extra["source_id"]]
    extra["source_sequence"] = max(event["source_sequence"] for event in source_events) + 1
    extra["source_monotonic_ns"] = max(
        event["source_monotonic_ns"] for event in source_events
    ) + 1
    events.append(extra)
    _write_events(directory, events)
    _write_source_inventory(directory, events)
    _reseal(directory)
    result = validation.validate_sealed_arm(
        directory,
        trusted_provenance=_trusted_provenance(directory),
    )
    assert result["verdict"] == "PASS"
    assert result["authoritative_commit_count"] == 5


@pytest.mark.parametrize("mutation", ("multiple", "conflicting", "q-short"))
def test_sealed_common_commit_uses_latest_eligible_q_witnessed_identity(
    mutation: str,
    tmp_path: Path,
) -> None:
    validation = _validation()
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    assert validation.validate_sealed_arm(
        directory,
        trusted_provenance=_trusted_provenance(directory),
    )["verdict"] == "PASS"

    if mutation == "multiple":
        _add_epoch2_common_commit_witnesses(directory)
        _reseal(directory)
        result = validation.validate_sealed_arm(
            directory,
            trusted_provenance=_trusted_provenance(directory),
        )
        assert result["verdict"] == "PASS"
        assert result["authoritative_commit_count"] == 5
        return

    events = _load_events(directory)
    observations = [
        event
        for event in events
        if event["event_type"] == "block.commit_observed"
    ]
    assert len(observations) == native_fixture.Q
    if mutation == "conflicting":
        observations[-1]["payload"]["block_hash"] = "f" * 64
    else:
        events.remove(observations[-1])
    _write_events(directory, events)
    _write_source_inventory(directory, events)
    _reseal(directory)
    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation.validate_sealed_arm(
            directory,
            trusted_provenance=_trusted_provenance(directory),
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "source-order",
        "commit",
        "survivor",
        "command",
        "activation",
        "epoch1-structure",
        "ranking",
        "roles",
        "windows",
        "pair",
        "build",
        "seed",
        "manager-leak",
        "proof-omission",
        "proof-drift",
    ),
)
def test_sealed_arm_rejects_runtime_graph_or_identity_drift(
    mutation: str,
    tmp_path: Path,
) -> None:
    validation = _validation()
    plan = fixture._plan(fixture._runner())
    child = next(
        item
        for item in fixture._children(plan, tmp_path)
        if item["arm"] == "adaptive"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    assert _document(
        validation.validate_sealed_arm(
            directory,
            trusted_provenance=_trusted_provenance(directory),
        )
    )["verdict"] == "PASS"

    events = _load_events(directory)
    if mutation == "source-order":
        manager_events = [
            event for event in events if event["source_id"] == "adaptive-manager"
        ]
        manager_events[1]["source_sequence"] = manager_events[0]["source_sequence"]
    elif mutation == "commit":
        commit = next(
            event for event in events if event["event_type"] == "block.committed"
        )
        commit["payload"]["transaction_count"] += 1  # type: ignore[index,operator]
    elif mutation == "survivor":
        events.remove(
            next(
                event
                for event in events
                if event["event_type"] == "epoch.activated"
                and event["payload"]["epoch_number"] == 1  # type: ignore[index]
            )
        )
    elif mutation == "command":
        command = next(
            event
            for event in events
            if event["event_type"] == "epoch.command_committed"
        )
        command["payload"]["payload_digest"] = "0" * 64  # type: ignore[index]
    elif mutation == "activation":
        activation = next(
            event for event in events if event["event_type"] == "epoch.activated"
        )
        activation["payload"]["activation_height"] = -1  # type: ignore[index]
    elif mutation == "epoch1-structure":
        epoch1_path = directory / "raw" / "epoch1.bundle"
        decoded = native_fixture.factorial_validation.decode_epoch_change_bundle(
            epoch1_path.read_bytes(),
            issuer_public_key=native_fixture.ISSUER_PUBLIC_KEY,
        )
        trees = [asdict(tree) for tree in decoded.trees]
        changed_members = list(trees[0]["members"])  # type: ignore[arg-type]
        changed_members[1:3] = reversed(changed_members[1:3])
        trees[0]["members"] = tuple(changed_members)
        wire, _ = native_fixture._encode_native_epoch_bundle(
            1,
            decoded.previous_epoch_digest,
            decoded.generation_seed,
            decoded.policy_version,
            trees,
            evidence_snapshot_id=decoded.evidence_snapshot_id,
            evidence_cutoff=decoded.evidence_cutoff,
        )
        epoch1_path.write_bytes(wire)
    elif mutation == "ranking":
        observation = next(
            event
            for event in events
            if event["event_type"] == "evidence.observation_accepted"
        )
        observation["payload"]["observation"]["response_duration_us"] += 1  # type: ignore[index,operator]
    elif mutation == "roles":
        epoch2_path = directory / "raw" / "epoch2.bundle"
        decoded = native_fixture.factorial_validation.decode_epoch_change_bundle(
            epoch2_path.read_bytes(),
            issuer_public_key=native_fixture.ISSUER_PUBLIC_KEY,
        )
        trees = [asdict(tree) for tree in decoded.trees]
        trees[0]["members"], trees[1]["members"] = (  # type: ignore[index]
            trees[1]["members"],  # type: ignore[index]
            trees[0]["members"],  # type: ignore[index]
        )
        wire, _ = native_fixture._encode_native_epoch_bundle(
            2,
            decoded.previous_epoch_digest,
            decoded.generation_seed,
            decoded.policy_version,
            trees,
            evidence_snapshot_id=decoded.evidence_snapshot_id,
            evidence_cutoff=decoded.evidence_cutoff,
        )
        epoch2_path.write_bytes(wire)
    elif mutation == "windows":
        events[-1]["source_monotonic_ns"] = 1
    elif mutation in {"pair", "build", "seed"}:
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        field = {"pair": "pair_id", "build": "build_sha256", "seed": "pair_seed"}[
            mutation
        ]
        manifest[field] = "wrong" if mutation != "seed" else 0
        _write_json(manifest_path, manifest)
    elif mutation == "manager-leak":
        observed_path = directory / "runtime" / "manager-observed-argv.json"
        manager_path = directory / "runtime" / "manager-input.json"
        observed = json.loads(observed_path.read_text())
        manager = json.loads(manager_path.read_text())
        observed["argv"].extend(("--crash-targets", "22,23,24"))
        manager["requested_argv"] = list(observed["argv"])
        manager["observed_argv"] = list(observed["argv"])
        _write_json(observed_path, observed)
        _write_json(manager_path, manager)
    if mutation == "proof-omission":
        profile = json.loads((directory / "profile.json").read_text())
        (directory / profile["topology"]["proof_path"]).unlink()
    elif mutation == "proof-drift":
        profile = json.loads((directory / "profile.json").read_text())
        proof_path = directory / profile["topology"]["proof_path"]
        proof_path.write_bytes(proof_path.read_bytes() + b"\n")
    if mutation not in {
        "epoch1-structure",
        "roles",
        "pair",
        "build",
        "seed",
        "manager-leak",
        "proof-omission",
        "proof-drift",
    }:
        _write_events(directory, events)
    _reseal(directory)
    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation.validate_sealed_arm(
            directory,
            trusted_provenance=_trusted_provenance(directory),
        )


def test_pair_and_campaign_consume_full_ledger_without_retry_and_keep_unfavorable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validation = _validation()
    projection_calls: list[object] = []
    projection_owner = validation
    projection_name = "epoch_structural_projection"
    if not hasattr(projection_owner, projection_name):
        projection_owner = validation.n31_crash_pair
        projection_name = "_epoch1_structural_projection"
    original_projection = getattr(projection_owner, projection_name)

    def exact_projection(decoded: object) -> object:
        projection_calls.append(decoded)
        return original_projection(decoded)

    monkeypatch.setattr(
        projection_owner,
        projection_name,
        exact_projection,
    )
    campaign_runner = fixture._runner()
    plan = fixture._plan(campaign_runner)
    source_children = fixture._children(plan, tmp_path / "source")
    for child in source_children:
        _complete_child(child)

    pair_id = "pair-03"
    pair_source = [child for child in source_children if child["pair_id"] == pair_id]
    assert {child["arm"] for child in pair_source} == {"control", "adaptive"}
    pair_root = tmp_path / "pair"
    pair_children = [
        _copy_child(child, pair_root / "children" / str(child["arm"]))
        for child in pair_source
    ]
    _write_json(
        pair_root / "pair-receipt.json",
        {
            "schema_version": 1,
            "pair_id": pair_id,
            "automatic_retries": 0,
            "replacement_policy": "none",
            "children": [
                {
                    "arm": child["arm"],
                    "path": str(
                        Path(child["sealed_child_directory"]).relative_to(pair_root)
                    ),
                    "tree_sha256": child["child_tree_sha256"],
                    "seal_sha256": child["child_seal_sha256"],
                }
                for child in pair_children
            ],
        },
    )
    pair_parent_seal = fixture._archive().create_evidence_seal(pair_root)
    assert fixture._archive().verify_evidence_seal(pair_root) == pair_parent_seal
    pair_trusted = _aggregate_trusted_provenance(pair_root, pair_children)
    assert len(
        {
            (entry["tree_sha256"], entry["seal_sha256"])
            for entry in pair_trusted["children"].values()
        }
    ) == 2
    pair = _document(
        validation.validate_sealed_pair(
            pair_root,
            trusted_provenance=pair_trusted,
        )
    )
    assert pair["verdict"] == "PASS"
    assert pair["scientific_outcome"] == "UNFAVORABLE"
    assert pair["retained"] is True

    campaign_root = tmp_path / "campaign"
    copied_children = [
        _copy_child(
            child,
            campaign_root / "children" / str(child["slot_id"]),
        )
        for child in source_children
    ]
    ledger = fixture._ledger(plan)
    assert [
        (slot["slot_id"], slot["pair_id"], slot["arm"])
        for slot in plan["slots"]
    ] == [
        (slot["slot_id"], slot["pair_id"], slot["arm"])
        for slot in fixture._expected_slots()
    ]
    by_slot = {str(child["slot_id"]): child for child in copied_children}
    for record in ledger:
        child = by_slot[str(record["slot_id"])]
        record["child_tree_sha256"] = child["child_tree_sha256"]
        record["child_seal_sha256"] = child["child_seal_sha256"]
    fixture._rehash_ledger(plan, ledger)
    _write_json(campaign_root / "plan.json", plan)
    (campaign_root / "campaign-ledger.jsonl").write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in ledger),
        encoding="utf-8",
    )
    campaign_parent_seal = fixture._archive().create_evidence_seal(campaign_root)
    assert (
        fixture._archive().verify_evidence_seal(campaign_root)
        == campaign_parent_seal
    )
    campaign_trusted = _aggregate_trusted_provenance(
        campaign_root,
        copied_children,
    )
    assert len(
        {
            (entry["tree_sha256"], entry["seal_sha256"])
            for entry in campaign_trusted["children"].values()
        }
    ) == 10
    campaign = _document(
        validation.validate_sealed_campaign(
            campaign_root,
            trusted_provenance=campaign_trusted,
        )
    )
    assert campaign["verdict"] == "PASS"
    assert campaign["terminal_slot_count"] == 10
    assert campaign["pair_count"] == 5
    assert campaign["automatic_retries"] == 0
    assert campaign["replacement_policy"] == "none"
    assert any(pair["scientific_outcome"] == "UNFAVORABLE" for pair in campaign["pairs"])
    assert campaign["figure_eligible"] is True

    truncated = ledger[:-1]
    (campaign_root / "campaign-ledger.jsonl").write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in truncated),
        encoding="utf-8",
    )
    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation.validate_sealed_campaign(
            campaign_root,
            trusted_provenance=_aggregate_trusted_provenance(
                campaign_root,
                copied_children,
            ),
        )
    assert len(projection_calls) >= 2
    assert all(getattr(decoded, "epoch_number") == 1 for decoded in projection_calls)


def test_sealed_pair_requires_one_exact_issuer_identity_across_arms(
    tmp_path: Path,
) -> None:
    validation = _validation()
    plan = fixture._plan(fixture._runner())
    pair_id = "pair-03"
    source_children = [
        child
        for child in fixture._children(plan, tmp_path / "source")
        if child["pair_id"] == pair_id
    ]
    for child in source_children:
        _complete_child(child)
    pair_root = tmp_path / "pair"
    children = [
        _copy_child(child, pair_root / "children" / str(child["arm"]))
        for child in source_children
    ]
    _write_json(
        pair_root / "pair-receipt.json",
        {
            "schema_version": 1,
            "pair_id": pair_id,
            "automatic_retries": 0,
            "replacement_policy": "none",
            "children": [
                {
                    "arm": child["arm"],
                    "path": str(
                        Path(child["sealed_child_directory"]).relative_to(pair_root)
                    ),
                    "tree_sha256": child["child_tree_sha256"],
                    "seal_sha256": child["child_seal_sha256"],
                }
                for child in children
            ],
        },
    )
    fixture._archive().create_evidence_seal(pair_root)
    assert validation.validate_sealed_pair(
        pair_root,
        trusted_provenance=_aggregate_trusted_provenance(pair_root, children),
    )["verdict"] == "PASS"

    adaptive = next(child for child in children if child["arm"] == "adaptive")
    adaptive_directory = adaptive["sealed_child_directory"]
    assert isinstance(adaptive_directory, Path)
    independent_public_key = _resign_child_with_independent_issuer(
        adaptive_directory
    )
    assert independent_public_key != native_fixture.ISSUER_PUBLIC_KEY
    adaptive_seal = fixture._archive().verify_evidence_seal(adaptive_directory)
    receipt_path = pair_root / "pair-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    adaptive_entry = next(
        entry for entry in receipt["children"] if entry["arm"] == "adaptive"
    )
    adaptive_entry["tree_sha256"] = adaptive_seal.tree_sha256
    adaptive_entry["seal_sha256"] = adaptive_seal.seal_sha256
    _write_json(receipt_path, receipt)
    _reseal(pair_root)
    assert validation.validate_sealed_arm(
        adaptive_directory,
        trusted_provenance=_trusted_provenance(adaptive_directory),
    )["verdict"] == "PASS"
    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation.validate_sealed_pair(
            pair_root,
            trusted_provenance=_aggregate_trusted_provenance(pair_root, children),
        )
