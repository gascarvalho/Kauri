"""Red-first source-blind validation contracts for focused crash pairs."""

from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import asdict, is_dataclass, replace
import hashlib
import importlib
import inspect
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from experiments.adaptive.tests import test_run_n31_crash_pair_campaign as fixture
from experiments.adaptive.tests import test_n31_crash_pair_contract as native_fixture
from experiments.adaptive.tests import (
    test_focused_crash_pair_runtime as runtime_fixture,
)

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


def _complete_child(
    child: dict[str, object], *, profile_path: Path = runtime_fixture.N31_PROFILE
) -> None:
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    (directory / "evidence-seal.json").unlink(missing_ok=True)
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    proof_source = runtime_fixture._topology_proof_path(
        profile_path,
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
    members = tuple(range(profile["protocol"]["N"]))
    fanout = profile["protocol"]["fanout"]
    pipeline = profile["protocol"]["pipeline_stretch"]
    (directory / "treegen.conf").write_text(
        "".join(
            " ".join(
                (
                    f"fan:{fanout}",
                    f"pipe:{pipeline}",
                    *(str(replica) for replica in members[offset:] + members[:offset]),
                )
            )
            + "\n"
            for offset in range(len(members))
        ),
        encoding="ascii",
    )
    (directory / "config").mkdir(parents=True, exist_ok=True)
    (directory / "config" / "main.conf").write_text(
        "\n".join(
            (
                f"block-size = {profile['protocol']['transactions_per_block']}",
                "nworker = 2",
                "repnworker = 2",
                "stat-period = 210.0",
                "pace-maker = dummy",
                "proposer = 0",
                f"fan-out = {fanout}",
                "piped_latency = 1",
                f"async_blocks = {pipeline}",
                "base-timeout = 2.0",
                "prop-delay = 0.1",
                "aggregation-timeout = 1.0",
                "leader-progress-timeout = 8.0",
                "leader-activation-grace = 1.0",
                "client-ip = 127.0.0.1",
                "tree-generation = default",
                "tree-switch-period = "
                + (
                    "2"
                    if profile["profile_id"]
                    in {
                        "n7-f2-q5-two-crash-pair-smoke-v3",
                        "n31-f5-q21-three-crash-pair-v3",
                    }
                    else str(len(members))
                ),
                "epoch-protocol-mode = adaptive_v2",
                "epoch-change-issuer-id = 7",
                f"epoch-change-issuer-public-key = {native_fixture.ISSUER_PUBLIC_KEY}",
                "epoch-change-minimum-activation-delay = 5",
                "epoch-change-maximum-activation-delay = 5",
                "epoch-change-maximum-block-extra-bytes = 65536",
                "epoch-change-maximum-ancestry-blocks = 4096",
                "epoch-manager-address = 127.0.0.1:20062",
                "epoch-manager-tls-cert = 00",
                "max-rep-msg = 8388608",
                *(f"replica = replica-{replica}" for replica in members),
            )
        )
        + "\n",
        encoding="ascii",
    )
    manager_argv, manager_input = native_fixture._safe_manager_boundary()
    if (
        child["arm"] == "control"
        and profile.get("profile_id") in _validation()._FCRASH_H_V3_PROFILE_IDS
    ):
        arguments = list(manager_argv)
        second_request = [
            index
            for index, argument in enumerate(arguments)
            if argument == "--transition-request"
        ][1]
        del arguments[second_request : second_request + 4]
        manager_argv = tuple(arguments)
        manager_input = {
            **manager_input,
            "requested_argv": list(manager_argv),
            "observed_argv": list(manager_argv),
        }
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
            if (
                event["event_type"] == "epoch.command_committed"
                and payload.get("successor_epoch_number") == 2
            ):
                event["payload"] = fixture._command_payload(epoch2, 11)
            elif (
                event["event_type"] == "epoch.activated"
                and payload.get("epoch_number") == 2
            ):
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
            if (
                event["event_type"] == "epoch.command_committed"
                and payload.get("successor_epoch_number") == 1
            ):
                event["payload"] = fixture._command_payload(control_epoch1, 4)
            elif (
                event["event_type"] == "epoch.activated"
                and payload.get("epoch_number") == 1
            ):
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
                {(event["source_kind"], event["source_id"]) for event in events}
            )
        },
    )
    _write_json(directory / "derived" / "phase-windows.json", {"phases": []})
    _write_json(directory / "derived" / "throughput.json", {"rows": []})
    _write_json(directory / "cleanup.json", {"complete": True})
    seal = fixture._archive().create_evidence_seal(directory)
    required = {
        "config/main.conf",
        "treegen.conf",
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


@pytest.mark.parametrize(
    ("relative_path", "old", "new"),
    (
        ("treegen.conf", "fan:5 pipe:2 20", "fan:1 pipe:0 20"),
        (
            "config/main.conf",
            "leader-progress-timeout = 8.0",
            "leader-progress-timeout = 2.0",
        ),
        ("config/main.conf", "fan-out = 5", "fan-out = 2"),
        (
            "config/main.conf",
            "leader-progress-timeout = 8.0",
            "leader-progress-timeout = 8.0\n leader-progress-timeout = 2.0",
        ),
        (
            "config/main.conf",
            "epoch-protocol-mode = adaptive_v2",
            "epoch-protocol-mode = adaptive_v2\ndefault_epoch = alternate.conf",
        ),
        (
            "config/main.conf",
            "epoch-protocol-mode = adaptive_v2",
            "epoch-protocol-mode = adaptive_v2\nconf = alternate.conf",
        ),
        (
            "config/main.conf",
            "fan-out = 5",
            "fan-out = 5\nF = 2",
        ),
        (
            "config/main.conf",
            "epoch-protocol-mode = adaptive_v2",
            "epoch-protocol-mode = adaptive_v2\nc = alternate.conf",
        ),
    ),
)
def test_sealed_arm_rejects_client_topology_or_timer_configuration_drift(
    relative_path: str,
    old: str,
    new: str,
    tmp_path: Path,
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    assert (
        _validation().validate_sealed_arm(
            directory,
            trusted_provenance=_trusted_provenance(directory),
        )["verdict"]
        == "PASS"
    )
    path = directory / relative_path
    path.write_text(
        path.read_text(encoding="ascii").replace(old, new, 1),
        encoding="ascii",
    )
    _reseal(directory)
    with pytest.raises(
        _validation().FocusedCrashPairValidationError,
        match="configuration",
    ):
        _validation().validate_sealed_arm(
            directory,
            trusted_provenance=_trusted_provenance(directory),
        )


def _trusted_provenance(directory: Path) -> dict[str, object]:
    profile = json.loads((directory / "profile.json").read_text(encoding="utf-8"))
    build = json.loads(
        (directory / "runtime" / "build-provenance.json").read_text(encoding="utf-8")
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
    pair_arms = [str(child.get("arm")) for child in children]
    pair_receipt = len(children) == 2 and set(pair_arms) == {"control", "adaptive"}
    for child in children:
        directory = child["sealed_child_directory"]
        assert isinstance(directory, Path)
        relative = (
            str(child["arm"]) if pair_receipt else str(directory.relative_to(root))
        )
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
    assert contract["authoritative_source_id"] == expected["authoritative_source_id"]
    assert contract["fault_target_count"] == expected["fault_target_count"]
    assert contract["manager_blinding_target_count"] == expected["fault_target_count"]
    assert contract["control_transition_count"] == expected["control_transition_count"]
    assert (
        contract["adaptive_transition_count"] == expected["adaptive_transition_count"]
    )


def test_validator_rejects_rebound_noncanonical_v5_profile(tmp_path: Path) -> None:
    validation = _validation()
    profile = json.loads(runtime_fixture.N31_PROFILE_V5.read_text(encoding="utf-8"))
    source_proof = runtime_fixture._topology_proof_path(
        runtime_fixture.N31_PROFILE_V5, profile
    )
    proof = json.loads(source_proof.read_text(encoding="utf-8"))
    profile["timers"]["arm_hard_deadline_seconds"] = 481
    proof["profile_sha256"] = runtime_fixture._canonical_profile_sha256(profile)
    proof_path = tmp_path / profile["topology"]["proof_path"]
    proof_path.parent.mkdir(parents=True)
    _write_json(proof_path, proof)
    profile["topology"]["proof_sha256"] = hashlib.sha256(
        proof_path.read_bytes()
    ).hexdigest()
    _write_json(tmp_path / "profile.json", profile)
    with pytest.raises(
        validation.FocusedCrashPairValidationError,
        match="not the frozen reviewed identity",
    ):
        validation.validation_contract_from_profile(tmp_path)


@pytest.mark.parametrize(
    "profile_path", (runtime_fixture.N7_PROFILE_V2, runtime_fixture.N31_PROFILE_V2)
)
def test_independent_validator_rechecks_fcrash_h_guard_and_deadline_caps(
    profile_path: Path,
    tmp_path: Path,
) -> None:
    """The sealed-validator helper owns the guard; it cannot trust the runner."""

    validation = _validation()
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    proof_source = runtime_fixture._topology_proof_path(profile_path, profile)
    proof_path = tmp_path / profile["topology"]["proof_path"]
    proof_path.parent.mkdir(parents=True, exist_ok=True)
    proof_path.write_bytes(proof_source.read_bytes())
    _write_json(tmp_path / "profile.json", profile)
    contract = _document(validation.validation_contract_from_profile(tmp_path))
    coverage = contract["reporter_coverage_plan"]
    fault_ns = 2_000_000_000
    observations = [
        {
            "epoch_number": 0,
            "tree_id": next(
                item["tree_id"]
                for item in target["first_qualifying_reporters"]
                if item["reporter_id"] == reporter
            ),
            "observed_replica_id": target["target_replica_id"],
            "reporter_id": reporter,
            "outcome": "timeout",
            "compensated": False,
            "source_monotonic_ns": fault_ns + 1 + ordinal,
        }
        for target in coverage["targets"]
        for reporter in target["authenticated_reporter_ids"]
        for ordinal in range(2)
    ]
    witness = {
        "fault_monotonic_ns": fault_ns,
        "nonresponse_monotonic_ns": fault_ns + 2,
        "snapshot_audit_monotonic_ns": fault_ns + 3,
        "epoch1_activation_monotonic_ns": fault_ns + 4,
        "epoch2_activation_monotonic_ns": fault_ns + 5,
        "timeout_observations": observations,
        "guard_drawdowns": {
            str(target["target_replica_id"]): -int(coverage["minimum_score_drop"])
            for target in coverage["targets"]
        },
    }
    assert validation.validate_fcrash_h_evidence(contract, witness) is None

    for mutation in (
        "compensated",
        "pre-fault",
        "wrong-tree",
        "healed-score",
        "evidence-after-audit",
        "evidence-cap",
        "epoch1-cap",
        "epoch2-cap",
    ):
        changed = deepcopy(witness)
        if mutation == "compensated":
            changed["timeout_observations"][0]["compensated"] = True
        elif mutation == "pre-fault":
            changed["timeout_observations"][0]["source_monotonic_ns"] = fault_ns
        elif mutation == "wrong-tree":
            changed["timeout_observations"][0]["tree_id"] = -1
        elif mutation == "healed-score":
            first_target = next(iter(changed["guard_drawdowns"]))
            changed["guard_drawdowns"][first_target] = 0
        elif mutation == "evidence-after-audit":
            changed["timeout_observations"][0]["source_monotonic_ns"] = changed[
                "snapshot_audit_monotonic_ns"
            ]
        elif mutation == "evidence-cap":
            changed["snapshot_audit_monotonic_ns"] = (
                fault_ns
                + int(coverage["deadlines_seconds"]["evidence_seconds"]) * 1_000_000_000
            )
        elif mutation == "epoch1-cap":
            changed["epoch1_activation_monotonic_ns"] = (
                fault_ns
                + int(coverage["deadlines_seconds"]["epoch1_activation_seconds"])
                * 1_000_000_000
            )
        else:
            changed["epoch2_activation_monotonic_ns"] = (
                changed["epoch1_activation_monotonic_ns"]
                + int(coverage["deadlines_seconds"]["optimization_activation_seconds"])
                * 1_000_000_000
            )
        with pytest.raises(validation.FocusedCrashPairValidationError):
            validation.validate_fcrash_h_evidence(contract, changed)


def _v3_progress_contract(tmp_path: Path) -> dict[str, object]:
    profile = json.loads(runtime_fixture.N7_PROFILE_V3.read_text(encoding="utf-8"))
    proof_source = runtime_fixture._topology_proof_path(
        runtime_fixture.N7_PROFILE_V3, profile
    )
    proof_path = tmp_path / profile["topology"]["proof_path"]
    proof_path.parent.mkdir(parents=True, exist_ok=True)
    proof_path.write_bytes(proof_source.read_bytes())
    _write_json(tmp_path / "profile.json", profile)
    return _document(_validation().validation_contract_from_profile(tmp_path))


def _v3_progress_events(contract: Mapping[str, object]) -> list[dict[str, object]]:
    run_id = "v3-progress-run"
    source_id = str(contract["authoritative_source_id"])
    instance = f"{run_id}-{source_id}-550e8400-e29b-41d4-a716-446655440000"
    digest = str(contract["epoch_zero_digest"])
    lifecycle = [
        {
            "event_schema_version": 1,
            "run_id": run_id,
            "source_kind": "replica",
            "source_id": source_id,
            "source_instance": instance,
            "source_sequence": index + 1,
            "source_monotonic_ns": 10 + index,
            "event_type": event_type,
            "payload": {},
        }
        for index, event_type in enumerate(("process.started", "process.ready"))
    ]
    starting_tree = int(contract["reporter_coverage_plan"]["active_tree_id"])
    members = [int(member) for member in contract["members"]]
    configurations = [
        {
            "event_schema_version": 1,
            "run_id": run_id,
            "source_kind": "replica",
            "source_id": source_id,
            "source_instance": instance,
            "source_sequence": 3 + position,
            "source_monotonic_ns": 88 + position,
            "event_type": "adaptive.configuration_active",
            "payload": {
                "epoch_number": 0,
                "tree_id": members[position % len(members)],
                "epoch_digest": digest,
            },
        }
        for position in range(len(members))
    ]
    configurations.extend(
        {
            **configuration,
            "source_sequence": 3 + len(members) + position,
            "source_monotonic_ns": 101 + position,
            "payload": {
                **configuration["payload"],
                "tree_id": members[
                    (members.index(starting_tree) + position + 1) % len(members)
                ],
            },
        }
        for position, configuration in enumerate(configurations[:5])
    )
    commits = [
        {
            "event_schema_version": 1,
            "run_id": run_id,
            "source_kind": "replica",
            "source_id": source_id,
            "source_instance": instance,
            "source_sequence": index + 16,
            "source_monotonic_ns": 120 + index,
            "event_type": "block.committed",
            "payload": {
                "block_height": index + 1,
                "block_hash": f"{index + 1:064x}",
                "parent_hash": "00" * 32 if index == 0 else f"{index:064x}",
                "transaction_count": 0 if index % 2 == 0 else 1000,
                "commit_batch_index": index % 3,
                "designated_observer": True,
                "view_generation": 6 if index == 0 else 12 if index == 1 else 5,
                "decision_proof": {
                    "epoch_number": 0,
                    "epoch_digest": digest,
                    "block_hash": f"{index + 1:064x}",
                    "tree_id": 5 if index == 0 else 4,
                },
            },
        }
        for index in range(12)
    ]
    return [*lifecycle, *configurations, *commits]


def test_v3_progress_witness_is_recomputed_from_exact_authoritative_commits(
    tmp_path: Path,
) -> None:
    validation = _validation()
    contract = _v3_progress_contract(tmp_path)
    progress = validation._fcrash_h_postfault_progress(
        contract,
        _v3_progress_events(contract),
        fault_ns=100,
        prefault_ns=100,
        audit_ns=200,
    )
    assert progress == {
        "required_tree_positions": 6,
        "actual_tree_positions": 6,
        "starting_tree_id": 6,
        "observed_tree_ids": [6, 0, 1, 2, 3, 4],
    }


def test_v3_sealed_progress_rejects_configuration_after_signal_request(
    tmp_path: Path,
) -> None:
    validation = _validation()
    contract = _v3_progress_contract(tmp_path)
    events = _v3_progress_events(contract)
    between_request_and_confirmation = deepcopy(events[3])
    between_request_and_confirmation["source_sequence"] = 4
    between_request_and_confirmation["source_monotonic_ns"] = 99
    events.append(between_request_and_confirmation)
    with pytest.raises(validation.FocusedCrashPairValidationError, match="fault batch"):
        validation._fcrash_h_postfault_progress(
            contract, events, fault_ns=100, prefault_ns=95, audit_ns=200
        )


@pytest.mark.parametrize(
    ("boundary_ns", "foreign_source"),
    ((100, False), (110, False), (105, True)),
)
def test_v3_sealed_progress_rejects_any_member_configuration_in_fault_batch(
    boundary_ns: int,
    foreign_source: bool,
    tmp_path: Path,
) -> None:
    validation = _validation()
    contract = _v3_progress_contract(tmp_path)
    events = _v3_progress_events(contract)
    postfault = [
        event
        for event in events
        if event["event_type"] == "adaptive.configuration_active"
        and event["source_monotonic_ns"] > 100
    ]
    for position, event in enumerate(postfault, start=1):
        event["source_monotonic_ns"] = 110 + position
    injected = deepcopy(postfault[0])
    if foreign_source:
        injected["source_id"] = "replica-0"
        injected["source_instance"] = "v3-progress-run-replica-0-foreign-uuid"
    injected.update(
        {
            "source_sequence": 1,
            "source_monotonic_ns": boundary_ns,
        }
    )
    events.append(injected)
    with pytest.raises(validation.FocusedCrashPairValidationError, match="fault batch"):
        validation._fcrash_h_postfault_progress(
            contract, events, fault_ns=110, prefault_ns=100, audit_ns=200
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "wrong-epoch",
        "wrong-digest",
        "malformed-proof",
        "missing-position",
        "unbound-lifecycle",
        "multiple-lifecycle-instances",
        "wrong-proof-hash",
        "wrong-proof-tree",
        "extra-proof-key",
        "proof-epoch-bool",
        "historical-config-epoch-bool",
        "historical-config-tree-bool",
        "out-of-order",
        "view-generation-zero",
        "view-generation-noninteger",
        "view-generation-overflow",
        "view-generation-future",
        "view-generation-wrong-epoch-packed",
        "view-generation-wrong-existing",
        "activation-after-commit",
        "batch-negative",
        "batch-noninteger",
        "batch-overflow",
        "transactions-negative",
        "transactions-noninteger",
        "transactions-overflow",
        "transactions-unexpected-workload",
    ),
)
def test_v3_progress_witness_rejects_non_authoritative_or_insufficient_raw_commits(
    mutation: str, tmp_path: Path
) -> None:
    validation = _validation()
    contract = _v3_progress_contract(tmp_path)
    events = _v3_progress_events(contract)
    target = events[-1]
    if mutation == "wrong-epoch":
        target["payload"]["decision_proof"]["epoch_number"] = 1  # type: ignore[index]
    elif mutation == "wrong-digest":
        target["payload"]["decision_proof"]["epoch_digest"] = "ff" * 32  # type: ignore[index]
    elif mutation == "malformed-proof":
        target["payload"]["decision_proof"] = {}  # type: ignore[index]
    elif mutation == "missing-position":
        events.pop(7)
    elif mutation == "unbound-lifecycle":
        events = [event for event in events if event["event_type"] != "process.ready"]
    elif mutation == "multiple-lifecycle-instances":
        events[1]["source_instance"] = "v3-progress-run-replica-2-other-uuid"
    elif mutation == "wrong-proof-hash":
        target["payload"]["decision_proof"]["block_hash"] = "ff" * 32  # type: ignore[index]
    elif mutation == "wrong-proof-tree":
        target["payload"]["decision_proof"]["tree_id"] = 99  # type: ignore[index]
    elif mutation == "extra-proof-key":
        target["payload"]["decision_proof"]["extra"] = True  # type: ignore[index]
    elif mutation == "proof-epoch-bool":
        target["payload"]["decision_proof"]["epoch_number"] = False  # type: ignore[index]
    elif mutation == "historical-config-epoch-bool":
        next(
            event
            for event in events
            if event["event_type"] == "adaptive.configuration_active"
            and event["source_monotonic_ns"] < 100
        )["payload"][
            "epoch_number"
        ] = False  # type: ignore[index]
    elif mutation == "historical-config-tree-bool":
        next(
            event
            for event in events
            if event["event_type"] == "adaptive.configuration_active"
            and event["source_monotonic_ns"] < 100
        )["payload"][
            "tree_id"
        ] = False  # type: ignore[index]
    elif mutation == "out-of-order":
        target["source_sequence"] = 1
    else:
        payload = target["payload"]
        if mutation == "view-generation-zero":
            payload["view_generation"] = 0
        elif mutation == "view-generation-noninteger":
            payload["view_generation"] = "132"
        elif mutation == "view-generation-overflow":
            payload["view_generation"] = 1 << 64
        elif mutation == "view-generation-future":
            payload["view_generation"] = 13
        elif mutation == "view-generation-wrong-epoch-packed":
            payload["view_generation"] = (1 << 32) + 1
        elif mutation == "view-generation-wrong-existing":
            payload["view_generation"] = 6
        elif mutation == "activation-after-commit":
            target = next(
                event
                for event in events
                if event["event_type"] == "block.committed"
                and event["payload"]["view_generation"] == 12
            )
            activation = next(
                event
                for event in events
                if event["event_type"] == "adaptive.configuration_active"
                and event["payload"]["tree_id"] == 4
                and event["source_sequence"] == 14
            )
            activation["source_sequence"] = int(target["source_sequence"]) + 1
            activation["source_monotonic_ns"] = int(target["source_monotonic_ns"]) + 1
        elif mutation == "batch-negative":
            payload["commit_batch_index"] = -1
        elif mutation == "batch-noninteger":
            payload["commit_batch_index"] = "2"
        elif mutation == "batch-overflow":
            payload["commit_batch_index"] = 1 << 64
        elif mutation == "transactions-negative":
            payload["transaction_count"] = -1
        elif mutation == "transactions-noninteger":
            payload["transaction_count"] = "1000"
        elif mutation == "transactions-overflow":
            payload["transaction_count"] = 1 << 64
        else:
            payload["transaction_count"] = 5
    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation._fcrash_h_postfault_progress(
            contract, events, fault_ns=100, prefault_ns=100, audit_ns=200
        )


def test_v3_progress_witness_rejects_duplicate_cyclic_configuration(
    tmp_path: Path,
) -> None:
    validation = _validation()
    contract = _v3_progress_contract(tmp_path)
    events = _v3_progress_events(contract)
    duplicate = deepcopy(
        next(
            event
            for event in reversed(events)
            if event["event_type"] == "adaptive.configuration_active"
        )
    )
    duplicate["source_sequence"] = (
        max(int(event["source_sequence"]) for event in events) + 1
    )
    duplicate["source_monotonic_ns"] = (
        max(int(event["source_monotonic_ns"]) for event in events) + 1
    )
    events.append(duplicate)
    with pytest.raises(validation.FocusedCrashPairValidationError, match="cyclic"):
        validation._fcrash_h_postfault_progress(
            contract, events, fault_ns=100, prefault_ns=100, audit_ns=200
        )


def test_sealed_prefault_barrier_uses_source_sequence_when_timestamps_tie(
    tmp_path: Path,
) -> None:
    validation = _validation()
    contract = _v3_progress_contract(tmp_path)
    events: list[dict[str, object]] = []
    active_tree = int(contract["reporter_coverage_plan"]["active_tree_id"])
    for replica in contract["members"]:
        for sequence, tree in ((1, active_tree), (2, 0)):
            events.append(
                {
                    "source_kind": "replica",
                    "source_id": f"replica-{replica}",
                    "source_monotonic_ns": 1,
                    "source_sequence": sequence,
                    "event_type": "adaptive.configuration_active",
                    "payload": {
                        "epoch_number": 0,
                        "tree_id": tree,
                        "epoch_digest": contract["epoch_zero_digest"],
                    },
                }
            )
    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation._validate_prefault_active_configuration(contract, events, fault_ns=2)


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


def test_v3_containment_roots_preserve_eligible_baselines_before_replacement() -> None:
    validation = _validation()

    # N7 native scorer order differs from containment roots: healthy baseline
    # roots retain their tree slots while failed slots consume replacements.
    assert validation._containment_roots(  # type: ignore[attr-defined]
        [2, 6, 3, 4, 5], {"quorum": 5}
    ) == (6, 5, 2, 3, 4)

    # The same rule must not collapse an N31-style mixed placement into the
    # first Q ranked members.
    ranked = [member for member in range(21) if member not in {1, 7}] + [25, 26]
    expected = list(range(21))
    expected[1] = 25
    expected[7] = 26
    assert validation._containment_roots(  # type: ignore[attr-defined]
        ranked, {"quorum": 21}
    ) == tuple(expected)


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
    assert (
        validation.validate_sealed_arm(
            directory,
            trusted_provenance=trusted,
        )["verdict"]
        == "PASS"
    )
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


def test_source_blind_validator_accepts_redacted_live_manager_boundary(
    tmp_path: Path,
) -> None:
    validation = _validation()
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "control"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    launch_path = directory / "runtime/launch-arguments.json"
    observed_path = directory / "runtime/manager-observed-argv.json"
    input_path = directory / "runtime/manager-input.json"
    launch = json.loads(launch_path.read_text(encoding="utf-8"))
    original = launch["manager_argv"]
    secrets = {
        original[original.index(option) + 1]
        for option in ("--tls-privkey", "--issuer-private-key")
    }
    profiled_runtime = runtime_fixture._runtime().profiled_fault_runtime
    normalized = profiled_runtime.normalized_manager_argv(original)
    _write_json(launch_path, {"manager_argv": normalized})
    _write_json(observed_path, {"argv": normalized})
    manager_input = json.loads(input_path.read_text(encoding="utf-8"))
    manager_input["requested_argv"] = normalized
    manager_input["observed_argv"] = normalized
    _write_json(input_path, manager_input)
    _reseal(directory)

    sealed_text = "".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (launch_path, observed_path, input_path)
    )
    assert all(secret not in sealed_text for secret in secrets)
    assert (
        validation.validate_sealed_arm(
            directory,
            trusted_provenance=_trusted_provenance(directory),
        )["verdict"]
        == "PASS"
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
        assert (
            validation.validate_sealed_arm(
                directory,
                trusted_provenance=_trusted_provenance(directory),
            )["verdict"]
            == "PASS"
        )
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
        source_events = [event for event in events if event["source_id"] == source_id]
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
        str(event["source_id"]): str(event["source_instance"]) for event in events
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
        for sequence, event_type in enumerate(
            ("process.started", "process.ready"), start=1
        ):
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
                event["source_monotonic_ns"] = base * second + int(
                    payload["ingestion_sequence"]
                ) * (second // 10)
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
        partial_events = [
            deepcopy(event)
            for event in events
            if not (
                event["source_id"] == "replica-30"
                and event["event_type"] == "process.ready"
            )
        ]
        for event in partial_events:
            if event["source_id"] == "replica-30" and int(event["source_sequence"]) > 2:
                event["source_sequence"] = int(event["source_sequence"]) - 1
        _write_events(directory, partial_events)
        assert source.poll("readiness") is None
        _write_events(directory, events)
        assert source.poll("readiness") == {"ready": True}
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


def test_raw_nonresponse_waits_for_its_first_native_snapshot_audit(
    tmp_path: Path,
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    events = _add_complete_ready_barrier(directory, include_client=False)
    source = _raw_source(directory)
    assert source.poll("nonresponse") is not None
    assert source.poll("ranking") is not None

    for predecessor, phase in ((0, "nonresponse"), (1, "ranking")):
        missing_audit = [
            deepcopy(event)
            for event in events
            if not (
                event["source_id"] == "adaptive-manager"
                and event["event_type"] == "adaptive_v2_evidence_snapshot"
                and event["payload"]["predecessor_epoch_number"] == predecessor
            )
        ]
        removed_sequence = next(
            int(event["source_sequence"])
            for event in events
            if event["source_id"] == "adaptive-manager"
            and event["event_type"] == "adaptive_v2_evidence_snapshot"
            and event["payload"]["predecessor_epoch_number"] == predecessor
        )
        for event in missing_audit:
            if (
                event["source_id"] == "adaptive-manager"
                and int(event["source_sequence"]) > removed_sequence
            ):
                event["source_sequence"] = int(event["source_sequence"]) - 1
        _write_events(directory, missing_audit)
        assert source.poll(phase) is None

        _write_events(directory, events)
        assert source.poll(phase) is not None


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
    assert (
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
        is None
    )
    assert source._common_commit(events, 2) is None


def test_raw_common_commit_rejects_ambiguous_quorum_witnessed_identity(
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
    original = next(
        event
        for event in events
        if event["event_type"] == "block.committed"
        and event["payload"]["decision_proof"]["epoch_number"] == 1
    )
    conflicting = deepcopy(original)
    conflicting["payload"]["block_hash"] = "f" * 64
    conflicting["payload"]["decision_proof"]["block_hash"] = "f" * 64
    events.append(conflicting)
    for observed in [
        event
        for event in events
        if event["event_type"] == "block.commit_observed"
        and event["payload"]["block_height"] == original["payload"]["block_height"]
    ]:
        conflicting_observation = deepcopy(observed)
        conflicting_observation["payload"]["block_hash"] = "f" * 64
        events.append(conflicting_observation)
    with pytest.raises(
        runtime_fixture._runtime().FocusedCrashPairRuntimeError, match="ambiguous"
    ):
        source._common_commit(events, 1)


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
    assert (
        validation.validate_sealed_arm(
            directory,
            trusted_provenance=_trusted_provenance(directory),
        )["verdict"]
        == "PASS"
    )

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


def _append_successful_manager_shutdown(
    directory: Path, *, epochs: tuple[int, ...] = (1,)
) -> None:
    """Append production-shaped successful manager terminals and orderly tail."""

    events = _load_events(directory)
    profile = json.loads((directory / "profile.json").read_text(encoding="utf-8"))
    manager = next(
        event for event in events if event["source_kind"] == "adaptation_manager"
    )
    manager_events = [
        event for event in events if event["source_kind"] == "adaptation_manager"
    ]
    sequence = max(int(event["source_sequence"]) for event in manager_events)
    timestamp = max(int(event["source_monotonic_ns"]) for event in manager_events)
    source = _raw_source(directory)
    for epoch_number in epochs:
        command = next(
            event
            for event in events
            if event["event_type"] == "epoch.command_committed"
            and event["payload"]["successor_epoch_number"] == epoch_number
        )["payload"]
        activation = next(
            event
            for event in events
            if event["event_type"] == "epoch.activated"
            and event["payload"]["epoch_number"] == epoch_number
        )["payload"]
        epoch = source._bundle(epoch_number)[1]
        ranking = source._ranking(events, predecessor_epoch=epoch_number - 1)
        audit = next(
            event
            for event in events
            if event["source_kind"] == "adaptation_manager"
            and event["event_type"] == "adaptive_v2_evidence_snapshot"
            and event["payload"]["predecessor_epoch_number"] == epoch_number - 1
        )["payload"]
        winning = {
            "predecessor_epoch_number": command["predecessor_epoch_number"],
            "predecessor_epoch_digest": command["predecessor_epoch_digest"],
            "successor_epoch_number": command["successor_epoch_number"],
            "successor_epoch_digest": command["successor_epoch_digest"],
            "command_payload_digest": command["payload_digest"],
            "command_block_height": command["command_block_height"],
            "command_block_hash": command["command_block_hash"],
            "activation_delay_blocks": command["activation_delay_blocks"],
            "activation_height": activation["activation_height"],
        }
        terminal = {
            "cycle_ordinal": epoch_number - 1,
            "policy_intent": (
                "fault_containment" if epoch_number == 1 else "performance_optimization"
            ),
            "outcome": "advanced",
            "reason": "successor_converged",
            "transition_artifact_id": (
                "e0-to-e1-containment" if epoch_number == 1 else "e1-to-e2-optimization"
            ),
            "predecessor_epoch_number": epoch_number - 1,
            "predecessor_epoch_digest": (
                profile["topology"]["epoch_zero_digest"]
                if epoch_number == 1
                else source._bundle(epoch_number - 1)[1].epoch_digest
            ),
            "successor_epoch_number": epoch_number,
            "successor_epoch_digest": epoch.epoch_digest,
            "command_payload_digest": epoch.command.payload_digest,
            "winning_activation": winning,
            "evidence_window_activation_generation": audit["activation_generation"],
            "baseline_evidence_cutoff": ranking["baseline_evidence_cutoff"],
            "current_evidence_cutoff": ranking["current_evidence_cutoff"],
        }
        sequence += 1
        timestamp += 1
        events.append(
            {
                "event_schema_version": 1,
                "run_id": manager["run_id"],
                "source_kind": "adaptation_manager",
                "source_id": "adaptive-manager",
                "source_instance": manager["source_instance"],
                "source_sequence": sequence,
                "source_monotonic_ns": timestamp,
                "event_type": "adaptive_v2_session_terminal",
                "payload": terminal,
            }
        )
    for event_type, payload in (
        ("process.stopping", {"exit_status": None}),
        ("process.stopped", {"exit_status": None}),
    ):
        sequence += 1
        timestamp += 1
        events.append(
            {
                "event_schema_version": 1,
                "run_id": manager["run_id"],
                "source_kind": "adaptation_manager",
                "source_id": "adaptive-manager",
                "source_instance": manager["source_instance"],
                "source_sequence": sequence,
                "source_monotonic_ns": timestamp,
                "event_type": event_type,
                "payload": payload,
            }
        )
    _write_events(directory, events)


def test_manager_clean_exit_requires_completed_native_transition_and_exact_tail(
    tmp_path: Path,
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "control"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    _append_successful_manager_shutdown(directory)
    process = runtime_fixture._FakeProcess(20_001)
    process.returncode = 0
    record = runtime_fixture.SimpleNamespace(
        name="adaptive-manager", replica_id=-1, process=process
    )
    source = runtime_fixture._runtime().FocusedRawEvidenceSource(
        run_directory=directory,
        poll_interval_s=0,
        timeout_s=1,
        process_records=(record,),
    )
    assert source.unexpected_exit_ids() == ()

    bundle_path = directory / "raw" / "epoch1.bundle"
    bundle = bundle_path.read_bytes()
    bundle_path.write_bytes(bundle[:-1] + bytes((bundle[-1] ^ 1,)))
    assert source.unexpected_exit_ids() == (-1,)
    bundle_path.write_bytes(bundle)
    assert source.unexpected_exit_ids() == ()

    events = _load_events(directory)
    terminal = next(
        event
        for event in events
        if event["event_type"] == "adaptive_v2_session_terminal"
    )
    terminal["payload"]["reason"] = "caller_failed"
    _write_events(directory, events)
    assert source.unexpected_exit_ids() == (-1,)


def _clean_manager_source(directory: Path, *, returncode: int) -> object:
    process = runtime_fixture._FakeProcess(20_001)
    process.returncode = returncode
    return runtime_fixture._runtime().FocusedRawEvidenceSource(
        run_directory=directory,
        poll_interval_s=0,
        timeout_s=1,
        process_records=(
            runtime_fixture.SimpleNamespace(
                name="adaptive-manager", replica_id=-1, process=process
            ),
        ),
    )


def test_manager_clean_exit_refreshes_one_stale_authenticated_snapshot(
    tmp_path: Path,
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "control"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    _append_successful_manager_shutdown(directory)
    events = _load_events(directory)
    terminal_sequence = next(
        int(event["source_sequence"])
        for event in events
        if event["source_kind"] == "adaptation_manager"
        and event["event_type"] == "adaptive_v2_session_terminal"
    )
    stale_events = [
        event
        for event in events
        if not (
            event["source_kind"] == "adaptation_manager"
            and int(event["source_sequence"]) >= terminal_sequence
        )
    ]
    assert (
        _clean_manager_source(directory, returncode=0).unexpected_exit_ids(stale_events)
        == ()
    )


@pytest.mark.parametrize("mutation", ("malformed-refresh", "nonzero-manager"))
def test_manager_clean_exit_stale_snapshot_refresh_remains_fail_closed(
    mutation: str, tmp_path: Path
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "control"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    _append_successful_manager_shutdown(directory)
    events = _load_events(directory)
    terminal_sequence = next(
        int(event["source_sequence"])
        for event in events
        if event["source_kind"] == "adaptation_manager"
        and event["event_type"] == "adaptive_v2_session_terminal"
    )
    stale_events = [
        event
        for event in events
        if not (
            event["source_kind"] == "adaptation_manager"
            and int(event["source_sequence"]) >= terminal_sequence
        )
    ]
    if mutation == "malformed-refresh":
        stopped = next(
            event
            for event in events
            if event["source_kind"] == "adaptation_manager"
            and event["event_type"] == "process.stopped"
        )
        stopped["payload"] = {"exit_status": 0}
        _write_events(directory, events)
        returncode = 0
    else:
        returncode = 1
    assert _clean_manager_source(directory, returncode=returncode).unexpected_exit_ids(
        stale_events
    ) == (-1,)


def _append_benign_post_terminal_activation_acknowledgements(
    directory: Path,
) -> list[dict[str, object]]:
    """Model the native post-terminal acknowledgment drain without new evidence."""

    events = _load_events(directory)
    manager_events = [
        event for event in events if event["source_kind"] == "adaptation_manager"
    ]
    terminal = [
        event
        for event in manager_events
        if event["event_type"] == "adaptive_v2_session_terminal"
    ][-1]
    stopping = next(
        event for event in manager_events if event["event_type"] == "process.stopping"
    )
    stopped = next(
        event for event in manager_events if event["event_type"] == "process.stopped"
    )
    events.remove(stopping)
    events.remove(stopped)
    winning = deepcopy(terminal["payload"]["winning_activation"])
    sequence = int(terminal["source_sequence"])
    timestamp = int(terminal["source_monotonic_ns"])
    acknowledgements: list[dict[str, object]] = []
    for replica_id in (30, 21, 5, 29, 3, 4, 0, 7, 6):
        for disposition in ("duplicate", "ack_sent"):
            sequence += 1
            timestamp += 1
            acknowledgements.append(
                {
                    **terminal,
                    "source_sequence": sequence,
                    "source_monotonic_ns": timestamp,
                    "event_type": "adaptive_v2_activation_observed",
                    "payload": {
                        "replica_id": replica_id,
                        "delivery_attempt": None,
                        "disposition": disposition,
                        "identity": deepcopy(winning),
                        "accepted_commit_count": 28,
                        "accepted_activation_count": 21,
                        "required_activation_count": 21,
                        "canonical_payload_digest": "e" * 64,
                        "failure_reason": None,
                    },
                }
            )
    events.extend(acknowledgements)
    for tail in (stopping, stopped):
        sequence += 1
        timestamp += 1
        tail["source_sequence"] = sequence
        tail["source_monotonic_ns"] = timestamp
        events.append(tail)
    _write_events(directory, events)
    return acknowledgements


def _append_final_cycle_commit_and_activation_ack_pairs(
    directory: Path,
) -> list[dict[str, object]]:
    """Model the native final-cycle commit and activation acknowledgment drain."""

    events = _load_events(directory)
    manager_events = [
        event for event in events if event["source_kind"] == "adaptation_manager"
    ]
    terminal = [
        event
        for event in manager_events
        if event["event_type"] == "adaptive_v2_session_terminal"
    ][-1]
    stopping = next(
        event for event in manager_events if event["event_type"] == "process.stopping"
    )
    stopped = next(
        event for event in manager_events if event["event_type"] == "process.stopped"
    )
    events.remove(stopping)
    events.remove(stopped)
    winning = deepcopy(terminal["payload"]["winning_activation"])
    sequence = int(terminal["source_sequence"])
    timestamp = int(terminal["source_monotonic_ns"])
    drain: list[dict[str, object]] = []
    for event_type, replica_id, digest in (
        ("adaptive_v2_commit_observed", 30, "c" * 64),
        ("adaptive_v2_activation_observed", 21, "a" * 64),
    ):
        for disposition in ("duplicate", "ack_sent"):
            sequence += 1
            timestamp += 1
            drain.append(
                {
                    **terminal,
                    "source_sequence": sequence,
                    "source_monotonic_ns": timestamp,
                    "event_type": event_type,
                    "payload": {
                        "replica_id": replica_id,
                        "delivery_attempt": None,
                        "disposition": disposition,
                        "identity": deepcopy(winning),
                        "accepted_commit_count": 28,
                        "accepted_activation_count": 21,
                        "required_activation_count": 21,
                        "canonical_payload_digest": digest,
                        "failure_reason": None,
                    },
                }
            )
    events.extend(drain)
    for tail in (stopping, stopped):
        sequence += 1
        timestamp += 1
        tail["source_sequence"] = sequence
        tail["source_monotonic_ns"] = timestamp
        events.append(tail)
    _write_events(directory, events)
    return drain


def test_adaptive_manager_clean_exit_allows_final_cycle_commit_and_activation_drain(
    tmp_path: Path,
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    _append_successful_manager_shutdown(directory, epochs=(1, 2))
    drain = _append_final_cycle_commit_and_activation_ack_pairs(directory)
    assert [event["event_type"] for event in drain] == [
        "adaptive_v2_commit_observed",
        "adaptive_v2_commit_observed",
        "adaptive_v2_activation_observed",
        "adaptive_v2_activation_observed",
    ]
    process = runtime_fixture._FakeProcess(20_001)
    process.returncode = 0
    source = runtime_fixture._runtime().FocusedRawEvidenceSource(
        run_directory=directory,
        poll_interval_s=0,
        timeout_s=1,
        process_records=(
            runtime_fixture.SimpleNamespace(
                name="adaptive-manager", replica_id=-1, process=process
            ),
        ),
    )
    assert source.unexpected_exit_ids() == ()


@pytest.mark.parametrize(
    "mutation",
    ("cross-kind", "accepted", "identity", "count", "digest"),
)
def test_adaptive_manager_clean_exit_rejects_final_cycle_commit_drain_drift(
    mutation: str, tmp_path: Path
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    _append_successful_manager_shutdown(directory, epochs=(1, 2))
    _append_final_cycle_commit_and_activation_ack_pairs(directory)
    events = _load_events(directory)
    drain = [
        event
        for event in events
        if event["event_type"]
        in {"adaptive_v2_commit_observed", "adaptive_v2_activation_observed"}
    ]
    if mutation == "cross-kind":
        drain[1]["event_type"] = "adaptive_v2_activation_observed"
    elif mutation == "accepted":
        drain[0]["payload"]["disposition"] = "accepted"
    elif mutation == "identity":
        drain[0]["payload"]["identity"]["successor_epoch_digest"] = "f" * 64
    elif mutation == "count":
        drain[0]["payload"]["accepted_commit_count"] = 27
    else:
        drain[1]["payload"]["canonical_payload_digest"] = "f" * 64
    _write_events(directory, events)
    process = runtime_fixture._FakeProcess(20_001)
    process.returncode = 0
    source = runtime_fixture._runtime().FocusedRawEvidenceSource(
        run_directory=directory,
        poll_interval_s=0,
        timeout_s=1,
        process_records=(
            runtime_fixture.SimpleNamespace(
                name="adaptive-manager", replica_id=-1, process=process
            ),
        ),
    )
    assert source.unexpected_exit_ids() == (-1,)


def test_manager_clean_exit_allows_only_benign_activation_ack_drain(
    tmp_path: Path,
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "control"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    _append_successful_manager_shutdown(directory)
    acknowledgements = _append_benign_post_terminal_activation_acknowledgements(
        directory
    )
    assert len(acknowledgements) == 18
    process = runtime_fixture._FakeProcess(20_001)
    process.returncode = 0
    source = runtime_fixture._runtime().FocusedRawEvidenceSource(
        run_directory=directory,
        poll_interval_s=0,
        timeout_s=1,
        process_records=(
            runtime_fixture.SimpleNamespace(
                name="adaptive-manager", replica_id=-1, process=process
            ),
        ),
    )
    assert source.unexpected_exit_ids() == ()


def test_manager_clean_exit_allows_repeated_partial_and_failed_ack_drain(
    tmp_path: Path,
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "control"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    _append_successful_manager_shutdown(directory)
    _append_benign_post_terminal_activation_acknowledgements(directory)
    events = _load_events(directory)
    suffix = [
        event
        for event in events
        if event["event_type"] == "adaptive_v2_activation_observed"
    ]
    for event in suffix:
        event["payload"]["accepted_commit_count"] = 0
    for event in suffix[2:4]:
        event["payload"]["replica_id"] = suffix[0]["payload"]["replica_id"]
    suffix[1]["payload"]["disposition"] = "ack_send_failed"
    _write_events(directory, events)
    process = runtime_fixture._FakeProcess(20_001)
    process.returncode = 0
    source = runtime_fixture._runtime().FocusedRawEvidenceSource(
        run_directory=directory,
        poll_interval_s=0,
        timeout_s=1,
        process_records=(
            runtime_fixture.SimpleNamespace(
                name="adaptive-manager", replica_id=-1, process=process
            ),
        ),
    )
    assert source.unexpected_exit_ids() == ()


def test_adaptive_manager_clean_exit_allows_final_cycle_activation_ack_drain(
    tmp_path: Path,
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    _append_successful_manager_shutdown(directory, epochs=(1, 2))
    _append_benign_post_terminal_activation_acknowledgements(directory)
    process = runtime_fixture._FakeProcess(20_001)
    process.returncode = 0
    source = runtime_fixture._runtime().FocusedRawEvidenceSource(
        run_directory=directory,
        poll_interval_s=0,
        timeout_s=1,
        process_records=(
            runtime_fixture.SimpleNamespace(
                name="adaptive-manager", replica_id=-1, process=process
            ),
        ),
    )
    assert source.unexpected_exit_ids() == ()


@pytest.mark.parametrize(
    "mutation",
    (
        "malformed",
        "new-identity",
        "accepted",
        "target-replica",
        "count-overflow",
        "pair-mismatch",
        "converged",
        "snapshot",
        "evidence",
        "terminal",
    ),
)
def test_manager_clean_exit_rejects_substantive_post_terminal_events(
    mutation: str, tmp_path: Path
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "control"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    _append_successful_manager_shutdown(directory)
    acknowledgements = _append_benign_post_terminal_activation_acknowledgements(
        directory
    )
    events = _load_events(directory)
    suffix = [
        event
        for event in events
        if event["event_type"] == "adaptive_v2_activation_observed"
    ]
    if mutation == "malformed":
        del suffix[0]["payload"]["identity"]
    elif mutation == "new-identity":
        suffix[0]["payload"]["identity"]["successor_epoch_digest"] = "f" * 64
    elif mutation == "accepted":
        suffix[0]["payload"]["disposition"] = "accepted"
    elif mutation == "target-replica":
        for event in suffix[:2]:
            event["payload"]["replica_id"] = 22
    elif mutation == "count-overflow":
        for event in suffix[:2]:
            event["payload"]["accepted_commit_count"] = 29
    elif mutation == "pair-mismatch":
        suffix[1]["payload"]["canonical_payload_digest"] = "f" * 64
    else:
        terminal = next(
            event
            for event in events
            if event["event_type"] == "adaptive_v2_session_terminal"
        )
        injected = deepcopy(terminal)
        injected["source_sequence"] = (
            max(
                int(event["source_sequence"])
                for event in events
                if event["source_kind"] == "adaptation_manager"
            )
            + 1
        )
        injected["source_monotonic_ns"] = (
            max(
                int(event["source_monotonic_ns"])
                for event in events
                if event["source_kind"] == "adaptation_manager"
            )
            + 1
        )
        injected["event_type"] = {
            "converged": "adaptive_v2_converged",
            "snapshot": "adaptive_v2_evidence_snapshot",
            "evidence": "evidence.observation_accepted",
            "terminal": "adaptive_v2_session_terminal",
        }[mutation]
        events.append(injected)
    _write_events(directory, events)
    process = runtime_fixture._FakeProcess(20_001)
    process.returncode = 0
    source = runtime_fixture._runtime().FocusedRawEvidenceSource(
        run_directory=directory,
        poll_interval_s=0,
        timeout_s=1,
        process_records=(
            runtime_fixture.SimpleNamespace(
                name="adaptive-manager", replica_id=-1, process=process
            ),
        ),
    )
    assert source.unexpected_exit_ids() == (-1,)


def test_raw_poll_reuses_its_single_event_read_for_exit_health(tmp_path: Path) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "control"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    source = _raw_source(directory)
    reads = 0
    original_events = source._events

    def counted_events() -> list[dict[str, object]]:
        nonlocal reads
        reads += 1
        return original_events()

    source._events = counted_events
    source.poll("readiness")
    assert reads == 1


def test_adaptive_manager_clean_exit_requires_both_successful_cycles(
    tmp_path: Path,
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    process = runtime_fixture._FakeProcess(20_001)
    process.returncode = 0
    record = runtime_fixture.SimpleNamespace(
        name="adaptive-manager", replica_id=-1, process=process
    )
    _append_successful_manager_shutdown(directory, epochs=(1, 2))
    source = runtime_fixture._runtime().FocusedRawEvidenceSource(
        run_directory=directory,
        poll_interval_s=0,
        timeout_s=1,
        process_records=(record,),
    )
    assert source.unexpected_exit_ids() == ()
    process.returncode = 1
    assert source.unexpected_exit_ids() == (-1,)
    process.returncode = 0
    events = _load_events(directory)
    epoch2_terminal = [
        event
        for event in events
        if event["event_type"] == "adaptive_v2_session_terminal"
    ][1]
    epoch2_terminal["event_type"] = "manager.note"
    _write_events(directory, events)
    assert source.unexpected_exit_ids() == (-1,)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("malformed-terminal", (-1,)),
        ("missing-tail", (-1,)),
        ("terminal-cycle-bool", (-1,)),
        ("winning-predecessor-bool", (-1,)),
        ("terminal-cutoff-drift", (-1,)),
        ("client-exit", (-2,)),
        ("survivor-record-exit", (20,)),
    ),
)
def test_manager_clean_exit_preserves_fail_closed_exit_identity(
    mutation: str, expected: tuple[int, ...], tmp_path: Path
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "control"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    _append_successful_manager_shutdown(directory)
    manager_process = runtime_fixture._FakeProcess(20_001)
    manager_process.returncode = 0
    records: tuple[object, ...] = (
        runtime_fixture.SimpleNamespace(
            name="adaptive-manager", replica_id=-1, process=manager_process
        ),
    )
    events = _load_events(directory)
    if mutation == "malformed-terminal":
        terminal = next(
            event
            for event in events
            if event["event_type"] == "adaptive_v2_session_terminal"
        )
        terminal["payload"]["unexpected"] = True
    elif mutation == "terminal-cycle-bool":
        terminal = next(
            event
            for event in events
            if event["event_type"] == "adaptive_v2_session_terminal"
        )
        terminal["payload"]["cycle_ordinal"] = False
    elif mutation == "winning-predecessor-bool":
        terminal = next(
            event
            for event in events
            if event["event_type"] == "adaptive_v2_session_terminal"
        )
        terminal["payload"]["winning_activation"]["predecessor_epoch_number"] = False
    elif mutation == "terminal-cutoff-drift":
        terminal = next(
            event
            for event in events
            if event["event_type"] == "adaptive_v2_session_terminal"
        )
        terminal["payload"]["baseline_evidence_cutoff"] = 0
        terminal["payload"]["current_evidence_cutoff"] = 0
    elif mutation == "missing-tail":
        stopped = next(
            event for event in events if event["event_type"] == "process.stopped"
        )
        stopped["event_type"] = "process.cleaned"
    elif mutation == "client-exit":
        client_events = [event for event in events if event["source_kind"] == "client"]
        template = next(
            event for event in events if event["source_kind"] == "adaptation_manager"
        )
        events.append(
            {
                **template,
                "source_kind": "client",
                "source_id": "client-0",
                "source_instance": f"{template['run_id']}-client-0",
                "source_sequence": len(client_events) + 1,
                "source_monotonic_ns": (
                    max(
                        (int(event["source_monotonic_ns"]) for event in client_events),
                        default=0,
                    )
                    + 1
                ),
                "event_type": "process.exited",
                "payload": {"exit_status": 0},
            }
        )
    else:
        survivor_process = runtime_fixture._FakeProcess(20_020)
        survivor_process.returncode = 0
        records += (
            runtime_fixture.SimpleNamespace(
                name="replica-20", replica_id=20, process=survivor_process
            ),
        )
    _write_events(directory, events)
    source = runtime_fixture._runtime().FocusedRawEvidenceSource(
        run_directory=directory,
        poll_interval_s=0,
        timeout_s=1,
        process_records=records,
    )
    if mutation == "malformed-terminal":
        with pytest.raises(
            runtime_fixture._runtime().FocusedCrashPairRuntimeError,
            match="manager terminal schema drifted",
        ):
            source.unexpected_exit_ids()
    else:
        assert source.unexpected_exit_ids() == expected


def test_raw_terminal_failure_detail_is_required_only_for_v4_profiles(
    tmp_path: Path,
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "control"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    _append_successful_manager_shutdown(directory)
    events = _load_events(directory)
    terminal = next(
        event
        for event in events
        if event["event_type"] == "adaptive_v2_session_terminal"
    )
    terminal["payload"]["reason"] = "controller_unhealthy"
    _write_events(directory, events)
    legacy = _raw_source(directory)
    assert legacy._events()
    v4 = _raw_source(directory)
    v4._profile = replace(v4._profile, profile_id="n7-f2-q5-two-crash-pair-smoke-v4")
    with pytest.raises(
        runtime_fixture._runtime().FocusedCrashPairRuntimeError,
        match="manager terminal schema drifted",
    ):
        v4._events()


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
        timestamp = (
            max(row["confirmed_monotonic_ns"] for row in receipt["sigkill_outcomes"])
            + 1
        )
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
                {(event["source_kind"], event["source_id"]) for event in events}
            )
        },
    )


def _fcrash_h_record(event: Mapping[str, object]) -> object:
    """Project one production-shaped manager acceptance into native replay input."""

    payload = event["payload"]
    assert isinstance(payload, dict)
    observation = payload["observation"]
    assert isinstance(observation, dict)
    configuration = observation["configuration"]
    assert isinstance(configuration, dict)
    return native_fixture.factorial_validation._EvidenceRecord(
        ingestion_sequence=int(payload["ingestion_sequence"]),
        acceptance_monotonic_ns=int(event["source_monotonic_ns"]),
        observation_id=str(observation["observation_id"]),
        reporter_id=int(observation["reporter_id"]),
        target_id=int(observation["observed_replica_id"]),
        epoch_number=int(configuration["epoch_number"]),
        tree_id=int(configuration["tree_id"]),
        epoch_digest=str(configuration["epoch_digest"]),
        block_hash=str(observation["block_hash"]),
        message_type=str(observation["expected_message_type"]),
        outcome=str(observation["outcome"]),
        response_duration_us=int(observation["response_duration_us"]),
        deadline_duration_us=int(observation["deadline_duration_us"]),
        reporter_monotonic_ns=int(observation["reporter_monotonic_ns"]),
        reporter_sequence=int(observation["reporter_sequence"]),
        signer_set=tuple(observation["signer_set"]),
        acceptance_source_sequence=int(event["source_sequence"]),
        schema_version=int(observation["schema_version"]),
    )


def _fcrash_h_v2_child(child: dict[str, object]) -> Path:
    """Seal an N31-v2 arm whose raw manager stream proves FCRASH-H itself.

    This intentionally does not borrow a precomputed snapshot: all IDs below
    are replayed from the manager observations written into the sealed child.
    """

    _complete_child(child, profile_path=runtime_fixture.N31_PROFILE_V2)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    validation = _validation()
    contract = _document(validation.validation_contract_from_profile(directory))
    coverage = contract["reporter_coverage_plan"]
    assert isinstance(coverage, dict)
    assert tuple(coverage["targets"][0]["authenticated_reporter_ids"]) == (
        0,
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        20,
        21,
        30,
    )
    receipt_path = directory / "raw" / "fault-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    fault_ns = max(
        item["confirmed_monotonic_ns"] for item in receipt["sigkill_outcomes"]
    )
    prefault_ns = (
        min(item["requested_monotonic_ns"] for item in receipt["sigkill_outcomes"]) - 1
    )
    active_tree_id = int(coverage["active_tree_id"])
    epoch0_digest = str(contract["epoch_zero_digest"])

    # Two on-time samples per survivor make the native responsiveness replay
    # non-vacuous.  Each selected target gets exactly two timeout samples from
    # every topology-derived common reporter.
    attempts: list[tuple[int, int, str, int]] = []
    timeout_tree_ids = {
        (int(target["target_replica_id"]), int(first["reporter_id"])): int(
            first["tree_id"]
        )
        for target in coverage["targets"]
        for first in target["first_qualifying_reporters"]
    }
    for survivor in contract["survivors"]:
        for attempt in range(2):
            attempts.append((30, int(survivor), "on_time", attempt))
    for target in coverage["targets"]:
        assert isinstance(target, dict)
        for reporter in target["authenticated_reporter_ids"]:
            for attempt in range(2):
                attempts.append(
                    (
                        int(reporter),
                        int(target["target_replica_id"]),
                        "timeout",
                        attempt,
                    )
                )

    manager_events: list[dict[str, object]] = [
        {
            "event_schema_version": 1,
            "run_id": "",
            "source_kind": "adaptation_manager",
            "source_id": "adaptive-manager",
            "source_instance": "",
            "source_sequence": 1,
            "source_monotonic_ns": fault_ns + 1,
            "event_type": "process.started",
            "payload": {"exit_status": None},
        },
        {
            "event_schema_version": 1,
            "run_id": "",
            "source_kind": "adaptation_manager",
            "source_id": "adaptive-manager",
            "source_instance": "",
            "source_sequence": 2,
            "source_monotonic_ns": fault_ns + 2,
            "event_type": "process.ready",
            "payload": {"exit_status": None},
        },
    ]
    reporter_sequences: dict[int, int] = {}
    for ingestion, (reporter, target, outcome, attempt) in enumerate(attempts, start=1):
        reporter_sequences[reporter] = reporter_sequences.get(reporter, 0) + 1
        block_hash = hashlib.sha256(
            f"fcrash-h-e0-{reporter}-{target}-{attempt}".encode("ascii")
        ).hexdigest()
        reporter_ns = fault_ns + 10 + ingestion
        manager_events.append(
            {
                "event_schema_version": 1,
                "run_id": "",
                "source_kind": "adaptation_manager",
                "source_id": "adaptive-manager",
                "source_instance": "",
                "source_sequence": ingestion + 1,
                "source_monotonic_ns": reporter_ns + 1,
                "event_type": "evidence.observation_accepted",
                "payload": {
                    "ingestion_sequence": ingestion,
                    "observation": {
                        "schema_version": 1,
                        "observation_id": native_fixture._observation_id(
                            reporter_id=reporter,
                            observed_replica_id=target,
                            epoch_number=0,
                            block_hash=block_hash,
                            epoch_digest=epoch0_digest,
                            tree_id=timeout_tree_ids.get((target, reporter), 0),
                        ),
                        "reporter_id": reporter,
                        "observed_replica_id": target,
                        "configuration": {
                            "epoch_number": 0,
                            "tree_id": timeout_tree_ids.get((target, reporter), 0),
                            "epoch_digest": epoch0_digest,
                        },
                        "block_hash": block_hash,
                        "expected_message_type": "direct_vote",
                        "outcome": outcome,
                        "response_duration_us": 100 if outcome == "on_time" else 0,
                        "deadline_duration_us": 1_000,
                        "reporter_monotonic_ns": reporter_ns,
                        "reporter_sequence": reporter_sequences[reporter],
                        "signer_set": [target] if outcome == "on_time" else [],
                    },
                },
            }
        )
    events = _load_events(directory)
    run_id = str(events[0]["run_id"])
    instance = f"{run_id}-adaptive-manager"
    for sequence, event in enumerate(manager_events, start=1):
        event["run_id"] = run_id
        event["source_instance"] = instance
        event["source_sequence"] = sequence
    records = [
        _fcrash_h_record(event)
        for event in manager_events
        if event["event_type"] == "evidence.observation_accepted"
    ]
    cutoff = len(records)
    full_snapshot_id = native_fixture.factorial_validation._snapshot_id(
        records,
        replica_count=native_fixture.N,
        epoch_number=0,
        epoch_digest=epoch0_digest,
        cutoff=cutoff,
        policy=native_fixture.NATIVE_RESPONSIVENESS_POLICY,
        seed=native_fixture.NATIVE_SNAPSHOT_SEED,
    )
    selected_snapshot_id = native_fixture.factorial_validation._snapshot_id(
        records,
        replica_count=native_fixture.N,
        epoch_number=0,
        epoch_digest=epoch0_digest,
        cutoff=cutoff,
        policy=native_fixture.NATIVE_RESPONSIVENESS_POLICY,
        seed=native_fixture.NATIVE_SNAPSHOT_SEED,
    )
    manager_events.append(
        {
            **manager_events[0],
            "source_sequence": len(manager_events) + 1,
            "source_monotonic_ns": max(
                int(event["source_monotonic_ns"]) for event in manager_events
            )
            + 1,
            "event_type": "adaptive_v2_evidence_snapshot",
            "payload": {
                "schema_version": 2,
                "cycle_ordinal": 0,
                "policy_intent": "fault_containment",
                "transition_artifact_id": "e0-to-e1-containment",
                "predecessor_epoch_number": 0,
                "predecessor_epoch_digest": epoch0_digest,
                "activation_generation": 1,
                "baseline_cutoff": 0,
                "current_cutoff": cutoff,
                "full_prefix_snapshot_id": full_snapshot_id,
                "evidence_snapshot_id": selected_snapshot_id,
                "accepted_prefix_count": cutoff,
                "eligible_ranking": list(range(native_fixture.Q)),
            },
        }
    )
    epoch1_wire, epoch1 = native_fixture._encode_native_epoch_bundle(
        1,
        epoch0_digest,
        native_fixture.NATIVE_SNAPSHOT_SEED,
        native_fixture.NATIVE_PLACEMENT_POLICY,
        native_fixture.E1_TREES,
        evidence_snapshot_id=selected_snapshot_id,
        evidence_cutoff=cutoff,
    )

    ranking_events = deepcopy(native_fixture._accepted_ranking_evidence())
    ranking_base_ns = 12_000_000_000
    for offset, event in enumerate(ranking_events, start=1):
        event["run_id"] = run_id
        event["source_instance"] = instance
        event["source_sequence"] = len(manager_events) + offset
        event["source_monotonic_ns"] = ranking_base_ns + offset
        payload = event["payload"]
        if event["event_type"] != "evidence.observation_accepted":
            continue
        observation = payload["observation"]
        configuration = observation["configuration"]
        configuration["epoch_digest"] = epoch1.epoch_digest
        observation["reporter_monotonic_ns"] = ranking_base_ns + offset - 1
        observation["observation_id"] = native_fixture._observation_id(
            reporter_id=int(observation["reporter_id"]),
            observed_replica_id=int(observation["observed_replica_id"]),
            epoch_number=1,
            block_hash=str(observation["block_hash"]),
            epoch_digest=epoch1.epoch_digest,
        )
    ranking_records = [
        _fcrash_h_record(event)
        for event in ranking_events
        if event["event_type"] == "evidence.observation_accepted"
    ]
    ranking_full = native_fixture.factorial_validation._snapshot_id(
        ranking_records,
        replica_count=native_fixture.N,
        epoch_number=1,
        epoch_digest=epoch1.epoch_digest,
        cutoff=native_fixture.EVIDENCE_CUTOFF,
        policy=native_fixture.NATIVE_RESPONSIVENESS_POLICY,
        seed=native_fixture.NATIVE_SNAPSHOT_SEED,
    )
    ranking_selected = native_fixture.factorial_validation._snapshot_id(
        native_fixture.factorial_validation._snapshot_records(
            ranking_records,
            baseline_cutoff=native_fixture.BASELINE_EVIDENCE_CUTOFF,
            current_cutoff=native_fixture.EVIDENCE_CUTOFF,
            suffix_only=True,
        ),
        replica_count=native_fixture.N,
        epoch_number=1,
        epoch_digest=epoch1.epoch_digest,
        cutoff=native_fixture.EVIDENCE_CUTOFF,
        policy=native_fixture.NATIVE_RESPONSIVENESS_POLICY,
        seed=native_fixture.NATIVE_SNAPSHOT_SEED,
    )
    audit = next(
        event
        for event in ranking_events
        if event["event_type"] == "adaptive_v2_evidence_snapshot"
    )
    audit["payload"]["predecessor_epoch_digest"] = epoch1.epoch_digest
    audit["payload"]["full_prefix_snapshot_id"] = ranking_full
    audit["payload"]["evidence_snapshot_id"] = ranking_selected
    epoch2_wire, epoch2 = native_fixture._encode_native_epoch_bundle(
        2,
        epoch1.epoch_digest,
        native_fixture.NATIVE_SNAPSHOT_SEED,
        native_fixture.NATIVE_PLACEMENT_POLICY,
        native_fixture.E2_TREES,
        evidence_snapshot_id=ranking_selected,
        evidence_cutoff=native_fixture.EVIDENCE_CUTOFF,
    )

    # The audit must remain after all its accepted observations but before the
    # containment/optimization events whose payloads bind the new digests.
    events = (
        [event for event in events if event["source_kind"] != "adaptation_manager"]
        + manager_events
        + ranking_events
    )
    for replica in range(native_fixture.N):
        source = f"replica-{replica}"
        source_events = [event for event in events if event["source_id"] == source]
        source_instance = (
            str(source_events[0]["source_instance"])
            if source_events
            else f"{run_id}-{source}"
        )
        for offset, event_type in enumerate(
            ("process.started", "process.ready"), start=2
        ):
            events.append(
                {
                    "event_schema_version": 1,
                    "run_id": run_id,
                    "source_kind": "replica",
                    "source_id": source,
                    "source_instance": source_instance,
                    "source_sequence": 0,
                    "source_monotonic_ns": prefault_ns - offset,
                    "event_type": event_type,
                    "payload": {"exit_status": None},
                }
            )
        events.append(
            {
                "event_schema_version": 1,
                "run_id": run_id,
                "source_kind": "replica",
                "source_id": source,
                "source_instance": source_instance,
                "source_sequence": (
                    1
                    if not source_events
                    else min(int(event["source_sequence"]) for event in source_events)
                    - 1
                ),
                "source_monotonic_ns": prefault_ns,
                "event_type": "adaptive.configuration_active",
                "payload": {
                    "epoch_number": 0,
                    "tree_id": active_tree_id,
                    "epoch_digest": epoch0_digest,
                },
            }
        )
    for event in events:
        payload = event["payload"]
        if event["event_type"] == "epoch.command_committed":
            successor = int(payload["successor_epoch_number"])
            event["payload"] = fixture._command_payload(
                epoch1 if successor == 1 else epoch2,
                int(payload["command_block_height"]),
            )
            event["source_monotonic_ns"] = (
                8_000_000_000 if successor == 1 else 14_000_000_000
            )
        elif event["event_type"] == "epoch.activated":
            epoch = int(payload["epoch_number"])
            payload["epoch_digest"] = (epoch1 if epoch == 1 else epoch2).epoch_digest
            event["source_monotonic_ns"] = (
                9_000_000_000 if epoch == 1 else 15_000_000_000
            )
        elif event["event_type"] == "block.committed":
            proof = payload.get("decision_proof")
            if isinstance(proof, dict) and proof.get("epoch_number") == 1:
                proof["epoch_digest"] = epoch1.epoch_digest
            elif isinstance(proof, dict) and proof.get("epoch_number") == 2:
                proof["epoch_digest"] = epoch2.epoch_digest
    (directory / "raw" / "epoch1.bundle").write_bytes(epoch1_wire)
    (directory / "raw" / "epoch2.bundle").write_bytes(epoch2_wire)
    sources: dict[tuple[str, str, str], list[dict[str, object]]] = {}
    for event in events:
        sources.setdefault(
            (
                str(event["source_kind"]),
                str(event["source_id"]),
                str(event["source_instance"]),
            ),
            [],
        ).append(event)
    for source_events in sources.values():
        source_events.sort(key=lambda event: int(event["source_monotonic_ns"]))
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
    _reseal(directory)
    return directory


def _add_same_predecessor_acceptance_relative_to_audit(
    directory: Path,
    *,
    placement: str,
) -> None:
    """Add one well-formed cutoff+1 acceptance without changing its audit."""

    assert placement in {"after", "before", "at"}
    events = _load_events(directory)
    audit = next(
        event
        for event in events
        if event["source_kind"] == "adaptation_manager"
        and event["event_type"] == "adaptive_v2_evidence_snapshot"
        and event["payload"]["predecessor_epoch_number"] == 0
    )
    accepted = next(
        event
        for event in events
        if event["source_kind"] == "adaptation_manager"
        and event["event_type"] == "evidence.observation_accepted"
        and event["payload"]["observation"]["configuration"]["epoch_number"] == 0
    )
    manager = [
        event for event in events if event["source_kind"] == "adaptation_manager"
    ]
    audit_sequence = int(audit["source_sequence"])
    audit_timestamp = int(audit["source_monotonic_ns"])
    extra = deepcopy(accepted)
    payload = extra["payload"]
    observation = payload["observation"]
    configuration = observation["configuration"]
    cutoff = int(audit["payload"]["current_cutoff"])
    reporter = int(observation["reporter_id"])
    observation["block_hash"] = hashlib.sha256(
        f"post-audit-{placement}-{cutoff}".encode("ascii")
    ).hexdigest()
    observation["reporter_sequence"] = 1 + max(
        int(candidate["payload"]["observation"]["reporter_sequence"])
        for candidate in manager
        if candidate["event_type"] == "evidence.observation_accepted"
        and int(candidate["payload"]["observation"]["reporter_id"]) == reporter
    )
    observation["reporter_monotonic_ns"] = 1 + max(
        int(candidate["payload"]["observation"]["reporter_monotonic_ns"])
        for candidate in manager
        if candidate["event_type"] == "evidence.observation_accepted"
        and int(candidate["payload"]["observation"]["reporter_id"]) == reporter
    )
    observation["observation_id"] = native_fixture._observation_id(
        reporter_id=reporter,
        observed_replica_id=int(observation["observed_replica_id"]),
        epoch_number=int(configuration["epoch_number"]),
        tree_id=int(configuration["tree_id"]),
        block_hash=str(observation["block_hash"]),
        epoch_digest=str(configuration["epoch_digest"]),
    )
    payload["ingestion_sequence"] = cutoff + 1
    if placement == "after":
        extra["source_sequence"] = 1 + max(
            int(event["source_sequence"]) for event in manager
        )
        extra["source_monotonic_ns"] = 1 + max(
            int(event["source_monotonic_ns"]) for event in manager
        )
    else:
        for event in manager:
            if int(event["source_sequence"]) >= audit_sequence:
                event["source_sequence"] = int(event["source_sequence"]) + 1
        extra["source_sequence"] = audit_sequence
        extra["source_monotonic_ns"] = (
            audit_timestamp - 1 if placement == "before" else audit_timestamp
        )
    events.append(extra)
    events.sort(
        key=lambda event: (
            str(event["source_kind"]),
            str(event["source_id"]),
            str(event["source_instance"]),
            int(event["source_sequence"]),
        )
    )
    _write_events(directory, events)
    _reseal(directory)


def test_sealed_n31_v2_fcrash_h_native_fixture_passes(
    tmp_path: Path,
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    directory = _fcrash_h_v2_child(child)
    assert (
        _validation().validate_sealed_arm(
            directory, trusted_provenance=_trusted_provenance(directory)
        )["verdict"]
        == "PASS"
    )


@pytest.mark.parametrize(
    ("field", "value"), (("commit_batch_index", 1), ("view_generation", 2))
)
def test_sealed_n31_v2_rejects_noncanonical_legacy_commit_scalars(
    field: str, value: int, tmp_path: Path
) -> None:
    validation = _validation()
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    directory = _fcrash_h_v2_child(child)
    events = _load_events(directory)
    next(event for event in events if event["event_type"] == "block.committed")[
        "payload"
    ][field] = value
    _write_events(directory, events)
    _reseal(directory)
    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation.validate_sealed_arm(
            directory, trusted_provenance=_trusted_provenance(directory)
        )


def test_native_audit_replay_ignores_later_same_predecessor_acceptance(
    tmp_path: Path,
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    directory = _fcrash_h_v2_child(child)
    _add_same_predecessor_acceptance_relative_to_audit(directory, placement="after")
    assert _raw_source(directory).poll("nonresponse") is not None
    assert (
        _validation().validate_sealed_arm(
            directory, trusted_provenance=_trusted_provenance(directory)
        )["verdict"]
        == "PASS"
    )


def test_native_audit_replay_rejects_corrupted_later_acceptance(
    tmp_path: Path,
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    directory = _fcrash_h_v2_child(child)
    _add_same_predecessor_acceptance_relative_to_audit(directory, placement="after")
    events = _load_events(directory)
    extra = max(
        (
            event
            for event in events
            if event["source_kind"] == "adaptation_manager"
            and event["event_type"] == "evidence.observation_accepted"
            and event["payload"]["observation"]["configuration"]["epoch_number"] == 0
        ),
        key=lambda event: int(event["source_sequence"]),
    )
    extra["payload"]["observation"]["observation_id"] = "0" * 64
    _write_events(directory, events)
    _reseal(directory)
    with pytest.raises(runtime_fixture._runtime().FocusedCrashPairRuntimeError):
        _raw_source(directory).poll("nonresponse")
    with pytest.raises(_validation().FocusedCrashPairValidationError):
        _validation().validate_sealed_arm(
            directory, trusted_provenance=_trusted_provenance(directory)
        )


@pytest.mark.parametrize("placement", ("before", "at"))
def test_native_audit_replay_rejects_cutoff_plus_one_acceptance_in_prefix(
    placement: str,
    tmp_path: Path,
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    directory = _fcrash_h_v2_child(child)
    _add_same_predecessor_acceptance_relative_to_audit(directory, placement=placement)
    with pytest.raises(runtime_fixture._runtime().FocusedCrashPairRuntimeError):
        _raw_source(directory).poll("nonresponse")
    with pytest.raises(_validation().FocusedCrashPairValidationError):
        _validation().validate_sealed_arm(
            directory, trusted_provenance=_trusted_provenance(directory)
        )


def test_raw_n31_v2_nonresponse_and_epoch1_share_the_native_audit(
    tmp_path: Path,
) -> None:
    """The audit follows its evidence and atomically produces Epoch 1."""

    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    directory = _fcrash_h_v2_child(child)
    source = _raw_source(directory)
    nonresponse = source.poll("nonresponse")
    epoch1 = source.poll("epoch1")
    assert nonresponse is not None
    assert epoch1 is not None
    assert int(nonresponse["source_monotonic_ns"]) < int(
        nonresponse["snapshot_audit_monotonic_ns"]
    )
    assert epoch1["source_monotonic_ns"] == nonresponse["snapshot_audit_monotonic_ns"]


def _renumber_raw_sources(events: list[dict[str, object]]) -> None:
    grouped: dict[tuple[str, str, str], list[dict[str, object]]] = {}
    for event in events:
        grouped.setdefault(
            (
                str(event["source_kind"]),
                str(event["source_id"]),
                str(event["source_instance"]),
            ),
            [],
        ).append(event)
    for source_events in grouped.values():
        source_events.sort(key=lambda event: int(event["source_sequence"]))
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


@pytest.mark.parametrize("mode", ("zero", "subset"))
def test_raw_epoch1_commands_remain_pending_until_full_survivor_witness_set(
    mode: str, tmp_path: Path
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    events = _load_events(directory)
    commands = [
        event
        for event in events
        if event["event_type"] == "epoch.command_committed"
        and event["payload"]["successor_epoch_number"] == 1
    ]
    assert commands
    removed = set(id(event) for event in (commands if mode == "zero" else commands[:1]))
    events = [event for event in events if id(event) not in removed]
    _renumber_raw_sources(events)
    _write_events(directory, events)
    assert _raw_source(directory).poll("commands1") is None


@pytest.mark.parametrize("mutation", ("duplicate", "wrong-payload", "wrong-source"))
def test_raw_epoch1_commands_reject_malformed_complete_witness_sets(
    mutation: str, tmp_path: Path
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    events = _load_events(directory)
    command = next(
        event
        for event in events
        if event["event_type"] == "epoch.command_committed"
        and event["payload"]["successor_epoch_number"] == 1
    )
    if mutation == "duplicate":
        duplicate = deepcopy(command)
        source_events = [
            event for event in events if event["source_id"] == command["source_id"]
        ]
        duplicate["source_sequence"] = (
            max(int(event["source_sequence"]) for event in source_events) + 1
        )
        duplicate["source_monotonic_ns"] = (
            max(int(event["source_monotonic_ns"]) for event in source_events) + 1
        )
        events.append(duplicate)
    elif mutation == "wrong-payload":
        command["payload"]["successor_epoch_digest"] = "f" * 64
    else:
        command["source_id"] = "replica-999"
    _renumber_raw_sources(events)
    _write_events(directory, events)
    with pytest.raises(runtime_fixture._runtime().FocusedCrashPairRuntimeError):
        _raw_source(directory).poll("commands1")


def test_raw_epoch1_activations_return_exact_full_native_snapshot(
    tmp_path: Path,
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    activation = _raw_source(directory).poll("activations1")
    assert activation is not None
    assert activation["successor_epoch_number"] == 1
    assert activation["witness_count"] == len(activation["survivor_replica_ids"])


@pytest.mark.parametrize("mode", ("zero", "subset"))
def test_raw_epoch1_activations_remain_pending_until_full_survivor_witness_set(
    mode: str, tmp_path: Path
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    events = _load_events(directory)
    activations = [
        event
        for event in events
        if event["event_type"] == "epoch.activated"
        and event["payload"]["epoch_number"] == 1
    ]
    assert activations
    removed = set(
        id(event) for event in (activations if mode == "zero" else activations[:1])
    )
    events = [event for event in events if id(event) not in removed]
    _renumber_raw_sources(events)
    _write_events(directory, events)
    assert _raw_source(directory).poll("activations1") is None


@pytest.mark.parametrize("mutation", ("duplicate", "wrong-payload", "wrong-source"))
def test_raw_epoch1_activations_reject_malformed_complete_witness_sets(
    mutation: str, tmp_path: Path
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    events = _load_events(directory)
    activation = next(
        event
        for event in events
        if event["event_type"] == "epoch.activated"
        and event["payload"]["epoch_number"] == 1
    )
    if mutation == "duplicate":
        duplicate = deepcopy(activation)
        source_events = [
            event for event in events if event["source_id"] == activation["source_id"]
        ]
        duplicate["source_sequence"] = (
            max(int(event["source_sequence"]) for event in source_events) + 1
        )
        duplicate["source_monotonic_ns"] = (
            max(int(event["source_monotonic_ns"]) for event in source_events) + 1
        )
        events.append(duplicate)
    elif mutation == "wrong-payload":
        activation["payload"]["successor_epoch_digest"] = "f" * 64
    else:
        activation["source_id"] = "replica-999"
    _renumber_raw_sources(events)
    _write_events(directory, events)
    with pytest.raises(runtime_fixture._runtime().FocusedCrashPairRuntimeError):
        _raw_source(directory).poll("activations1")


@pytest.mark.parametrize(("epoch", "phase"), ((1, "epoch1"), (2, "epoch2")))
def test_raw_n31_v2_bundle_must_match_its_replayed_snapshot(
    epoch: int,
    phase: str,
    tmp_path: Path,
) -> None:
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    directory = _fcrash_h_v2_child(child)
    bundle_path = directory / "raw" / f"epoch{epoch}.bundle"
    decoded = native_fixture.factorial_validation.decode_epoch_change_bundle(
        bundle_path.read_bytes(),
        issuer_public_key=native_fixture.ISSUER_PUBLIC_KEY,
    )
    wire, _changed = native_fixture._encode_native_epoch_bundle(
        epoch,
        decoded.previous_epoch_digest,
        decoded.generation_seed,
        decoded.policy_version,
        [asdict(tree) for tree in decoded.trees],
        evidence_snapshot_id="f" * 64,
        evidence_cutoff=decoded.evidence_cutoff,
    )
    bundle_path.write_bytes(wire)
    with pytest.raises(runtime_fixture._runtime().FocusedCrashPairRuntimeError):
        _raw_source(directory).poll(phase)


def test_sealed_n31_v2_epoch2_bundle_must_match_replayed_snapshot(
    tmp_path: Path,
) -> None:
    validation = _validation()
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    directory = _fcrash_h_v2_child(child)
    bundle_path = directory / "raw" / "epoch2.bundle"
    decoded = native_fixture.factorial_validation.decode_epoch_change_bundle(
        bundle_path.read_bytes(),
        issuer_public_key=native_fixture.ISSUER_PUBLIC_KEY,
    )
    wire, changed = native_fixture._encode_native_epoch_bundle(
        2,
        decoded.previous_epoch_digest,
        decoded.generation_seed,
        decoded.policy_version,
        [asdict(tree) for tree in decoded.trees],
        evidence_snapshot_id="f" * 64,
        evidence_cutoff=decoded.evidence_cutoff,
    )
    bundle_path.write_bytes(wire)
    events = _load_events(directory)
    for event in events:
        payload = event["payload"]
        if (
            event["event_type"] == "epoch.command_committed"
            and payload["successor_epoch_number"] == 2
        ):
            event["payload"] = fixture._command_payload(
                changed, int(payload["command_block_height"])
            )
        elif event["event_type"] == "epoch.activated" and payload["epoch_number"] == 2:
            payload["epoch_digest"] = changed.epoch_digest
        elif event["event_type"] == "block.committed":
            proof = payload.get("decision_proof")
            if isinstance(proof, dict) and proof.get("epoch_number") == 2:
                proof["epoch_digest"] = changed.epoch_digest
    _write_events(directory, events)
    _reseal(directory)
    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation.validate_sealed_arm(
            directory, trusted_provenance=_trusted_provenance(directory)
        )


def test_sealed_n31_v2_epoch2_command_must_follow_its_snapshot_audit(
    tmp_path: Path,
) -> None:
    validation = _validation()
    plan = fixture._plan(fixture._runner())
    child = next(
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    directory = _fcrash_h_v2_child(child)
    events = _load_events(directory)
    command_ns = min(
        int(event["source_monotonic_ns"])
        for event in events
        if event["event_type"] == "epoch.command_committed"
        and event["payload"]["successor_epoch_number"] == 2
    )
    audit = next(
        event
        for event in events
        if event["event_type"] == "adaptive_v2_evidence_snapshot"
        and event["payload"]["predecessor_epoch_number"] == 1
    )
    audit["source_monotonic_ns"] = command_ns + 1
    _write_events(directory, events)
    _reseal(directory)
    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation.validate_sealed_arm(
            directory, trusted_provenance=_trusted_provenance(directory)
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
            and event["payload"]["observation"]["configuration"]["epoch_number"] == 1
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
            if (
                event["event_type"] == "epoch.command_committed"
                and payload.get("successor_epoch_number") == 2
            ):
                event["payload"] = fixture._command_payload(epoch2, 11)
            elif (
                event["event_type"] == "epoch.activated"
                and payload.get("epoch_number") == 2
            ):
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
                "source_monotonic_ns": (
                    confirmation - 1 if accepted else confirmation + 1
                ),
                "source_sequence": 1,
            }
        )
    _write_events(directory, events)
    _write_source_inventory(directory, events)
    _reseal(directory)
    trusted = _trusted_provenance(directory)
    if accepted:
        assert (
            validation.validate_sealed_arm(
                directory,
                trusted_provenance=trusted,
            )["verdict"]
            == "PASS"
        )
    else:
        with pytest.raises(validation.FocusedCrashPairValidationError):
            validation.validate_sealed_arm(
                directory,
                trusted_provenance=trusted,
            )


def _v3_epoch_packed_commit_fixture(
    tmp_path: Path,
) -> tuple[Path, list[dict[str, object]], object, object, dict[str, object]]:
    """Small direct v3 replay with exact Epoch 1/2 packed generations."""

    root = tmp_path / "reconstruction"
    _write_json(root / "derived" / "phase-windows.json", {"phases": []})
    e0_digest, e1_digest, e2_digest = ("0" * 64, "1" * 64, "2" * 64)
    e1 = SimpleNamespace(
        epoch_digest=e1_digest,
        trees=(SimpleNamespace(tree_id=0), SimpleNamespace(tree_id=1)),
    )
    e2 = SimpleNamespace(
        epoch_digest=e2_digest,
        trees=(SimpleNamespace(tree_id=0), SimpleNamespace(tree_id=1)),
    )
    run_id = "packed-generation-run"
    instance = f"{run_id}-replica-0-uuid"
    events: list[dict[str, object]] = [
        {
            "source_kind": "replica",
            "source_id": "replica-0",
            "source_instance": instance,
            "source_sequence": sequence,
            "source_monotonic_ns": sequence,
            "event_type": event_type,
            "payload": {},
        }
        for sequence, event_type in ((1, "process.started"), (2, "process.ready"))
    ]
    replica1_instance = f"{run_id}-replica-1-uuid"
    events.extend(
        {
            "source_kind": "replica",
            "source_id": "replica-1",
            "source_instance": replica1_instance,
            "source_sequence": sequence,
            "source_monotonic_ns": sequence,
            "event_type": event_type,
            "payload": {},
        }
        for sequence, event_type in ((1, "process.started"), (2, "process.ready"))
    )
    configurations = (
        (0, 0, e0_digest),
        (1, 0, e1_digest),
        (1, 1, e1_digest),
        (2, 0, e2_digest),
        (2, 1, e2_digest),
    )
    for sequence, (epoch, tree, digest) in enumerate(configurations, start=3):
        events.append(
            {
                "source_kind": "replica",
                "source_id": "replica-0",
                "source_instance": instance,
                "source_sequence": sequence,
                "source_monotonic_ns": sequence,
                "event_type": "adaptive.configuration_active",
                "payload": {
                    "epoch_number": epoch,
                    "tree_id": tree,
                    "epoch_digest": digest,
                },
            }
        )
    commits = (
        (0, 0, 1),
        (1, 0, (1 << 32) + 1),
        (1, 1, (1 << 32) + 2),
        (2, 0, (2 << 32) + 1),
    )
    for height, (epoch, tree, generation) in enumerate(commits, start=1):
        block_hash = f"{height:064x}"
        payload = {
            "block_height": height,
            "block_hash": block_hash,
            "parent_hash": "0" * 64 if height == 1 else f"{height - 1:064x}",
            "transaction_count": 1000,
            "commit_batch_index": 0,
            "designated_observer": True,
            "view_generation": generation,
            "decision_proof": {
                "epoch_number": epoch,
                "epoch_digest": (e0_digest, e1_digest, e2_digest)[epoch],
                "block_hash": block_hash,
                "tree_id": tree,
            },
        }
        events.append(
            {
                "source_kind": "replica",
                "source_id": "replica-0",
                "source_instance": instance,
                "source_sequence": height + 7,
                "source_monotonic_ns": height + 7,
                "event_type": "block.committed",
                "payload": payload,
            }
        )
        events.append(
            {
                "source_kind": "replica",
                "source_id": "replica-0",
                "source_instance": instance,
                "source_sequence": height + 20,
                "source_monotonic_ns": height + 20,
                "event_type": "block.commit_observed",
                "payload": {
                    key: payload[key]
                    for key in (
                        "block_height",
                        "block_hash",
                        "parent_hash",
                        "transaction_count",
                        "commit_batch_index",
                    )
                },
            }
        )
    contract = {
        "authoritative_source_id": "replica-0",
        "profile": {"profile_id": "n7-f2-q5-two-crash-pair-smoke-v3"},
        "members": (0, 1),
        "survivors": (0,),
        "quorum": 1,
        "epoch_zero_digest": e0_digest,
        "transactions_per_block": 1000,
        "phase_names": ("baseline",),
        "bucket_width_seconds": 1,
    }
    return root, events, e1, e2, contract


def test_v3_commit_reconstruction_accepts_epoch_packed_rotations(
    tmp_path: Path,
) -> None:
    validation = _validation()
    root, events, epoch1, epoch2, contract = _v3_epoch_packed_commit_fixture(tmp_path)
    commits, _measurements = validation._commit_reconstruction(
        root, events, epoch1, epoch2, contract
    )
    assert len(commits) == 4


@pytest.mark.parametrize(
    "mutation",
    ("wrong-epoch", "future-rotation", "tree-generation-mismatch", "extra-proof-key"),
)
def test_v3_commit_reconstruction_rejects_epoch_packed_generation_drift(
    mutation: str, tmp_path: Path
) -> None:
    validation = _validation()
    root, events, epoch1, epoch2, contract = _v3_epoch_packed_commit_fixture(tmp_path)
    commits = [event for event in events if event["event_type"] == "block.committed"]
    epoch1_commit = commits[1]
    if mutation == "wrong-epoch":
        epoch1_commit["payload"]["view_generation"] = (2 << 32) + 1
    elif mutation == "future-rotation":
        epoch1_commit["payload"]["view_generation"] = (1 << 32) + 3
    elif mutation == "extra-proof-key":
        epoch1_commit["payload"]["decision_proof"]["extra"] = True
    else:
        commits[2]["payload"]["view_generation"] = (1 << 32) + 1
    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation._commit_reconstruction(root, events, epoch1, epoch2, contract)


def _v5_causal_phase_window_fixture(
    tmp_path: Path,
    *,
    arm: str = "adaptive",
) -> tuple[Path, list[dict[str, object]], object, object, dict[str, object]]:
    """Minimal v5 causal anchors with deliberately gapped 5-second windows."""

    root = tmp_path / "v5-causal-windows"
    root.mkdir()
    second = 1_000_000_000
    e0_digest, e1_digest, e2_digest = ("0" * 64, "1" * 64, "2" * 64)
    e1 = SimpleNamespace(epoch_digest=e1_digest, trees=(SimpleNamespace(tree_id=0),))
    e2 = (
        SimpleNamespace(epoch_digest=e2_digest, trees=(SimpleNamespace(tree_id=0),))
        if arm == "adaptive"
        else None
    )
    instance = "v5-run-replica-0"

    def commit(epoch: int, height: int, timestamp: int) -> dict[str, object]:
        digest = (e0_digest, e1_digest, e2_digest)[epoch]
        block_hash = f"{height:064x}"
        return {
            "source_kind": "replica",
            "source_id": "replica-0",
            "source_instance": instance,
            "source_sequence": height,
            "source_monotonic_ns": timestamp,
            "event_type": "block.committed",
            "payload": {
                "block_height": height,
                "block_hash": block_hash,
                "parent_hash": "0" * 64 if height == 1 else f"{height - 1:064x}",
                "transaction_count": 1000,
                "commit_batch_index": 0,
                "designated_observer": True,
                "view_generation": 1,
                "decision_proof": {
                    "epoch_number": epoch,
                    "tree_id": 0,
                    "epoch_digest": digest,
                    "block_hash": block_hash,
                },
            },
        }

    commits = [
        commit(0, 1, 5 * second),
        commit(0, 2, 36 * second),
        commit(0, 3, 49 * second),
        commit(1, 4, 60 * second),
        commit(1, 5, 91 * second),
        *(
            [commit(2, 6, 110 * second), commit(2, 7, 141 * second)]
            if arm == "adaptive"
            else [commit(1, 6, 126 * second)]
        ),
    ]
    events: list[dict[str, object]] = [*commits]
    for item in commits:
        payload = item["payload"]
        assert isinstance(payload, dict)
        events.append(
            {
                "source_kind": "replica",
                "source_id": "replica-0",
                "source_instance": instance,
                "source_sequence": int(item["source_sequence"]) + 10,
                "source_monotonic_ns": int(item["source_monotonic_ns"]),
                "event_type": "block.commit_observed",
                "payload": {
                    key: payload[key]
                    for key in (
                        "block_height",
                        "block_hash",
                        "parent_hash",
                        "transaction_count",
                        "commit_batch_index",
                    )
                },
            }
        )
    events.extend(
        {
            "source_kind": "replica",
            "source_id": "replica-0",
            "source_instance": instance,
            "source_sequence": 30 + epoch,
            "source_monotonic_ns": timestamp * second,
            "event_type": "epoch.activated",
            "payload": {"epoch_number": epoch, "epoch_digest": digest},
        }
        for epoch, digest, timestamp in (
            ((1, e1_digest, 55), (2, e2_digest, 105))
            if arm == "adaptive"
            else ((1, e1_digest, 55),)
        )
    )
    events.extend(
        {
            "source_kind": "replica",
            "source_id": "replica-0",
            "source_instance": instance,
            "source_sequence": 40 + epoch,
            "source_monotonic_ns": timestamp * second,
            "event_type": "epoch.command_committed",
            "payload": {"successor_epoch_number": epoch},
        }
        for epoch, timestamp in (
            ((1, 50), (2, 100)) if arm == "adaptive" else ((1, 50),)
        )
    )
    _write_json(
        root / "raw" / "fault-receipt.json",
        {
            "sigkill_outcomes": [
                {
                    "requested_monotonic_ns": 40 * second,
                    "confirmed_monotonic_ns": 40 * second,
                },
            ]
        },
    )
    phases = [
        {
            "phase": "baseline",
            "start_ns": 35 * second,
            "end_ns": 40 * second,
            "epoch_number": 0,
        },
        {
            "phase": "fault",
            "start_ns": 40 * second,
            "end_ns": 45 * second,
            "epoch_number": 0,
        },
        {
            "phase": "epoch1",
            "start_ns": 90 * second,
            "end_ns": 95 * second,
            "epoch_number": 1,
        },
        {
            "phase": "late",
            "start_ns": (140 if arm == "adaptive" else 125) * second,
            "end_ns": (145 if arm == "adaptive" else 130) * second,
            "epoch_number": 2 if arm == "adaptive" else 1,
        },
    ]
    _write_json(
        root / "derived" / "phase-windows.json",
        {
            "schema_version": 1,
            "domain": "kauri-focused-causal-phase-windows-v1",
            "phases": phases,
        },
    )
    contract = {
        "profile_id": "n31-f5-q21-three-crash-pair-v5",
        "authoritative_source_id": "replica-0",
        "profile": {
            "profile_id": "synthetic-causal-phase-fixture",
            "timers": {"stable_phase_seconds": 30},
            "measurement": {
                "bucket_width_seconds": 5,
                "phase_names": ("baseline", "fault", "epoch1", "late"),
                "phase_window_contract": {
                    "schema_version": 1,
                    "domain": "kauri-focused-causal-phase-windows-v1",
                    "stabilization_offset_seconds": 30,
                    "control_optimization_hold_seconds": 30,
                },
            },
        },
        "arm": arm,
        "members": (0,),
        "survivors": (0,),
        "quorum": 1,
        "epoch_zero_digest": e0_digest,
        "transactions_per_block": 1000,
        "phase_names": ("baseline", "fault", "epoch1", "late"),
        "bucket_width_seconds": 5,
        "phase_window_contract": {
            "schema_version": 1,
            "domain": "kauri-focused-causal-phase-windows-v1",
            "stabilization_offset_seconds": 30,
            "control_optimization_hold_seconds": 30,
        },
    }
    return root, events, e1, e2, contract


def test_v5_causal_phase_reconstruction_accepts_gapped_windows(
    tmp_path: Path,
) -> None:
    validation = _validation()
    root, events, epoch1, epoch2, contract = _v5_causal_phase_window_fixture(tmp_path)
    _commits, measurements = validation._commit_reconstruction(
        root, events, epoch1, epoch2, contract
    )
    assert [phase["phase"] for phase in measurements["phases"]] == [
        "baseline",
        "fault",
        "epoch1",
        "late",
    ]
    assert measurements["phases"][1]["transactions"] == 0


def test_v4_phase_fallback_is_not_figure_acceptable(tmp_path: Path) -> None:
    validation = _validation()
    root, events, epoch1, epoch2, contract = _v5_causal_phase_window_fixture(tmp_path)
    contract["profile_id"] = "n31-f5-q21-three-crash-pair-v4"
    contract.pop("phase_window_contract")
    with pytest.raises(
        validation.FocusedCrashPairValidationError,
        match="lacks the frozen causal phase-window contract",
    ):
        validation._commit_reconstruction(root, events, epoch1, epoch2, contract)


@pytest.mark.parametrize("profile_id", ("synthetic-v1", "synthetic-v2", "synthetic-v3"))
def test_archived_phase_fault_still_requires_positive_transactions(
    profile_id: str, tmp_path: Path
) -> None:
    validation = _validation()
    root, events, epoch1, epoch2, contract = _v5_causal_phase_window_fixture(tmp_path)
    contract["profile_id"] = profile_id
    contract.pop("phase_window_contract")
    phase_path = root / "derived" / "phase-windows.json"
    phase_document = json.loads(phase_path.read_text())
    for index, phase in enumerate(phase_document["phases"]):
        phase["start_ns"] = (35 + index * 5) * 1_000_000_000
        phase["end_ns"] = (40 + index * 5) * 1_000_000_000
    _write_json(phase_path, phase_document)
    with pytest.raises(
        validation.FocusedCrashPairValidationError,
        match="throughput phase has no authoritative committed transactions",
    ):
        validation._commit_reconstruction(root, events, epoch1, epoch2, contract)


@pytest.mark.parametrize(
    "mutation",
    (
        "empty",
        "run-start-fallback",
        "boundary-crossing",
        "wrong-proof-epoch",
        "missing-e2-late",
        "control-before-hold",
        "overlap",
        "zero-transactions",
        "wrong-epoch-fault",
        "command-at-fault-end",
    ),
)
def test_v5_causal_phase_reconstruction_rejects_unanchored_or_invalid_windows(
    mutation: str,
    tmp_path: Path,
) -> None:
    validation = _validation()
    arm = "control" if mutation == "control-before-hold" else "adaptive"
    root, events, epoch1, epoch2, contract = _v5_causal_phase_window_fixture(
        tmp_path, arm=arm
    )
    path = root / "derived" / "phase-windows.json"
    document = json.loads(path.read_text())
    phases = document["phases"]
    if mutation in {"empty", "run-start-fallback"}:
        document["phases"] = []
    elif mutation == "boundary-crossing":
        phases[0]["end_ns"] += 1_000_000_000
    elif mutation == "wrong-proof-epoch":
        phases[-1]["epoch_number"] = 1
    elif mutation == "missing-e2-late":
        events = [
            event
            for event in events
            if not (
                event["event_type"] == "block.committed"
                and event["payload"]["decision_proof"]["epoch_number"] == 2
            )
        ]
    elif mutation == "control-before-hold":
        phases[-1].update(
            {"start_ns": 59_000_000_000, "end_ns": 64_000_000_000, "epoch_number": 1}
        )
        epoch2 = None
    elif mutation == "overlap":
        phases[2]["start_ns"] = phases[1]["end_ns"] - 1
        phases[2]["end_ns"] = phases[2]["start_ns"] + 5_000_000_000
    elif mutation == "wrong-epoch-fault":
        wrong = deepcopy(
            next(
                event
                for event in events
                if event["event_type"] == "block.committed"
                and event["payload"]["decision_proof"]["epoch_number"] == 1
            )
        )
        wrong["source_monotonic_ns"] = 12_000_000_000
        events.append(wrong)
    elif mutation == "command-at-fault-end":
        for event in events:
            if (
                event["event_type"] == "epoch.command_committed"
                and event["payload"]["successor_epoch_number"] == 1
            ):
                event["source_monotonic_ns"] = 15_000_000_000
    else:
        phases[-1].update({"start_ns": 104_000_000_000, "end_ns": 109_000_000_000})
    _write_json(path, document)
    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation._commit_reconstruction(root, events, epoch1, epoch2, contract)


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
    assert (
        validation.validate_sealed_arm(
            directory,
            trusted_provenance=_trusted_provenance(directory),
        )["verdict"]
        == "PASS"
    )
    events = _load_events(directory)
    commits = [event for event in events if event["event_type"] == "block.committed"]
    last = max(commits, key=lambda event: event["payload"]["block_height"])
    extra = deepcopy(last)
    extra_payload = extra["payload"]
    extra_payload["block_height"] += 1
    extra_payload["parent_hash"] = last["payload"]["block_hash"]
    extra_payload["block_hash"] = "f" * 64
    extra_payload["decision_proof"]["block_hash"] = extra_payload["block_hash"]
    source_events = [
        event for event in events if event["source_id"] == extra["source_id"]
    ]
    extra["source_sequence"] = (
        max(event["source_sequence"] for event in source_events) + 1
    )
    extra["source_monotonic_ns"] = (
        max(event["source_monotonic_ns"] for event in source_events) + 1
    )
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
    assert (
        validation.validate_sealed_arm(
            directory,
            trusted_provenance=_trusted_provenance(directory),
        )["verdict"]
        == "PASS"
    )

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
        event for event in events if event["event_type"] == "block.commit_observed"
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
        item for item in fixture._children(plan, tmp_path) if item["arm"] == "adaptive"
    )
    _complete_child(child)
    directory = child["sealed_child_directory"]
    assert isinstance(directory, Path)
    assert (
        _document(
            validation.validate_sealed_arm(
                directory,
                trusted_provenance=_trusted_provenance(directory),
            )
        )["verdict"]
        == "PASS"
    )

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
    assert (
        len(
            {
                (entry["tree_sha256"], entry["seal_sha256"])
                for entry in pair_trusted["children"].values()
            }
        )
        == 2
    )
    pair = _document(
        validation.validate_sealed_pair(
            pair_root,
            trusted_provenance=pair_trusted,
        )
    )
    assert pair["verdict"] == "PASS"
    assert pair["scientific_outcome"] == "UNFAVORABLE"
    assert pair["retained"] is True
    for mutation in ("extra", "renamed", "swapped"):
        malformed = deepcopy(pair_trusted)
        entries = malformed["children"]
        assert isinstance(entries, dict)
        if mutation == "extra":
            entries["unrelated"] = deepcopy(entries["control"])
        elif mutation == "renamed":
            entries["renamed"] = entries.pop("control")
        else:
            entries["control"], entries["adaptive"] = (
                entries["adaptive"],
                entries["control"],
            )
        with pytest.raises(validation.FocusedCrashPairValidationError):
            validation.validate_sealed_pair(pair_root, trusted_provenance=malformed)

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
        (slot["slot_id"], slot["pair_id"], slot["arm"]) for slot in plan["slots"]
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
        fixture._archive().verify_evidence_seal(campaign_root) == campaign_parent_seal
    )
    campaign_trusted = _aggregate_trusted_provenance(
        campaign_root,
        copied_children,
    )
    assert (
        len(
            {
                (entry["tree_sha256"], entry["seal_sha256"])
                for entry in campaign_trusted["children"].values()
            }
        )
        == 10
    )
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
    assert any(
        pair["scientific_outcome"] == "UNFAVORABLE" for pair in campaign["pairs"]
    )
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
    assert (
        validation.validate_sealed_pair(
            pair_root,
            trusted_provenance=_aggregate_trusted_provenance(pair_root, children),
        )["verdict"]
        == "PASS"
    )

    adaptive = next(child for child in children if child["arm"] == "adaptive")
    adaptive_directory = adaptive["sealed_child_directory"]
    assert isinstance(adaptive_directory, Path)
    independent_public_key = _resign_child_with_independent_issuer(adaptive_directory)
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
    assert (
        validation.validate_sealed_arm(
            adaptive_directory,
            trusted_provenance=_trusted_provenance(adaptive_directory),
        )["verdict"]
        == "PASS"
    )
    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation.validate_sealed_pair(
            pair_root,
            trusted_provenance=_aggregate_trusted_provenance(pair_root, children),
        )
