"""Red-first Python contracts for prospective CERT13 adaptive-v3 execution.

The fixtures are intentionally self-contained: no v13 profile, proof, native
wire, or production helper exists yet.  Each red assertion therefore names the
missing v13 boundary instead of depending on an untracked fixture artifact.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Mapping

import pytest


RUNTIME_MODULE = (
    "experiments.adaptive.kauri_experiment.focused_crash_pair_runtime"
)
VALIDATION_MODULE = (
    "experiments.adaptive.kauri_experiment.focused_crash_pair_validation"
)
RUNNER_MODULE = "experiments.adaptive.run_focused_n31_crash_pair"
PROFILE_ROOT = Path(__file__).parents[1] / "profiles"

N7_V12 = PROFILE_ROOT / "n7-f2-q5-two-crash-pair-smoke-v12.json"
N31_V12 = PROFILE_ROOT / "n31-f5-q21-three-crash-pair-v12.json"
N7_V13 = PROFILE_ROOT / "n7-f2-q5-two-crash-pair-smoke-v13.json"
N31_V13 = PROFILE_ROOT / "n31-f5-q21-three-crash-pair-v13.json"

V13_PROFILE_IDS = frozenset(
    {
        "n7-f2-q5-two-crash-pair-smoke-v13",
        "n31-f5-q21-three-crash-pair-v13",
    }
)

_DIGESTS = {
    "membership": "11" * 32,
    "epoch0": "20" * 32,
    "epoch1": "21" * 32,
    "epoch2": "22" * 32,
    "command1": "31" * 32,
    "command2": "32" * 32,
    "command_block1": "41" * 32,
    "command_block2": "42" * 32,
    "boundary1": "51" * 32,
    "boundary2": "52" * 32,
    "certificate1": "61" * 32,
    "certificate2": "62" * 32,
}


def _runtime() -> Any:
    return importlib.import_module(RUNTIME_MODULE)


def _validation() -> Any:
    return importlib.import_module(VALIDATION_MODULE)


def _runner() -> Any:
    return importlib.import_module(RUNNER_MODULE)


def test_runtime_bundle_decode_routes_only_exact_v13_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    calls: list[tuple[str, bytes, str]] = []

    def decode_v2(wire: bytes, *, issuer_public_key: str) -> str:
        calls.append(("v2", wire, issuer_public_key))
        return "v2"

    def decode_v3(wire: bytes, *, issuer_public_key: str) -> str:
        calls.append(("v3", wire, issuer_public_key))
        return "v3"

    monkeypatch.setattr(
        runtime.factorial_validation, "decode_epoch_change_bundle", decode_v2
    )
    monkeypatch.setattr(
        runtime.factorial_validation,
        "decode_adaptive_v3_epoch_change_bundle",
        decode_v3,
    )

    assert runtime._decode_focused_epoch_change_bundle(
        b"same-wire",
        issuer_public_key="issuer",
        profile={"profile_id": "n7-f2-q5-two-crash-pair-smoke-v13"},
    ) == "v3"
    assert runtime._decode_focused_epoch_change_bundle(
        b"same-wire",
        issuer_public_key="issuer",
        profile={"profile_id": "n7-f2-q5-two-crash-pair-smoke-v12"},
    ) == "v2"
    assert calls == [
        ("v3", b"same-wire", "issuer"),
        ("v2", b"same-wire", "issuer"),
    ]


def _canonical_v13_contract(
    root: Path, profile_path: Path = N7_V13
) -> dict[str, Any]:
    validation = _validation()
    profile = json.loads(profile_path.read_bytes())
    proof_relative = Path(profile["topology"]["proof_path"])
    proof_source = PROFILE_ROOT / proof_relative
    root.mkdir(parents=True)
    (root / proof_relative.parent).mkdir(parents=True)
    shutil.copyfile(profile_path, root / "profile.json")
    shutil.copyfile(proof_source, root / proof_relative)
    return validation.validation_contract_from_profile(root)


@pytest.mark.parametrize(
    ("profile_path", "expected_reporters"),
    [(N7_V13, 5), (N31_V13, 28)],
)
def test_v13_canonical_profile_derives_independent_validation_contract(
    tmp_path: Path, profile_path: Path, expected_reporters: int
) -> None:
    profile = json.loads(profile_path.read_bytes())
    contract = _canonical_v13_contract(tmp_path / "contract", profile_path)

    assert contract["profile_id"] == profile["profile_id"]
    assert contract["protocol_mode"] == "adaptive_v3"
    assert contract["activation_readiness_contract"] == (
        profile["transitions"]["activation_readiness_contract"]
    )
    assert contract["quorum"] == profile["protocol"]["Q"]
    assert contract["survivor_barrier_count"] == expected_reporters


@pytest.mark.parametrize(
    ("profile_id", "expected"),
    (
        ("n7-f2-q5-two-crash-pair-smoke-v13", "8.0"),
        ("n31-f5-q21-three-crash-pair-v13", "8.0"),
        ("n31-f5-q21-three-crash-pair-v12", "8.0"),
    ),
)
def test_validator_preserves_the_frozen_progress_budget(
    profile_id: str,
    expected: str,
) -> None:
    validation = _validation()

    assert validation._expected_leader_progress_timeout(profile_id) == expected


@pytest.mark.parametrize(
    ("profile_id", "expected_streak"),
    (
        ("n7-f2-q5-two-crash-pair-smoke-v13", 2),
        ("n31-f5-q21-three-crash-pair-v13", 32),
        ("n31-f5-q21-three-crash-pair-v12", 2),
    ),
)
def test_validator_binds_the_n31_v13_timeout_streak_only(
    profile_id: str,
    expected_streak: int,
) -> None:
    validation = _validation()

    policy = validation._native_responsiveness_policy(profile_id)

    assert policy["attempt_window"] == 32
    assert policy["trailing_timeout_streak"] == expected_streak
    assert policy["minimum_response_rate_ppm"] == 750_000
    assert policy["maximum_timeout_rate_ppm"] == 250_000


def _n7_v13_fault_receipt(validation: Any) -> dict[str, Any]:
    targets = (0, 1)
    plan = {
        "schema_version": 1,
        "seed": 41_720,
        "scenario": {
            "replica_ids": list(range(7)),
            "quorum": 5,
            "crash_budget": 2,
            "successor_bundle_retry_limit": 1,
        },
        "actions": [
            {
                "fault_id": f"crash-replica-{replica}",
                "kind": "replica_group_sigkill",
                "replica_id": replica,
            }
            for replica in targets
        ],
    }
    records = [
        {
            "name": f"replica-{replica}",
            "replica_id": replica,
            "pid": 20_000 + replica,
            "pgid": 20_000 + replica,
        }
        for replica in targets
    ]
    outcomes = [
        {
            **record,
            "fault_id": f"crash-replica-{record['replica_id']}",
            "signal_number": 9,
            "returncode": -9,
            "requested_monotonic_ns": 40_000_000_000 + ordinal,
            "confirmed_monotonic_ns": 40_001_000_000,
        }
        for ordinal, record in enumerate(records)
    ]
    plan_sha256 = hashlib.sha256(
        validation._canonical(plan).rstrip(b"\n")
    ).hexdigest()
    journal = [
        {
            "schema_version": 1,
            "source_id": "fault-orchestrator",
            "source_sequence": ordinal,
            "source_monotonic_ns": 40_000_000_000 + ordinal,
            "plan_sha256": plan_sha256,
            "fault_id": f"crash-replica-{replica}",
            "lifecycle": "started",
        }
        for ordinal, replica in enumerate(targets)
    ]
    journal.extend(
        {
            "schema_version": 1,
            "source_id": "fault-orchestrator",
            "source_sequence": len(targets) + ordinal,
            "source_monotonic_ns": 40_001_000_000 + ordinal,
            "plan_sha256": plan_sha256,
            "fault_id": outcome["fault_id"],
            "lifecycle": "terminal",
            "outcome": {**outcome, "status": "succeeded"},
        }
        for ordinal, outcome in enumerate(outcomes)
    )
    return {
        "schema_version": 1,
        "fault_plan": plan,
        "process_records": records,
        "sigkill_outcomes": outcomes,
        "fault_journal": journal,
    }


def _issuer_public_key(validation: Any, private_key_hex: str) -> str:
    point = validation.factorial_validation._secp256k1_multiply(
        int(private_key_hex, 16),
        (
            validation.factorial_validation._SECP256K1_GX,
            validation.factorial_validation._SECP256K1_GY,
        ),
    )
    assert point is not None
    return ("02" if point[1] % 2 == 0 else "03") + f"{point[0]:064x}"


def _materialize_sealed_v13_arm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    arm: str,
) -> tuple[Any, Path, dict[str, object], Path]:
    fixture_root_value = os.environ.get("KAURI_CERT13_NATIVE_FIXTURE_ROOT")
    verifier_value = os.environ.get("KAURI_CERT13_READINESS_VERIFIER")
    if fixture_root_value is None or verifier_value is None:
        pytest.skip("native CERT13 fixture or readiness verifier is not configured")
    fixture_root = Path(fixture_root_value)
    verifier = Path(verifier_value).resolve()
    validation = _validation()
    runtime = _runtime()
    profile = runtime.load_focused_profile(N7_V13)
    manifest_bytes = (
        fixture_root / "shared" / "arm_manifest.json"
    ).read_bytes()
    manifest = json.loads(manifest_bytes)
    assert runtime._canonical_json(manifest) == manifest_bytes

    output_root = (tmp_path / "results").resolve()
    projection = {
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "membership_digest": manifest["membership_digest"],
        "member_count": 7,
    }
    parent_request = {
        "schema_version": 2,
        "mode": "smoke",
        "pair_count": 1,
        "profile_sha256": profile.profile_sha256,
        "topology_proof_sha256": profile.topology_proof_sha256,
        "output_root": str(output_root),
        "automatic_retries": 0,
        "replacement_policy": "none",
        "authorization_nonce": hashlib.sha256(
            f"smoke:1:{output_root}".encode("utf-8")
        ).hexdigest(),
        "execution_context_sha256": hashlib.sha256(
            b"cert13-native-sealed-fixture-context"
        ).hexdigest(),
        "pair_readiness_manifests": {"pair-01": projection},
    }
    parent_request_bytes = runtime._canonical_json(parent_request)
    parent_receipt = {
        **parent_request,
        "request_sha256": hashlib.sha256(parent_request_bytes).hexdigest(),
        "approval_reference": "CERT13 native sealed fixture",
        "approved_utc": "2026-08-20T00:00:00+00:00",
    }

    monkeypatch.setattr(
        runtime,
        "build_focused_authorization_request",
        lambda _preflight: parent_request_bytes,
    )
    monkeypatch.setattr(
        runtime,
        "verify_focused_authorization_receipt",
        lambda request, _receipt: (
            parent_receipt
            if request == parent_request_bytes
            else pytest.fail("materializer changed the authorized request")
        ),
    )

    def generate_tls(
        _adapter: object,
        *,
        keygen_binary: Path,
        tls_keygen_binary: Path,
        config_directory: Path,
    ) -> list[dict[str, str]]:
        assert keygen_binary.name == "hotstuff-keygen"
        assert tls_keygen_binary.name == "hotstuff-tls-keygen"
        rows = [
            {
                "crt": f"fixture-tls-crt-{identity}",
                "sec": f"fixture-tls-sec-{identity}",
                "cid": f"fixture-tls-cid-{identity}",
            }
            for identity in range(8)
        ]
        (config_directory / "tls-identities.txt").write_text(
            "fixture TLS identities\n", encoding="ascii"
        )
        return rows

    monkeypatch.setattr(runtime, "_generate_v13_arm_tls_identities", generate_tls)

    external = tmp_path / "external-readiness" / "pair-01"
    external.mkdir(parents=True)
    private_allocations = []
    for member in manifest["members"]:
        replica = member["replica_id"]
        scalar = f"{replica + 1:064x}"
        path = external / f"replica-{replica}.sec"
        path.write_text(scalar + "\n", encoding="ascii")
        path.chmod(0o600)
        private_allocations.append(
            {
                "private_key_path": str(path.resolve()),
                "private_key_sha256": hashlib.sha256(
                    (scalar + "\n").encode("ascii")
                ).hexdigest(),
                "public_key_hex": member["public_key_hex"],
                "replica_id": replica,
                "private_key": scalar,
            }
        )
    allocation = {
        "manifest_path": str((external / "manifest.json").resolve()),
        "manifest_sha256": projection["manifest_sha256"],
        "membership_digest": projection["membership_digest"],
        "members": manifest["members"],
        "private_allocations": private_allocations,
        "manifest_bytes": manifest_bytes,
    }
    build_directory = tmp_path / "build"
    build_directory.mkdir()
    (
        build_directory / runtime.profiled_fault_runtime.BUILD_PROVENANCE_FILENAME
    ).write_bytes(runtime._canonical_json({"schema_version": 1, "revision": "a" * 40}))
    issuer_private = (
        "4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57"
    )
    issuer_public = _issuer_public_key(validation, issuer_private)
    context = {
        "profile": profile,
        "pair_seed": 41_720,
        "output_root": output_root,
        "build_directory": build_directory,
        "binaries": {
            "app": Path("/build/hotstuff-app"),
            "manager": Path("/build/adaptation-manager"),
            "client": Path("/build/hotstuff-client"),
            "keygen": Path("/build/hotstuff-keygen"),
            "tls_keygen": Path("/build/hotstuff-tls-keygen"),
        },
        "pair_issuer_allocations": {
            "pair-01": {
                "public_key": issuer_public,
                "control": {
                    "public_key": issuer_public,
                    "private_key": issuer_private,
                },
                "adaptive": {
                    "public_key": issuer_public,
                    "private_key": issuer_private,
                },
            }
        },
        "pair_readiness_allocations": {"pair-01": allocation},
        "preflight_receipt": {},
        "authorization_receipt": parent_receipt,
    }
    configuration = runtime.FocusedLaunchBackend().materialize_arm_configuration(
        context, pair_ordinal=1, arm=arm
    )
    root = Path(configuration["run_directory"])
    contract = validation.validation_contract_from_profile(root)
    fault_receipt = _n7_v13_fault_receipt(validation)
    fault_receipt_bytes = validation._canonical(fault_receipt)
    coverage = contract["reporter_coverage_plan"]
    prefault_tree = coverage["active_tree_id"]
    positions = coverage["required_postfault_tree_positions"]
    fault_window_arm = {
        "schema_version": 4,
        "kind": "kauri-focused-fault-window-arm-v4",
        "run_id": configuration["run_id"],
        "profile_id": contract["profile_id"],
        "profile_sha256": contract["profile_sha256"],
        "topology_proof_sha256": contract["topology_proof_sha256"],
        "request_sha256": hashlib.sha256(parent_request_bytes).hexdigest(),
        "epoch_number": 0,
        "epoch_digest": contract["epoch_zero_digest"],
        "fault_receipt_sha256": hashlib.sha256(fault_receipt_bytes).hexdigest(),
        "evidence_start_monotonic_ns": 40_001_000_000,
        "prefault_tree_id": prefault_tree,
        "required_tree_positions": positions,
        "required_tree_ids": [
            (prefault_tree + offset) % 7 for offset in range(positions)
        ],
        "clock_domain": "same_host_clock_monotonic_raw",
        "required_observation_schema": 3,
        "timeout_evidence_basis": "exact_timeout_attempt_id_v1",
        "snapshot_evidence_basis": "exact_post_fault_attempt_start_v1",
        "selection_cardinality_policy": "all_guarded_up_to_fault_bound_v1",
    }
    fault_window_arm_bytes = validation._canonical(fault_window_arm)

    exporter = Path(__file__).resolve().parents[3] / (
        "build-adaptive/test/test_cert13_v13_fixture_exporter"
    )
    native_root = tmp_path / "native"
    native_root.mkdir()
    environment = {
        **os.environ,
        "KAURI_CERT13_EXPORT_DIR": str(native_root),
        "KAURI_CERT13_FIXTURE_RUN_ID": str(configuration["run_id"]),
        "KAURI_CERT13_FIXTURE_PARENT_REQUEST_SHA256": hashlib.sha256(
            parent_request_bytes
        ).hexdigest(),
        "KAURI_CERT13_FIXTURE_FAULT_RECEIPT_SHA256": hashlib.sha256(
            fault_receipt_bytes
        ).hexdigest(),
        "KAURI_CERT13_FIXTURE_FAULT_WINDOW_ARM_SHA256": hashlib.sha256(
            fault_window_arm_bytes
        ).hexdigest(),
        **{
            "KAURI_CERT13_FIXTURE_SOURCE_INSTANCE_"
            + source_id.upper().replace("-", "_"): source_instance
            for source_id, source_instance in configuration[
                "source_instances"
            ].items()
        },
    }
    completed = subprocess.run(
        (str(exporter),),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    shutil.copytree(native_root / arm / "raw", root / "raw", dirs_exist_ok=True)
    shutil.copyfile(
        native_root / arm / "runtime" / "source-inventory.json",
        root / "runtime" / "source-inventory.json",
    )
    shutil.copyfile(
        native_root
        / arm
        / "runtime"
        / "activation-readiness-public-manifest.json",
        root / "runtime" / "activation-readiness-public-manifest.json",
    )
    shutil.copyfile(
        native_root / arm / "derived" / "phase-windows.json",
        root / "derived" / "phase-windows.json",
    )
    (root / "raw" / "fault-receipt.json").write_bytes(fault_receipt_bytes)
    Path(configuration["fault_window_arm_path"]).write_bytes(
        fault_window_arm_bytes
    )
    launch = json.loads(
        (root / "runtime" / "launch-arguments.json").read_bytes()
    )
    manager_argv = launch["manager_argv"]
    (root / "runtime" / "manager-observed-argv.json").write_bytes(
        validation._canonical({"argv": manager_argv})
    )
    (root / "runtime" / "manager-input.json").write_bytes(
        validation._canonical(
            {
                "input_source": "normalized_manager_launch_boundary_v1",
                "requested_argv": manager_argv,
                "observed_argv": manager_argv,
                "stdin": "closed",
            }
        )
    )
    (root / "cleanup.json").write_bytes(validation._canonical({"complete": True}))
    archive = importlib.import_module(
        "experiments.adaptive.kauri_experiment.profiled_fault_archive"
    )
    seal = archive.create_evidence_seal(root)
    build = json.loads(
        (root / "runtime" / "build-provenance.json").read_bytes()
    )
    trusted = {
        "schema_version": 1,
        "revision": build["revision"],
        "build_sha256": build["build_sha256"],
        "profile_sha256": contract["profile_sha256"],
        "topology_proof_sha256": contract["topology_proof_sha256"],
        "epoch_profile_digest_sha256": hashlib.sha256(
            verifier.read_bytes()
        ).hexdigest(),
        "evidence_tree_sha256": seal.tree_sha256,
        "evidence_seal_sha256": seal.seal_sha256,
    }
    return validation, root, trusted, verifier


@pytest.mark.parametrize(
    ("arm", "expected_commit_count", "expected_phase_transactions"),
    [
        ("control", 10, [1000, 0, 1000, 1000]),
        ("adaptive", 17, [1000, 0, 1000, 1000]),
    ],
)
def test_v13_native_sealed_arm_passes_exact_certified_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arm: str,
    expected_commit_count: int,
    expected_phase_transactions: list[int],
) -> None:
    validation, root, trusted, verifier = _materialize_sealed_v13_arm(
        tmp_path, monkeypatch, arm=arm
    )

    result = validation.validate_sealed_arm(
        root,
        trusted_provenance=trusted,
        readiness_verifier_path=verifier,
    )

    assert result["verdict"] == "PASS"
    assert result["arm"] == arm
    assert result["epoch2_present"] is (arm == "adaptive")
    assert result["certified_readiness_verified"] is True
    assert result["guarded_nonresponsive_replica_ids"] == [0, 1]
    assert result["authoritative_commit_count"] == expected_commit_count
    assert [
        row["transactions"]
        for row in result["scientific_measurements"]["phases"]
    ] == expected_phase_transactions


def _reseal_v13_arm(
    root: Path, trusted: Mapping[str, object]
) -> dict[str, object]:
    seal_path = root / "evidence-seal.json"
    seal_path.unlink()
    archive = importlib.import_module(
        "experiments.adaptive.kauri_experiment.profiled_fault_archive"
    )
    seal = archive.create_evidence_seal(root)
    return {
        **trusted,
        "evidence_tree_sha256": seal.tree_sha256,
        "evidence_seal_sha256": seal.seal_sha256,
    }


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_verifier",
        "wrong_verifier_provenance",
        "manifest_key",
        "terminal_reason",
        "e2_hard_equality",
        "fault_target",
        "replica_public_key",
        "replica_private_unredacted",
        "replica_source_instance",
        "client_mode",
        "launch_extra_field",
    ),
)
def test_v13_native_sealed_arm_mutations_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    validation, root, trusted, verifier = _materialize_sealed_v13_arm(
        tmp_path, monkeypatch, arm="adaptive"
    )
    verifier_path: Path | None = verifier

    if mutation == "missing_verifier":
        verifier_path = None
    elif mutation == "wrong_verifier_provenance":
        trusted["epoch_profile_digest_sha256"] = "00" * 32
    elif mutation == "manifest_key":
        path = root / "runtime" / "activation-readiness-public-manifest.json"
        manifest = json.loads(path.read_bytes())
        manifest["members"][0]["public_key_hex"] = "00" * 48
        path.write_bytes(validation._canonical(manifest))
        trusted = _reseal_v13_arm(root, trusted)
    elif mutation in {"terminal_reason", "e2_hard_equality"}:
        path = root / "raw" / "adaptive-manager-events.jsonl"
        events = [json.loads(line) for line in path.read_text().splitlines()]
        if mutation == "terminal_reason":
            terminal = next(
                event
                for event in reversed(events)
                if event["event_type"] == "adaptive_v3.readiness_terminal"
            )
            terminal["payload"]["terminal_reason"] = 2
        else:
            e2 = next(
                event
                for event in events
                if event["event_type"] == "adaptive_v3.e2_eligibility"
            )
            e2["payload"]["e2_actual_begin_raw_ns"] = (
                e2["payload"]["e2_hard_deadline_raw_ns"]
                - e2["payload"]["e2_reserve_raw_ns"]
            )
        path.write_bytes(b"".join(validation._canonical(event) for event in events))
        trusted = _reseal_v13_arm(root, trusted)
    elif mutation == "fault_target":
        path = root / "raw" / "fault-receipt.json"
        receipt = json.loads(path.read_bytes())
        receipt["sigkill_outcomes"][0]["replica_id"] = 2
        path.write_bytes(validation._canonical(receipt))
        trusted = _reseal_v13_arm(root, trusted)
    elif mutation in {
        "replica_public_key",
        "replica_private_unredacted",
        "replica_source_instance",
        "client_mode",
        "launch_extra_field",
    }:
        path = root / "runtime" / "launch-arguments.json"
        launch = json.loads(path.read_bytes())
        if mutation == "launch_extra_field":
            launch["unexpected"] = True
        elif mutation == "client_mode":
            position = launch["client_argv"].index("--epoch-protocol-mode")
            launch["client_argv"][position + 1] = "adaptive_v2"
        else:
            command = launch["replica_argv"][0]
            option = {
                "replica_public_key": "--activation-readiness-member",
                "replica_private_unredacted": "--privkey",
                "replica_source_instance": "--structured-event-source-instance",
            }[mutation]
            position = command.index(option)
            command[position + 1] = {
                "replica_public_key": "0," + "00" * 48,
                "replica_private_unredacted": "01" * 32,
                "replica_source_instance": command[position + 1] + "-drift",
            }[mutation]
        path.write_bytes(validation._canonical(launch))
        trusted = _reseal_v13_arm(root, trusted)
    else:  # pragma: no cover - parameter list is exhaustive
        raise AssertionError(mutation)

    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation.validate_sealed_arm(
            root,
            trusted_provenance=trusted,
            readiness_verifier_path=verifier_path,
        )


def test_v13_native_fragment_joins_fault_truth_only_after_reconstruction(
    tmp_path: Path,
) -> None:
    fixture_root = os.environ.get("KAURI_CERT13_NATIVE_FIXTURE_ROOT")
    if fixture_root is None:
        pytest.skip("native CERT13 exporter fixture root is not configured")
    validation = _validation()
    contract = _canonical_v13_contract(tmp_path / "contract")
    arm = tmp_path / "pair-01" / "adaptive"
    shutil.copytree(Path(fixture_root) / "adaptive", arm)
    receipt = _n7_v13_fault_receipt(validation)
    (arm / "raw" / "fault-receipt.json").write_bytes(
        validation._canonical(receipt)
    )

    state = validation._validate_v13_certified_transition_fragment(arm, contract)
    joined = validation._validate_v13_fault_join(arm, contract, state)

    assert joined["targets"] == (0, 1)
    assert joined["survivors"] == (2, 3, 4, 5, 6)
    assert joined["fault_anchor_raw_ns"] == 40_001_000_000
    assert joined["hard_deadline_raw_ns"] == 520_001_000_000


def test_v13_native_fragment_reconstructs_authoritative_measurements(
    tmp_path: Path,
) -> None:
    fixture_root = os.environ.get("KAURI_CERT13_NATIVE_FIXTURE_ROOT")
    if fixture_root is None:
        pytest.skip("native CERT13 exporter fixture root is not configured")
    validation = _validation()
    contract = _canonical_v13_contract(tmp_path / "contract")
    arm = tmp_path / "pair-01" / "adaptive"
    shutil.copytree(Path(fixture_root) / "adaptive", arm)
    (arm / "raw" / "fault-receipt.json").write_bytes(
        validation._canonical(_n7_v13_fault_receipt(validation))
    )

    state = validation._validate_v13_certified_transition_fragment(arm, contract)
    commits, measurements = validation._commit_reconstruction(
        arm,
        state["events"],
        state["bundles"][0],
        state["bundles"][1],
        contract,
    )

    assert len(commits) == 17
    assert [row["phase"] for row in measurements["phases"]] == [
        "baseline",
        "fault",
        "epoch1",
        "late",
    ]
    assert [row["transactions"] for row in measurements["phases"]] == [
        1000,
        0,
        1000,
        1000,
    ]


@pytest.mark.parametrize(
    "mutation",
    ["late_crashed_event", "short_readiness", "wrong_hard_cap", "late_e2_activation"],
)
def test_v13_fault_join_mutations_fail_closed(
    tmp_path: Path, mutation: str
) -> None:
    fixture_root = os.environ.get("KAURI_CERT13_NATIVE_FIXTURE_ROOT")
    if fixture_root is None:
        pytest.skip("native CERT13 exporter fixture root is not configured")
    validation = _validation()
    contract = _canonical_v13_contract(tmp_path / "contract")
    arm = tmp_path / "pair-01" / "adaptive"
    shutil.copytree(Path(fixture_root) / "adaptive", arm)
    (arm / "raw" / "fault-receipt.json").write_bytes(
        validation._canonical(_n7_v13_fault_receipt(validation))
    )
    state = validation._validate_v13_certified_transition_fragment(arm, contract)

    if mutation == "late_crashed_event":
        event = next(
            item for item in state["events"] if item["source_id"] == "replica-0"
        )
        event["source_monotonic_ns"] = 40_001_000_001
    elif mutation == "short_readiness":
        state["readiness_cycles"][0]["signers"] = (2, 3, 4, 5)
    elif mutation == "wrong_hard_cap":
        event = next(
            item
            for item in state["events"]
            if item["event_type"] == "adaptive_v3.e2_eligibility"
        )
        event["payload"]["e2_hard_deadline_raw_ns"] += 1
    elif mutation == "late_e2_activation":
        commands = state["transitions"][1]["commands"]
        anchor = max(item["source_monotonic_ns"] for item in commands)
        state["transitions"][1]["activations"][0]["source_monotonic_ns"] = (
            anchor + 90_000_000_000
        )
    else:  # pragma: no cover - parameter list is exhaustive
        raise AssertionError(mutation)

    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation._validate_v13_fault_join(arm, contract, state)


def test_v13_bundle_decoder_route_is_explicit_not_sniffed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    validation = _validation()
    wire = tmp_path / "bundle.bin"
    wire.write_bytes(b"canonical-v3-test-wire")

    class Decoded:
        epoch_number = 1
        trees = (object(), object(), object(), object(), object())

    calls: list[str] = []
    monkeypatch.setattr(
        validation.factorial_validation,
        "decode_adaptive_v3_epoch_change_bundle",
        lambda payload, *, issuer_public_key: calls.append("v3") or Decoded(),
    )
    monkeypatch.setattr(
        validation.factorial_validation,
        "decode_epoch_change_bundle",
        lambda payload, *, issuer_public_key: calls.append("v2") or Decoded(),
    )
    contract = {
        "profile": {"profile_id": "n7-f2-q5-two-crash-pair-smoke-v13"},
        "quorum": 5,
    }
    validation._decode_bundle(wire, "issuer", 1, contract)
    assert calls == ["v3"]
    calls.clear()
    contract["profile"] = {"profile_id": "n7-f2-q5-two-crash-pair-smoke-v12"}
    validation._decode_bundle(wire, "issuer", 1, contract)
    assert calls == ["v2"]


def test_v13_native_exported_readiness_rows_are_admitted_when_available() -> None:
    root = os.environ.get("KAURI_CERT13_NATIVE_FIXTURE_ROOT")
    if root is None:
        pytest.skip("native CERT13 exporter fixture root is not configured")
    validation = _validation()
    contract = {"members": tuple(range(7)), "survivor_barrier_count": 5}
    validation._validate_v13_sources(Path(root) / "control", contract)
    validation._validate_v13_sources(Path(root) / "adaptive", contract)
    names = {"epoch.activation_prepared", "epoch.activation_ready_signed",
             "adaptive_v3.readiness_observation_retry_exhausted",
             "adaptive_v3.readiness_observation_accepted", "adaptive_v3.readiness_certificate_assembled",
             "adaptive_v3.readiness_certificate_delivery", "adaptive_v3.readiness_certificate_accepted",
             "adaptive_v3.readiness_certificate_acknowledged", "adaptive_v3.e2_eligibility",
             "adaptive_v3.readiness_terminal"}
    for arm in ("control", "adaptive"):
        for stream in ("adaptive-manager-events.jsonl", "replica-events.jsonl"):
            for line in (Path(root) / arm / "raw" / stream).read_text().splitlines():
                event = json.loads(line)
                if event["event_type"] in names:
                    validation._validate_v13_readiness_event_payload(
                        event, contract
                    )
                    bad = deepcopy(event)
                    bad["payload"]["unexpected"] = True
                    with pytest.raises(validation.FocusedCrashPairValidationError):
                        validation._validate_v13_readiness_event_payload(
                            bad, contract
                        )
                    if event["event_type"] == "epoch.activation_ready_signed":
                        retry = deepcopy(event)
                        retry["event_type"] = (
                            "adaptive_v3.readiness_observation_retry_exhausted"
                        )
                        retry["payload"]["disposition"] = "retry_exhausted"
                        validation._validate_v13_readiness_event_payload(
                            retry, contract
                        )
                        retry["payload"]["disposition"] = "garbage"
                        with pytest.raises(
                            validation.FocusedCrashPairValidationError
                        ):
                            validation._validate_v13_readiness_event_payload(
                                retry, contract
                            )


def test_v13_native_exported_readiness_chain_reconstructs_when_available() -> None:
    root_value = os.environ.get("KAURI_CERT13_NATIVE_FIXTURE_ROOT")
    if root_value is None:
        pytest.skip("native CERT13 exporter fixture root is not configured")
    validation = _validation()
    root = Path(root_value)
    contract = {
        "members": tuple(range(7)),
        "survivor_barrier_count": 5,
    }
    for arm_name, cycle_count in (("control", 1), ("adaptive", 2)):
        arm = root / arm_name
        events, _inventory = validation._validate_v13_sources(arm, contract)
        bundle_digests = [
            hashlib.sha256((arm / "raw" / f"epoch{epoch}.bundle").read_bytes())
            .hexdigest()
            for epoch in range(1, cycle_count + 1)
        ]
        cycles = validation._reconstruct_v13_certified_readiness(
            events,
            contract,
            expected_cycle_count=cycle_count,
            bundle_digests=bundle_digests,
        )
        assert len(cycles) == cycle_count
        assert all(cycle["signers"] == (2, 3, 4, 5, 6) for cycle in cycles)
        assert all(
            cycle["terminal_event"]["payload"]["disposition"]
            == "session_terminal"
            for cycle in cycles
        )
        retry_only = deepcopy(events)
        delivery_replica = next(
            event["payload"]["replica_id"]
            for event in retry_only
            if event["event_type"]
            == "adaptive_v3.readiness_certificate_delivery"
        )
        for event in retry_only:
            if (
                event["event_type"]
                == "adaptive_v3.readiness_certificate_delivery"
                and event["payload"]["replica_id"] == delivery_replica
            ):
                event["payload"]["delivery_enqueued"] = False
                event["payload"]["disposition"] = "retry_scheduled"
        with pytest.raises(validation.FocusedCrashPairValidationError):
            validation._reconstruct_v13_certified_readiness(
                retry_only,
                contract,
                expected_cycle_count=cycle_count,
                bundle_digests=bundle_digests,
            )

        too_many_attempts = deepcopy(events)
        first_delivery = next(
            event
            for event in too_many_attempts
            if event["event_type"]
            == "adaptive_v3.readiness_certificate_delivery"
            and event["payload"]["replica_id"] == delivery_replica
        )
        first_sequence = first_delivery["source_sequence"]
        for event in too_many_attempts:
            if (
                event["source_kind"] == "adaptation_manager"
                and event["source_sequence"] > first_sequence
            ):
                event["source_sequence"] += 5
        for attempt in range(2, 7):
            retry = deepcopy(first_delivery)
            retry["source_sequence"] = first_sequence + attempt - 1
            retry["payload"]["delivery_attempt"] = attempt
            retry["payload"]["delivery_enqueued"] = False
            retry["payload"]["disposition"] = "retry_scheduled"
            too_many_attempts.append(retry)
        with pytest.raises(validation.FocusedCrashPairValidationError):
            validation._reconstruct_v13_certified_readiness(
                too_many_attempts,
                contract,
                expected_cycle_count=cycle_count,
                bundle_digests=bundle_digests,
            )


def test_v13_native_certified_transition_fragment_reconstructs_when_available() -> None:
    root_value = os.environ.get("KAURI_CERT13_NATIVE_FIXTURE_ROOT")
    if root_value is None:
        pytest.skip("native CERT13 exporter fixture root is not configured")
    validation = _validation()
    contract = {
        "profile": {
            "profile_id": "n7-f2-q5-two-crash-pair-smoke-v13"
        },
        "members": tuple(range(7)),
        "quorum": 5,
        "survivor_barrier_count": 5,
        "authoritative_source_id": "replica-2",
        "epoch_zero_digest": (
            "f550407e56cc54a8fd4e93d1997ebe658b75699f4f2a9e955f4cc829b52bec81"
        ),
    }
    root = Path(root_value)
    control = validation._validate_v13_certified_transition_fragment(
        root / "control", contract
    )
    adaptive = validation._validate_v13_certified_transition_fragment(
        root / "adaptive", contract
    )
    assert control["arm"] == "control"
    assert len(control["readiness_cycles"]) == 1
    assert len(control["transitions"]) == 1
    assert control["e2_common_commit"] is None
    assert adaptive["arm"] == "adaptive"
    assert len(adaptive["readiness_cycles"]) == 2
    assert len(adaptive["transitions"]) == 2
    assert adaptive["e2_common_commit"]["sources"] == (2, 3, 4, 5, 6)


def test_v13_native_selection_audits_replay_when_available(
    tmp_path: Path,
) -> None:
    root_value = os.environ.get("KAURI_CERT13_NATIVE_FIXTURE_ROOT")
    if root_value is None:
        pytest.skip("native CERT13 exporter fixture root is not configured")
    validation = _validation()
    contract = _canonical_v13_contract(tmp_path / "v13-selection-contract")
    state = validation._validate_v13_certified_transition_fragment(
        Path(root_value) / "adaptive", contract
    )
    containment = validation._ranking(
        state["events"], state["bundles"][0], contract,
        predecessor_epoch=0,
    )
    assert containment[2] == (0, 1)
    assert containment[3] == state["bundles"][0].evidence_snapshot_id
    optimization = validation._ranking(
        state["events"], state["bundles"][0], contract,
        predecessor_epoch=1,
        inherited_wait_exempt=containment[2],
    )
    assert optimization[2] == (0, 1)
    assert optimization[3] == state["bundles"][1].evidence_snapshot_id


def test_v13_native_selection_reconstructs_before_profile_fault_ids_when_available(
    tmp_path: Path,
) -> None:
    root_value = os.environ.get("KAURI_CERT13_NATIVE_FIXTURE_ROOT")
    if root_value is None:
        pytest.skip("native CERT13 exporter fixture root is not configured")
    validation = _validation()
    contract = _canonical_v13_contract(tmp_path / "v13-blind-selection-contract")
    state = validation._validate_v13_certified_transition_fragment(
        Path(root_value) / "adaptive", contract
    )
    blinded = {
        **contract,
        "targets": (5, 6),
        "survivors": (0, 1, 2, 3, 4),
    }
    containment = validation._ranking(
        state["events"],
        state["bundles"][0],
        blinded,
        predecessor_epoch=0,
        enforce_expected_nonresponses=False,
    )
    assert containment[2] == (0, 1)
    optimization = validation._ranking(
        state["events"],
        state["bundles"][0],
        blinded,
        predecessor_epoch=1,
        inherited_wait_exempt=containment[2],
        enforce_expected_nonresponses=False,
    )
    assert optimization[2] == containment[2]


def test_v13_native_fragment_certificates_verify_cryptographically_when_available() -> None:
    root_value = os.environ.get("KAURI_CERT13_NATIVE_FIXTURE_ROOT")
    verifier_value = os.environ.get("KAURI_CERT13_READINESS_VERIFIER")
    if root_value is None or verifier_value is None:
        pytest.skip("native CERT13 fixture or readiness verifier is not configured")
    validation = _validation()
    profile_sha256 = (
        "3aa61c80d1c777db1469658532c978afc6fb52291cc6859cb5a889810541bd03"
    )
    contract = {
        "profile": {
            "profile_id": "n7-f2-q5-two-crash-pair-smoke-v13",
            "protocol": {"N": 7},
        },
        "profile_id": "n7-f2-q5-two-crash-pair-smoke-v13",
        "profile_sha256": profile_sha256,
        "members": tuple(range(7)),
        "quorum": 5,
        "survivor_barrier_count": 5,
        "authoritative_source_id": "replica-2",
        "epoch_zero_digest": (
            "f550407e56cc54a8fd4e93d1997ebe658b75699f4f2a9e955f4cc829b52bec81"
        ),
    }
    arm = Path(root_value) / "adaptive"
    state = validation._validate_v13_certified_transition_fragment(
        arm, contract
    )
    manifest_path = (
        arm / "runtime" / "activation-readiness-public-manifest.json"
    )
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    selected_projection = {
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "membership_digest": manifest["membership_digest"],
        "member_count": len(manifest["members"]),
    }
    verifier_path = Path(verifier_value).resolve()
    results = validation._validate_v13_certified_readiness_crypto(
        arm,
        contract,
        selected_projection,
        state,
        trusted_provenance={
            "epoch_profile_digest_sha256": hashlib.sha256(
                verifier_path.read_bytes()
            ).hexdigest()
        },
        readiness_verifier_path=verifier_path,
    )
    assert len(results) == 2
    assert all(result["valid"] is True for result in results)
    assert all(result["observation_count"] == 5 for result in results)


def _native_v13_e2_common_state() -> (
    tuple[Any, list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]
):
    root_value = os.environ.get("KAURI_CERT13_NATIVE_FIXTURE_ROOT")
    if root_value is None:
        pytest.skip("native CERT13 exporter fixture root is not configured")
    validation = _validation()
    arm = Path(root_value) / "adaptive"
    contract = {
        "members": tuple(range(7)),
        "survivor_barrier_count": 5,
        "authoritative_source_id": "replica-2",
    }
    events, _inventory = validation._validate_v13_sources(arm, contract)
    bundle_digests = [
        hashlib.sha256((arm / "raw" / f"epoch{epoch}.bundle").read_bytes()).hexdigest()
        for epoch in (1, 2)
    ]
    cycles = validation._reconstruct_v13_certified_readiness(
        events,
        contract,
        expected_cycle_count=2,
        bundle_digests=bundle_digests,
    )
    return validation, events, contract, cycles


def test_v13_native_e2_common_commit_reconstructs_when_available() -> None:
    validation, events, contract, cycles = _native_v13_e2_common_state()
    common = validation._validate_v13_e2_common_commit(events, contract, cycles)
    assert common["sources"] == (2, 3, 4, 5, 6)
    assert common["authoritative_event"]["source_id"] == "replica-2"
    assert len(common["observations"]) == 5


def test_v13_native_transitions_bind_bundles_commands_and_certified_activation_when_available() -> None:
    validation, events, contract, cycles = _native_v13_e2_common_state()
    arm = Path(os.environ["KAURI_CERT13_NATIVE_FIXTURE_ROOT"]) / "adaptive"
    contract.update(
        {
            "profile": {
                "profile_id": "n7-f2-q5-two-crash-pair-smoke-v13"
            },
            "quorum": 5,
            "epoch_zero_digest": "f550407e56cc54a8fd4e93d1997ebe658b75699f4f2a9e955f4cc829b52bec81",
        }
    )
    issuer = (arm / "raw" / "issuer-public-key.txt").read_text().strip()
    predecessor = contract["epoch_zero_digest"]
    for epoch, cycle in enumerate(cycles, start=1):
        _wire, decoded = validation._decode_bundle(
            arm / "raw" / f"epoch{epoch}.bundle", issuer, epoch, contract
        )
        assert decoded.previous_epoch_digest == predecessor
        transition = validation._validate_v13_transition(
            events, decoded, cycle, contract
        )
        assert transition["sources"] == (2, 3, 4, 5, 6)
        assert len(transition["commands"]) == 5
        assert len(transition["activations"]) == 5
        predecessor = decoded.epoch_digest


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_command",
        "duplicate_command",
        "non_r_command",
        "wrong_command_hash",
        "command_after_prepared",
        "missing_authoritative_command",
        "duplicate_authoritative_command",
        "wrong_authoritative_command_source",
        "command_not_designated",
        "wrong_predecessor_tree",
        "wrong_predecessor_generation",
        "wrong_identity_successor",
        "prepared_identity_mismatch",
        "missing_activation",
        "non_r_activation",
        "wrong_apply_height",
        "wrong_certificate_digest",
        "extra_activation_field",
        "activation_before_acceptance",
        "activation_time_regression",
    ],
)
def test_v13_native_transition_mutations_fail_closed(mutation: str) -> None:
    validation, original_events, contract, original_cycles = (
        _native_v13_e2_common_state()
    )
    events = deepcopy(original_events)
    cycle = deepcopy(original_cycles[0])
    arm = Path(os.environ["KAURI_CERT13_NATIVE_FIXTURE_ROOT"]) / "adaptive"
    contract.update(
        {
            "profile": {
                "profile_id": "n7-f2-q5-two-crash-pair-smoke-v13"
            },
            "quorum": 5,
        }
    )
    issuer = (arm / "raw" / "issuer-public-key.txt").read_text().strip()
    _wire, decoded = validation._decode_bundle(
        arm / "raw" / "epoch1.bundle", issuer, 1, contract
    )
    command = next(
        event
        for event in events
        if event["event_type"] == "epoch.command_committed"
        and event["source_id"] == "replica-2"
        and event["payload"]["successor_epoch_number"] == 1
    )
    prepared = next(
        event
        for event in events
        if event["event_type"] == "epoch.activation_prepared"
        and event["source_id"] == "replica-2"
        and event["payload"]["identity"]["successor_configuration"][
            "epoch_number"
        ]
        == 1
    )
    accepted = next(
        event
        for event in events
        if event["event_type"]
        == "adaptive_v3.readiness_certificate_accepted"
        and event["source_id"] == "replica-2"
        and event["payload"]["identity"]["successor_configuration"][
            "epoch_number"
        ]
        == 1
    )
    activation = next(
        event
        for event in events
        if event["event_type"] == "epoch.activated"
        and event["source_id"] == "replica-2"
        and event["payload"]["epoch_number"] == 1
    )
    authoritative_command = next(
        event
        for event in events
        if event["event_type"] == "block.committed"
        and event["payload"]["block_height"]
        == cycle["identity"]["command_block_height"]
        and event["payload"]["block_hash"]
        == cycle["identity"]["command_block_hash"]
    )
    authoritative_boundary = next(
        event
        for event in events
        if event["event_type"] == "block.committed"
        and event["payload"]["block_height"]
        == cycle["identity"]["activation_height"]
        and event["payload"]["block_hash"]
        == cycle["identity"]["activation_boundary_block_hash"]
    )

    if mutation == "missing_command":
        events.remove(command)
    elif mutation == "duplicate_command":
        events.append(deepcopy(command))
    elif mutation == "non_r_command":
        command["source_id"] = "replica-1"
    elif mutation == "wrong_command_hash":
        command["payload"]["command_block_hash"] = "00" * 32
    elif mutation == "command_after_prepared":
        command["source_sequence"] = prepared["source_sequence"]
    elif mutation == "missing_authoritative_command":
        events.remove(authoritative_command)
    elif mutation == "duplicate_authoritative_command":
        events.append(deepcopy(authoritative_command))
    elif mutation == "wrong_authoritative_command_source":
        authoritative_command["source_id"] = "replica-3"
    elif mutation == "command_not_designated":
        authoritative_command["payload"]["designated_observer"] = False
    elif mutation == "wrong_predecessor_tree":
        authoritative_boundary["payload"]["decision_proof"]["tree_id"] += 1
    elif mutation == "wrong_predecessor_generation":
        authoritative_boundary["payload"]["view_generation"] += 1
    elif mutation == "wrong_identity_successor":
        cycle["identity"]["successor_configuration"]["epoch_number"] = 2
    elif mutation == "prepared_identity_mismatch":
        prepared["payload"]["identity"]["command_block_hash"] = "00" * 32
    elif mutation == "missing_activation":
        events.remove(activation)
    elif mutation == "non_r_activation":
        activation["source_id"] = "replica-1"
    elif mutation == "wrong_apply_height":
        activation["payload"]["certificate_apply_committed_height"] = (
            cycle["identity"]["activation_height"] - 1
        )
    elif mutation == "wrong_certificate_digest":
        activation["payload"]["activation_readiness_certificate_digest"] = (
            "00" * 32
        )
    elif mutation == "extra_activation_field":
        activation["payload"]["unexpected"] = True
    elif mutation == "activation_before_acceptance":
        activation["source_sequence"] = accepted["source_sequence"]
    elif mutation == "activation_time_regression":
        activation["source_monotonic_ns"] = command["source_monotonic_ns"] - 1
    else:  # pragma: no cover - parameter list is exhaustive
        raise AssertionError(mutation)

    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation._validate_v13_transition(events, decoded, cycle, contract)


def test_v13_transition_accepts_delayed_certificate_application_when_available(
) -> None:
    validation, original_events, contract, original_cycles = (
        _native_v13_e2_common_state()
    )
    events = deepcopy(original_events)
    cycle = deepcopy(original_cycles[0])
    arm = Path(os.environ["KAURI_CERT13_NATIVE_FIXTURE_ROOT"]) / "adaptive"
    contract.update(
        {
            "profile": {
                "profile_id": "n7-f2-q5-two-crash-pair-smoke-v13"
            },
            "quorum": 5,
        }
    )
    issuer = (arm / "raw" / "issuer-public-key.txt").read_text().strip()
    _wire, decoded = validation._decode_bundle(
        arm / "raw" / "epoch1.bundle", issuer, 1, contract
    )
    activation = next(
        event
        for event in events
        if event["event_type"] == "epoch.activated"
        and event["source_id"] == "replica-2"
        and event["payload"]["epoch_number"] == decoded.epoch_number
    )
    activation["payload"]["certificate_apply_committed_height"] += 1
    validation._validate_v13_transition(events, decoded, cycle, contract)
    bundle_digests = [
        hashlib.sha256(
            (arm / "raw" / f"epoch{epoch}.bundle").read_bytes()
        ).hexdigest()
        for epoch in (1, 2)
    ]
    reconstructed = validation._reconstruct_v13_certified_readiness(
        events,
        contract,
        expected_cycle_count=2,
        bundle_digests=bundle_digests,
    )
    assert len(reconstructed) == 2


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_authoritative",
        "wrong_authoritative_source",
        "not_designated",
        "missing_view_generation",
        "legacy_reporter_tick",
        "wrong_decision_configuration",
        "authoritative_after_common_tick",
        "missing_observation",
        "duplicate_observation",
        "wrong_observation_hash",
        "observation_after_common_tick",
        "short_audit_sources",
        "empty_audit_sources",
        "audit_before_atomic_begin",
        "wrong_final_ack_anchor",
        "wrong_e1_bundle_digest",
    ],
)
def test_v13_native_e2_common_commit_mutations_fail_closed(
    mutation: str,
) -> None:
    validation, original_events, contract, original_cycles = (
        _native_v13_e2_common_state()
    )
    events = deepcopy(original_events)
    cycles = deepcopy(original_cycles)
    e2 = next(
        event
        for event in events
        if event["event_type"] == "adaptive_v3.e2_eligibility"
    )
    common = e2["payload"]["e2_common_commit"]
    authoritative = next(
        event
        for event in events
        if event["event_type"] == "block.committed"
        and event["payload"]["decision_proof"] == common
    )
    observations = [
        event
        for event in events
        if event["event_type"] == "block.commit_observed"
        and event["payload"]["block_hash"] == common["block_hash"]
    ]
    common_tick = e2["payload"]["e2_common_commit_raw_ns"]

    if mutation == "duplicate_authoritative":
        events.append(deepcopy(authoritative))
    elif mutation == "wrong_authoritative_source":
        authoritative["source_id"] = "replica-3"
    elif mutation == "not_designated":
        authoritative["payload"]["designated_observer"] = False
    elif mutation == "missing_view_generation":
        authoritative["payload"]["view_generation"] = None
    elif mutation == "legacy_reporter_tick":
        authoritative["payload"]["reporter_local_commit_monotonic_ns"] = common_tick
    elif mutation == "wrong_decision_configuration":
        authoritative["payload"]["decision_proof"]["epoch_number"] += 1
    elif mutation == "authoritative_after_common_tick":
        authoritative["source_monotonic_ns"] = common_tick + 1
    elif mutation == "missing_observation":
        events.remove(observations[-1])
    elif mutation == "duplicate_observation":
        events.append(deepcopy(observations[-1]))
    elif mutation == "wrong_observation_hash":
        observations[-1]["payload"]["block_hash"] = "ff" * 32
    elif mutation == "observation_after_common_tick":
        observations[-1]["source_monotonic_ns"] = common_tick + 1
    elif mutation == "short_audit_sources":
        e2["payload"]["observed_signers"].pop()
        e2["payload"]["e2_common_commit_sources"].pop()
        e2["payload"]["required_release_count"] -= 1
    elif mutation == "empty_audit_sources":
        e2["payload"]["observed_signers"] = []
        e2["payload"]["e2_common_commit_sources"] = []
        e2["payload"]["required_release_count"] = 0
    elif mutation == "audit_before_atomic_begin":
        e2["source_monotonic_ns"] = e2["payload"]["e2_actual_begin_raw_ns"] - 1
    elif mutation == "wrong_final_ack_anchor":
        e2["payload"]["e2_final_ack_raw_ns"] = (
            cycles[0]["final_ack_manager_time_ns"] + 1
        )
    elif mutation == "wrong_e1_bundle_digest":
        e2["payload"]["e1_bundle_digest"] = "ff" * 32
    else:  # pragma: no cover - parameter list is exhaustive
        raise AssertionError(f"unknown mutation {mutation}")

    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation._validate_v13_e2_common_commit(events, contract, cycles)


def test_v13_native_e2_common_commit_uses_cross_source_identity_witnesses() -> None:
    validation, events, contract, cycles = _native_v13_e2_common_state()
    events = deepcopy(events)
    e2 = next(
        event
        for event in events
        if event["event_type"] == "adaptive_v3.e2_eligibility"
    )
    common = e2["payload"]["e2_common_commit"]
    authoritative = next(
        event
        for event in events
        if event["event_type"] == "block.committed"
        and event["payload"]["decision_proof"] == common
    )
    witnesses = [
        event
        for event in events
        if event["event_type"] == "block.commit_identity_witness"
        and event["payload"]["decision_proof"] == common
    ]
    assert witnesses
    events.remove(authoritative)

    validation._validate_v13_e2_common_commit(events, contract, cycles)


def test_v13_commit_reconstruction_accepts_replica_local_batch_skew() -> None:
    validation, original_events, contract, _cycles = _native_v13_e2_common_state()
    events = deepcopy(original_events)
    witness = next(
        event
        for event in events
        if event["event_type"] == "block.commit_identity_witness"
    )
    observation = next(
        event
        for event in events
        if event["event_type"] == "block.commit_observed"
        and event["source_id"] == witness["source_id"]
        and event["payload"]["block_hash"] == witness["payload"]["block_hash"]
        and event["payload"]["block_height"]
        == witness["payload"]["block_height"]
    )
    observation["payload"]["commit_batch_index"] += 1
    witness["payload"]["commit_batch_index"] += 1

    validation._v13_reconstruct_authoritative_commits(events, contract)


@pytest.mark.parametrize(
    ("orphan_height_delta", "parent_matches", "accepted"),
    [
        (1, True, True),
        (0, True, False),
        (2, True, False),
        (1, False, False),
    ],
)
def test_v13_commit_reconstruction_bounds_shutdown_suffix(
    orphan_height_delta: int,
    parent_matches: bool,
    accepted: bool,
) -> None:
    validation, original_events, contract, _cycles = _native_v13_e2_common_state()
    events = deepcopy(original_events)
    authoritative = [
        event
        for event in events
        if event["event_type"] == "block.commit_observed"
        and event["source_id"] == contract["authoritative_source_id"]
    ]
    tip = max(authoritative, key=lambda event: event["payload"]["block_height"])
    witness_template = next(
        event
        for event in events
        if event["event_type"] == "block.commit_identity_witness"
    )
    observation_template = next(
        event
        for event in events
        if event["event_type"] == "block.commit_observed"
        and event["source_id"] == witness_template["source_id"]
        and event["source_sequence"] + 1 == witness_template["source_sequence"]
    )
    observation = deepcopy(observation_template)
    witness = deepcopy(witness_template)
    source_events = [
        event for event in events if event["source_id"] == witness["source_id"]
    ]
    next_sequence = max(event["source_sequence"] for event in source_events) + 1
    next_time = max(event["source_monotonic_ns"] for event in source_events) + 1
    block_hash = "ef" * 32
    height = tip["payload"]["block_height"] + orphan_height_delta
    for event, sequence, timestamp in (
        (observation, next_sequence, next_time),
        (witness, next_sequence + 1, next_time + 1),
    ):
        event["source_sequence"] = sequence
        event["source_monotonic_ns"] = timestamp
        event["payload"]["block_height"] = height
        event["payload"]["block_hash"] = block_hash
        event["payload"]["parent_hash"] = (
            tip["payload"]["block_hash"] if parent_matches else "ab" * 32
        )
        event["payload"]["commit_batch_index"] = 0
    witness["payload"]["decision_proof"]["block_hash"] = block_hash
    events.extend((observation, witness))

    if accepted:
        validation._v13_reconstruct_authoritative_commits(events, contract)
    else:
        with pytest.raises(validation.FocusedCrashPairValidationError):
            validation._v13_reconstruct_authoritative_commits(events, contract)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_all_carriers",
        "conflicting_fallback_identity",
        "orphan_physical_block",
        "nonadjacent_source_sequence",
        "designated_source_witness",
        "carrier_after_common_tick",
    ],
)
def test_v13_native_cross_source_commit_witness_mutations_fail_closed(
    mutation: str,
) -> None:
    validation, original_events, contract, cycles = _native_v13_e2_common_state()
    events = deepcopy(original_events)
    e2 = next(
        event
        for event in events
        if event["event_type"] == "adaptive_v3.e2_eligibility"
    )
    common = e2["payload"]["e2_common_commit"]
    authoritative = next(
        event
        for event in events
        if event["event_type"] == "block.committed"
        and event["payload"]["decision_proof"] == common
    )
    witnesses = [
        event
        for event in events
        if event["event_type"] == "block.commit_identity_witness"
        and event["payload"]["decision_proof"] == common
    ]
    assert witnesses
    witness = witnesses[0]

    if mutation == "missing_all_carriers":
        events.remove(authoritative)
        for candidate in witnesses:
            events.remove(candidate)
    elif mutation == "conflicting_fallback_identity":
        events.remove(authoritative)
        witness["payload"]["decision_proof"]["epoch_number"] += 1
    elif mutation == "orphan_physical_block":
        replacement = "ff" * 32
        witness["payload"]["block_hash"] = replacement
        witness["payload"]["decision_proof"]["block_hash"] = replacement
    elif mutation == "nonadjacent_source_sequence":
        witness["source_sequence"] += 1
    elif mutation == "designated_source_witness":
        witness["source_id"] = contract["authoritative_source_id"]
        designated = next(
            event
            for event in events
            if event["source_id"] == contract["authoritative_source_id"]
        )
        witness["source_instance"] = designated["source_instance"]
    elif mutation == "carrier_after_common_tick":
        witness["source_monotonic_ns"] = (
            e2["payload"]["e2_common_commit_raw_ns"] + 1
        )
    else:  # pragma: no cover - parameter list is exhaustive
        raise AssertionError(f"unknown mutation {mutation}")

    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation._validate_v13_e2_common_commit(events, contract, cycles)


def test_v13_native_designated_commit_precedes_conflicting_witness() -> None:
    validation, events, contract, _cycles = _native_v13_e2_common_state()
    e2 = next(
        event
        for event in events
        if event["event_type"] == "adaptive_v3.e2_eligibility"
    )
    common = e2["payload"]["e2_common_commit"]
    witness = next(
        event
        for event in events
        if event["event_type"] == "block.commit_identity_witness"
        and event["payload"]["decision_proof"] == common
    )
    witness["payload"]["decision_proof"] = {
        **witness["payload"]["decision_proof"],
        "tree_id": witness["payload"]["decision_proof"]["tree_id"] + 1,
    }

    commits = validation._v13_reconstruct_authoritative_commits(events, contract)
    reconstructed = next(
        event
        for event in commits
        if event["payload"]["block_hash"] == common["block_hash"]
    )

    assert reconstructed["payload"]["decision_proof"] == common


@pytest.mark.parametrize(
    ("event_type", "source_kind", "source_id"),
    [
        ("adaptive_v3.readiness_certificate_assembled", "replica", "replica-2"),
        ("adaptive_v3.readiness_certificate_accepted", "adaptation_manager", "adaptive-manager"),
        ("epoch.activation_ready_signed", "adaptation_manager", "adaptive-manager"),
        ("adaptive_v3.readiness_observation_accepted", "replica", "replica-2"),
    ],
)
def test_v13_native_readiness_source_swaps_fail_closed(
    event_type: str, source_kind: str, source_id: str,
) -> None:
    root = os.environ.get("KAURI_CERT13_NATIVE_FIXTURE_ROOT")
    if root is None:
        pytest.skip("native CERT13 exporter fixture root is not configured")
    validation = _validation()
    contract = {"members": tuple(range(7)), "survivor_barrier_count": 5}
    rows = []
    for stream in ("adaptive-manager-events.jsonl", "replica-events.jsonl"):
        rows.extend(
            json.loads(line)
            for line in (
                Path(root) / "adaptive" / "raw" / stream
            ).read_text().splitlines()
        )
    event = deepcopy(next(row for row in rows if row["event_type"] == event_type))
    event["source_kind"] = source_kind
    event["source_id"] = source_id
    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation._validate_v13_readiness_event_payload(event, contract)


def _mutated_native_v13_arm(
    tmp_path: Path,
    source_root: Path,
    event_type: str,
    mutate: Any,
) -> Path:
    """Copy an actual native arm, then alter exactly one native envelope."""
    arm = tmp_path / "adaptive"
    shutil.copytree(source_root / "adaptive", arm)
    stream = arm / "raw" / "adaptive-manager-events.jsonl"
    rows = [json.loads(line) for line in stream.read_text().splitlines()]
    for row in rows:
        if row["event_type"] == event_type:
            mutate(row)
            stream.write_text("".join(
                json.dumps(item, separators=(",", ":"), sort_keys=True) + "\n"
                for item in rows
            ))
            return arm
    raise AssertionError(f"native adaptive fixture lacks {event_type}")


def _mutate_e2_sources(row: dict[str, object], sources: list[int]) -> None:
    payload = row["payload"]
    assert isinstance(payload, dict)
    payload["observed_signers"] = sources
    payload["e2_common_commit_sources"] = sources
    payload["required_release_count"] = len(sources)


@pytest.mark.parametrize(
    ("event_type", "mutate"),
    [
        ("adaptive_v3.readiness_observation_accepted",
         lambda row: row["payload"].__setitem__("disposition", "garbage")),
        ("adaptive_v3.readiness_certificate_delivery",
         lambda row: row["payload"].__setitem__("disposition", "deadline_expired")),
        ("adaptive_v3.readiness_certificate_acknowledged",
         lambda row: row["payload"].__setitem__("disposition", "garbage")),
        ("adaptive_v3.readiness_terminal",
         lambda row: row["payload"].__setitem__("disposition", "incomplete")),
        ("adaptive_v3.e2_eligibility",
         lambda row: row["payload"].__setitem__("disposition", "garbage")),
        ("adaptive_v3.e2_eligibility",
         lambda row: _mutate_e2_sources(row, [])),
        ("adaptive_v3.e2_eligibility",
         lambda row: _mutate_e2_sources(row, [2, 3, 4, 5])),
        ("adaptive_v3.readiness_observation_accepted",
         lambda row: row.__setitem__("event_type", "adaptive_v3.readiness_legacy_unknown")),
        ("adaptive_v3.readiness_observation_accepted",
         lambda row: row.__setitem__("event_type", "adaptive_v3_readiness_observation_accepted")),
    ],
    ids=("observation-disposition", "delivery-deadline", "ack-disposition",
         "terminal-incomplete", "e2-disposition", "e2-empty-sources",
         "e2-short-sources", "unknown-readiness-name",
         "legacy-underscore-name"),
)
def test_v13_native_exported_readiness_mutations_fail_closed(
    tmp_path: Path, event_type: str, mutate: Any
) -> None:
    root = os.environ.get("KAURI_CERT13_NATIVE_FIXTURE_ROOT")
    if root is None:
        pytest.skip("native CERT13 exporter fixture root is not configured")
    arm = _mutated_native_v13_arm(tmp_path, Path(root), event_type, mutate)
    with pytest.raises(_validation().FocusedCrashPairValidationError):
        _validation()._validate_v13_sources(
            arm,
            {"members": tuple(range(7)), "survivor_barrier_count": 5},
        )


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii") + b"\n"


def test_v13_observation_retry_after_assembly_remains_causal() -> None:
    validation = _validation()
    rows = [
        {
            "source_sequence": 10,
            "source_monotonic_ns": 1_001,
            "payload": {"disposition": "accepted"},
        },
        {
            "source_sequence": 12,
            "source_monotonic_ns": 1_010,
            "payload": {"disposition": "duplicate"},
        },
    ]

    validation._validate_v13_observation_acceptance_causality(
        rows,
        signer_raw_ns=1_000,
        assembled_sequence=11,
        terminal_sequence=13,
    )


@pytest.mark.parametrize(
    "mutation",
    ("primary-after-assembly", "duplicate-before-primary", "after-terminal", "missing-primary"),
)
def test_v13_observation_acceptance_causality_mutations_fail_closed(
    mutation: str,
) -> None:
    validation = _validation()
    rows = [
        {
            "source_sequence": 10,
            "source_monotonic_ns": 1_001,
            "payload": {"disposition": "accepted"},
        },
        {
            "source_sequence": 12,
            "source_monotonic_ns": 1_010,
            "payload": {"disposition": "duplicate"},
        },
    ]
    if mutation == "primary-after-assembly":
        rows[0]["source_sequence"] = 11
    elif mutation == "duplicate-before-primary":
        rows[1]["source_sequence"] = 9
    elif mutation == "after-terminal":
        rows[1]["source_sequence"] = 13
    elif mutation == "missing-primary":
        rows[0]["payload"]["disposition"] = "duplicate"
    else:  # pragma: no cover - parameter list is exhaustive
        raise AssertionError(f"unknown mutation {mutation}")

    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation._validate_v13_observation_acceptance_causality(
            rows,
            signer_raw_ns=1_000,
            assembled_sequence=11,
            terminal_sequence=13,
        )


def _v13_parent_authorization_tree(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    """Write the smallest canonical child tree accepted by the v13 helper."""
    root = tmp_path / "pair-01" / "control"
    runtime = root / "runtime"
    runtime.mkdir(parents=True)
    contract: dict[str, object] = {
        "profile_id": "n7-f2-q5-two-crash-pair-smoke-v13",
        "profile_sha256": "10" * 32,
        "topology_proof_sha256": "20" * 32,
        "profile": {
            "profile_id": "n7-f2-q5-two-crash-pair-smoke-v13",
            "protocol": {"N": 7},
        },
    }
    request: dict[str, object] = {
        "schema_version": 2,
        "mode": "smoke",
        "pair_count": 1,
        "profile_sha256": contract["profile_sha256"],
        "topology_proof_sha256": contract["topology_proof_sha256"],
        "output_root": str(tmp_path.resolve()),
        "automatic_retries": 0,
        "replacement_policy": "none",
        "authorization_nonce": "approval-bound-nonce",
        "execution_context_sha256": "30" * 32,
        "pair_readiness_manifests": {
            "pair-01": {
                "manifest_sha256": "40" * 32,
                "membership_digest": "50" * 32,
                "member_count": 7,
            }
        },
    }
    _write_v13_parent_authorization(root, request)
    return root, contract


def _write_v13_parent_authorization(
    root: Path, request: Mapping[str, object], *, receipt: Mapping[str, object] | None = None,
    canonical: bool = True, pair_id: str = "pair-01",
) -> None:
    request_bytes = _canonical_json(request)
    if receipt is None:
        receipt = {
            **request,
            "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
            "approval_reference": "CERT13 approval",
            "approved_utc": "2026-08-20T00:00:00+00:00",
        }
    runtime = root / "runtime"
    request_path = runtime / "parent-authorization-request.json"
    receipt_path = runtime / "parent-authorization-receipt.json"
    request_path.write_bytes(request_bytes if canonical else request_bytes.rstrip() + b" \n")
    receipt_path.write_bytes(_canonical_json(receipt))
    (root / "pair-receipt.json").write_bytes(_canonical_json({"pair_id": pair_id}))


def test_v13_parent_authorization_projection_is_directly_callable(tmp_path: Path) -> None:
    root, contract = _v13_parent_authorization_tree(tmp_path)
    assert _validation()._validate_v13_parent_authorization_projection(root, contract) == {
        "manifest_sha256": "40" * 32,
        "membership_digest": "50" * 32,
        "member_count": 7,
    }


def test_v13_parent_authorization_accepts_rfc3339_z_utc(tmp_path: Path) -> None:
    root, contract = _v13_parent_authorization_tree(tmp_path)
    request = json.loads(
        (root / "runtime" / "parent-authorization-request.json").read_text()
    )
    request_bytes = _canonical_json(request)
    receipt = {
        **request,
        "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
        "approval_reference": "CERT13 approval",
        "approved_utc": "2026-08-21T10:49:24Z",
    }
    _write_v13_parent_authorization(root, request, receipt=receipt)

    assert _validation()._validate_v13_parent_authorization_projection(
        root, contract
    )["member_count"] == 7


def test_v13_parent_authorization_projection_binds_campaign_slot_path(
    tmp_path: Path,
) -> None:
    original, contract = _v13_parent_authorization_tree(tmp_path)
    root = tmp_path / "children" / "slot-01"
    root.parent.mkdir()
    original.rename(root)
    request = json.loads(
        (root / "runtime" / "parent-authorization-request.json").read_text()
    )
    request["mode"] = "campaign"
    _write_v13_parent_authorization(root, request)
    (root / "pair-receipt.json").write_bytes(
        _canonical_json({"pair_id": "pair-01", "slot_id": "slot-01"})
    )

    assert _validation()._validate_v13_parent_authorization_projection(
        root, contract
    ) == {
        "manifest_sha256": "40" * 32,
        "membership_digest": "50" * 32,
        "member_count": 7,
    }


def test_archived_parent_authorization_schema_one_stays_accepted(tmp_path: Path) -> None:
    root, contract = _v13_parent_authorization_tree(tmp_path)
    contract["profile"] = {"profile_id": "n7-f2-q5-two-crash-pair-smoke-v12"}
    request = json.loads((root / "runtime" / "parent-authorization-request.json").read_text())
    request["schema_version"] = 1
    del request["execution_context_sha256"]
    del request["pair_readiness_manifests"]
    _write_v13_parent_authorization(root, request)
    assert _validation()._validate_parent_authorization_projection(root, contract) is None


def test_archived_parent_authorization_schema_one_accepts_execution_context(tmp_path: Path) -> None:
    root, contract = _v13_parent_authorization_tree(tmp_path)
    contract["profile"] = {"profile_id": "n7-f2-q5-two-crash-pair-smoke-v12"}
    request = json.loads((root / "runtime" / "parent-authorization-request.json").read_text())
    request["schema_version"] = 1
    del request["pair_readiness_manifests"]
    _write_v13_parent_authorization(root, request)
    assert _validation()._validate_parent_authorization_projection(root, contract) is None


def test_archived_parent_authorization_rejects_v13_schema_two(tmp_path: Path) -> None:
    root, contract = _v13_parent_authorization_tree(tmp_path)
    contract["profile"] = {"profile_id": "n7-f2-q5-two-crash-pair-smoke-v12"}
    with pytest.raises(_validation().FocusedCrashPairValidationError):
        _validation()._validate_parent_authorization_projection(root, contract)


def _v13_public_manifest_tree(
    tmp_path: Path,
) -> tuple[Path, dict[str, object], dict[str, object], dict[str, object]]:
    root, contract = _v13_parent_authorization_tree(tmp_path)
    members = [
        {"replica_id": replica, "public_key_hex": f"{replica + 1:02x}" * 48}
        for replica in range(7)
    ]
    validation = _validation()
    manifest: dict[str, object] = {
        "algorithm": "bls-pop",
        "domain": "kauri-adaptive-v3-readiness-public-key-manifest-v1",
        "membership_digest": validation._v13_readiness_membership_digest(members),
        "members": members,
        "profile_id": contract["profile_id"],
        "profile_sha256": contract["profile_sha256"],
        "protocol_mode": "adaptive_v3",
        "schema_version": 1,
    }
    raw = _canonical_json(manifest)
    (root / "runtime" / "activation-readiness-public-manifest.json").write_bytes(raw)
    selected = {
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "membership_digest": manifest["membership_digest"],
        "member_count": 7,
    }
    return root, contract, manifest, selected


def _write_v13_public_manifest(root: Path, manifest: Mapping[str, object], *, canonical: bool = True) -> None:
    raw = _canonical_json(manifest)
    if not canonical:
        raw = raw.rstrip() + b" \n"
    (root / "runtime" / "activation-readiness-public-manifest.json").write_bytes(raw)


def test_v13_readiness_public_manifest_is_directly_callable(tmp_path: Path) -> None:
    root, contract, manifest, selected = _v13_public_manifest_tree(tmp_path)
    assert _validation()._validate_v13_readiness_public_manifest(root, contract, selected) == manifest


@pytest.mark.parametrize(
    "mutation",
    (
        "bytes", "order", "replica_id", "key_length", "key_case", "duplicate_key",
        "key", "membership_digest", "profile", "profile_digest", "mode", "schema",
        "algorithm", "domain", "extra_field", "member_schema",
        "selected_manifest", "selected_membership", "selected_count",
    ),
)
def test_v13_readiness_public_manifest_rejects_mutations(
    tmp_path: Path, mutation: str
) -> None:
    root, contract, manifest, selected = _v13_public_manifest_tree(tmp_path)
    canonical = True
    members = manifest["members"]
    if mutation == "bytes":
        canonical = False
    elif mutation == "order":
        members[0], members[1] = members[1], members[0]
    elif mutation == "replica_id":
        members[0]["replica_id"] = 7
    elif mutation == "key_length":
        members[0]["public_key_hex"] = "01" * 47
    elif mutation == "key_case":
        members[0]["public_key_hex"] = ("ab" * 48).upper()
    elif mutation == "duplicate_key":
        members[1]["public_key_hex"] = members[0]["public_key_hex"]
    elif mutation == "key":
        members[0]["public_key_hex"] = "ff" * 48
    elif mutation == "membership_digest":
        manifest["membership_digest"] = "00" * 32
    elif mutation == "profile":
        manifest["profile_id"] = "n31-f5-q21-three-crash-pair-v13"
    elif mutation == "profile_digest":
        manifest["profile_sha256"] = "00" * 32
    elif mutation == "mode":
        manifest["protocol_mode"] = "adaptive_v2"
    elif mutation == "schema":
        manifest["schema_version"] = 2
    elif mutation == "algorithm":
        manifest["algorithm"] = "bls"
    elif mutation == "domain":
        manifest["domain"] = "wrong-domain"
    elif mutation == "extra_field":
        manifest["unexpected"] = True
    elif mutation == "member_schema":
        members[0]["unexpected"] = True
    elif mutation == "selected_manifest":
        selected["manifest_sha256"] = "00" * 32
    elif mutation == "selected_membership":
        selected["membership_digest"] = "00" * 32
    elif mutation == "selected_count":
        selected["member_count"] = 6
    else:
        raise AssertionError(mutation)
    _write_v13_public_manifest(root, manifest, canonical=canonical)
    with pytest.raises(_validation().FocusedCrashPairValidationError):
        _validation()._validate_v13_readiness_public_manifest(root, contract, selected)


def _v13_verifier_fixture(tmp_path: Path) -> tuple[Path, dict[str, object], dict[str, object], Path, Path, Path, dict[str, object]]:
    root, contract, _manifest, selected = _v13_public_manifest_tree(tmp_path)
    verifier = tmp_path / "epoch-profile-digest"
    verifier.write_bytes(b"#!/bin/sh\nexit 0\n")
    verifier.chmod(0o700)
    certificate = tmp_path / "certificate.hex"
    identity = tmp_path / "identity.hex"
    certificate.write_bytes(b"abcd\n")
    identity.write_bytes(b"1234\n")
    provenance = {"epoch_profile_digest_sha256": hashlib.sha256(verifier.read_bytes()).hexdigest()}
    return root, contract, selected, verifier, certificate, identity, provenance


def _v13_verifier_result(root: Path, contract: Mapping[str, object], certificate: Path, identity: Path) -> dict[str, object]:
    validation = _validation()
    manifest_bytes = (root / "runtime" / "activation-readiness-public-manifest.json").read_bytes()
    certificate_bytes = bytes.fromhex(certificate.read_text().strip())
    identity_bytes = bytes.fromhex(identity.read_text().strip())
    return {
        "certificate_digest": "90" * 32,
        "certificate_payload_digest": hashlib.sha256(certificate_bytes).hexdigest(),
        "expected_identity_payload_digest": hashlib.sha256(identity_bytes).hexdigest(),
        "manifest_payload_digest": hashlib.sha256(manifest_bytes).hexdigest(),
        "member_count": 7,
        "membership_digest": validation._validate_v13_readiness_public_manifest(
            root, contract, {
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "membership_digest": json.loads(manifest_bytes)["membership_digest"],
                "member_count": 7,
            },
        )["membership_digest"],
        "observation_count": 5,
        "schema": "kauri-adaptive-v3-readiness-verification-v1",
        "valid": True,
    }


def test_v13_readiness_verifier_requires_bound_provenance_and_exact_result(tmp_path: Path) -> None:
    root, contract, selected, verifier, certificate, identity, provenance = _v13_verifier_fixture(tmp_path)
    result = _v13_verifier_result(root, contract, certificate, identity)
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def run_command(argv: tuple[str, ...], **kwargs: object) -> object:
        calls.append((argv, kwargs))
        return type("Result", (), {"returncode": 0, "stdout": _canonical_json(result), "stderr": b""})()

    assert _validation()._validate_v13_readiness_verifier(
        root, contract, selected, trusted_provenance=provenance,
        readiness_verifier_path=verifier, certificate_path=certificate,
        identity_path=identity, expected_certificate_digest="90" * 32,
        expected_observation_count=5, run_command=run_command,
    ) == result
    assert len(calls) == 1
    assert calls[0][0][0] != str(verifier)
    assert Path(calls[0][0][0]).name == "verified"
    assert calls[0][1] == {"check": False, "capture_output": True, "text": False,
                            "shell": False, "timeout": 5}


def test_v13_readiness_verifier_executes_the_verified_descriptor_after_path_swap(tmp_path: Path) -> None:
    root, contract, selected, verifier, certificate, identity, provenance = _v13_verifier_fixture(tmp_path)
    result = _v13_verifier_result(root, contract, certificate, identity)
    original = verifier.read_bytes()

    def run_command(_argv: tuple[str, ...], **kwargs: object) -> object:
        replacement = verifier.with_name("epoch-profile-digest-replacement")
        replacement.write_bytes(b"#!/bin/sh\necho replacement\n")
        replacement.chmod(0o700)
        os.replace(replacement, verifier)
        assert Path(_argv[0]).read_bytes() == original
        return type("Result", (), {"returncode": 0, "stdout": _canonical_json(result), "stderr": b""})()

    _validation()._validate_v13_readiness_verifier(
        root, contract, selected, trusted_provenance=provenance,
        readiness_verifier_path=verifier, certificate_path=certificate,
        identity_path=identity, expected_certificate_digest="90" * 32,
        expected_observation_count=5, run_command=run_command,
    )


@pytest.mark.parametrize(
    "mutation",
    ("missing_provenance", "wrong_hash", "tampered_binary", "symlink", "relative_path", "timeout",
     "stderr", "schema", "certificate_payload_digest", "identity_payload_digest",
     "manifest_payload_digest", "certificate_digest"),
)
def test_v13_readiness_verifier_rejects_unbound_or_invalid_results(
    tmp_path: Path, mutation: str
) -> None:
    root, contract, selected, verifier, certificate, identity, provenance = _v13_verifier_fixture(tmp_path)
    result = _v13_verifier_result(root, contract, certificate, identity)
    calls: list[object] = []
    path: Path = verifier
    if mutation == "missing_provenance":
        provenance = {}
    elif mutation == "wrong_hash":
        provenance["epoch_profile_digest_sha256"] = "00" * 32
    elif mutation == "tampered_binary":
        verifier.write_bytes(b"#!/bin/sh\necho changed\n")
        verifier.chmod(0o700)
    elif mutation == "symlink":
        path = tmp_path / "epoch-profile-digest-link"
        path.symlink_to(verifier)
    elif mutation == "relative_path":
        path = Path("epoch-profile-digest")
    elif mutation == "timeout":
        def run_command(*_args: object, **_kwargs: object) -> object:
            calls.append(True)
            raise subprocess.TimeoutExpired("verifier", 5)
    else:
        if mutation == "stderr":
            stderr = b"unexpected\n"
        else:
            stderr = b""
            result[mutation] = "00" * 32 if "digest" in mutation else "wrong-schema"
        def run_command(*_args: object, **_kwargs: object) -> object:
            calls.append(True)
            return type("Result", (), {"returncode": 0, "stdout": _canonical_json(result), "stderr": stderr})()
    if mutation not in {"timeout", "stderr", "schema", "certificate_payload_digest", "identity_payload_digest", "manifest_payload_digest", "certificate_digest"}:
        def run_command(*_args: object, **_kwargs: object) -> object:
            calls.append(True)
            raise AssertionError("subprocess must not run before provenance/path checks")
    with pytest.raises(_validation().FocusedCrashPairValidationError):
        _validation()._validate_v13_readiness_verifier(
            root, contract, selected, trusted_provenance=provenance,
            readiness_verifier_path=path, certificate_path=certificate,
            identity_path=identity, expected_certificate_digest="90" * 32,
            expected_observation_count=5, run_command=run_command,
        )
    if mutation not in {"timeout", "stderr", "schema", "certificate_payload_digest", "identity_payload_digest", "manifest_payload_digest", "certificate_digest"}:
        assert calls == []


def _v13_readiness_wire_chain() -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
    validation = _validation()
    identity: dict[str, object] = {
        "schema_version": 1, "membership_digest": "11" * 32,
        "predecessor_boundary_configuration": {"epoch_number": 0, "tree_id": 1, "epoch_digest": "12" * 32},
        "predecessor_boundary_generation": 1,
        "successor_configuration": {"epoch_number": 1, "tree_id": 0, "epoch_digest": "13" * 32},
        "successor_activation_generation": (1 << 32) + 1,
        "command_payload_digest": "14" * 32, "command_block_height": 10,
        "command_block_hash": "15" * 32, "activation_delay_blocks": 2,
        "activation_height": 12, "activation_boundary_block_hash": "16" * 32,
    }
    observations = [
        {"identity": deepcopy(identity), "signer_replica_id": signer,
         "signer_source_sequence": signer + 1, "signer_monotonic_raw_ns": 100 + signer,
         "vote_fence_engaged": True, "signature_hex": f"{signer + 1:02x}" * 96}
        for signer in (0, 1)
    ]
    certificate: dict[str, object] = {"schema_version": 1, "identity": deepcopy(identity), "observations": observations, "certificate_digest": validation._v13_certificate_digest(identity, observations)}
    certificate_payload = validation._v13_encode_readiness_certificate(certificate)
    acknowledgement: dict[str, object] = {
        "schema_version": 1, "acknowledged_opcode": 0x22, "recipient_replica_id": 0,
        "identity": deepcopy(identity), "certificate_digest": certificate["certificate_digest"],
        "payload_digest": validation._v13_ack_payload_digest(0x22, certificate_payload), "disposition": 1,
    }
    return identity, observations[0], certificate, acknowledgement


def test_v13_delivery_audit_distinguishes_retry_from_enqueue() -> None:
    validation = _validation()
    identity, _observation, certificate, _acknowledgement = (
        _v13_readiness_wire_chain()
    )
    certificate_wire = validation._v13_encode_readiness_certificate(certificate)
    payload: dict[str, object] = {
        key: None for key in validation._V13_READINESS_EVENT_KEYS
    }
    payload.update(
        {
            "identity": identity,
            "replica_id": 0,
            "observed_signers": [],
            "e2_common_commit_sources": [],
            "required_release_count": 0,
            "delivery_attempt": 1,
            "delivery_enqueued": False,
            "canonical_wire_payload_hex": certificate_wire.hex(),
            "certificate_digest": certificate["certificate_digest"],
            "payload_digest": validation._v13_ack_payload_digest(
                validation._V13_READY_CERTIFICATE_OPCODE, certificate_wire
            ),
            "disposition": "retry_scheduled",
        }
    )
    event = {
        "source_kind": "adaptation_manager",
        "source_id": "adaptive-manager",
        "event_type": "adaptive_v3.readiness_certificate_delivery",
        "payload": payload,
    }
    contract = {"members": tuple(range(7)), "survivor_barrier_count": 5}
    validation._validate_v13_readiness_event_payload(event, contract)

    inconsistent_retry = deepcopy(event)
    inconsistent_retry["payload"]["delivery_enqueued"] = True
    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation._validate_v13_readiness_event_payload(
            inconsistent_retry, contract
        )

    inconsistent_queue = deepcopy(event)
    inconsistent_queue["payload"]["disposition"] = "queued"
    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation._validate_v13_readiness_event_payload(
            inconsistent_queue, contract
        )


def test_v13_readiness_wire_is_independently_canonical_and_cross_bound() -> None:
    validation = _validation()
    identity, observation, certificate, acknowledgement = _v13_readiness_wire_chain()
    identity_wire = validation._v13_encode_ready_identity(identity)
    observation_wire = validation._v13_encode_ready_observation(observation)
    certificate_wire = validation._v13_encode_readiness_certificate(certificate)
    ack_wire = validation._v13_encode_readiness_ack(acknowledgement)
    chain = validation._validate_v13_readiness_wire_chain(identity_wire, observation_wire, certificate_wire, ack_wire,
                                                           maximum_members=7, maximum_payload_bytes=65536)
    assert chain["certificate"]["certificate_digest"] == certificate["certificate_digest"]
    assert chain["certificate_payload_digest"] == hashlib.sha256(certificate_wire).hexdigest()
    assert chain["ack_payload_digest"] != chain["certificate_payload_digest"]


@pytest.mark.parametrize("mutation", ("domain", "flags", "schema", "truncated", "trailing", "certificate_digest", "ack_opcode", "ack_disposition", "duplicate", "identity"))
def test_v13_readiness_wire_rejects_canonicality_and_binding_mutations(mutation: str) -> None:
    validation = _validation()
    identity, observation, certificate, acknowledgement = _v13_readiness_wire_chain()
    identity_wire = validation._v13_encode_ready_identity(identity)
    observation_wire = validation._v13_encode_ready_observation(observation)
    certificate_wire = validation._v13_encode_readiness_certificate(certificate)
    ack_wire = validation._v13_encode_readiness_ack(acknowledgement)
    if mutation == "domain":
        identity_wire = b"x" + identity_wire[1:]
    elif mutation == "flags":
        identity_wire = identity_wire[:len(validation._V13_READY_IDENTITY_DOMAIN)] + b"\x01" + identity_wire[len(validation._V13_READY_IDENTITY_DOMAIN) + 1:]
    elif mutation == "schema":
        schema_offset = len(validation._V13_READY_IDENTITY_DOMAIN) + 1
        identity_wire = identity_wire[:schema_offset + 3] + b"\x02" + identity_wire[schema_offset + 4:]
    elif mutation == "truncated":
        observation_wire = observation_wire[:-1]
    elif mutation == "trailing":
        certificate_wire += b"\0"
    elif mutation == "certificate_digest":
        acknowledgement["certificate_digest"] = "ff" * 32
        ack_wire = validation._v13_encode_readiness_ack(acknowledgement)
    elif mutation == "ack_opcode":
        acknowledgement["acknowledged_opcode"] = 0x21
        acknowledgement["payload_digest"] = validation._v13_ack_payload_digest(0x21, certificate_wire)
        ack_wire = validation._v13_encode_readiness_ack(acknowledgement)
    elif mutation == "ack_disposition":
        acknowledgement["disposition"] = 3
        with pytest.raises(validation.FocusedCrashPairValidationError):
            validation._v13_encode_readiness_ack(acknowledgement)
        return
    elif mutation == "duplicate":
        certificate["observations"][1]["signer_replica_id"] = 0
        with pytest.raises(validation.FocusedCrashPairValidationError):
            validation._v13_encode_readiness_certificate(certificate)
        return
    elif mutation == "identity":
        certificate["identity"]["activation_height"] = 11
        with pytest.raises(validation.FocusedCrashPairValidationError):
            validation._v13_encode_readiness_certificate(certificate)
        return
    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation._validate_v13_readiness_wire_chain(identity_wire, observation_wire, certificate_wire, ack_wire,
                                                       maximum_members=7, maximum_payload_bytes=65536)


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_request_key", "extra_request_key", "schema_drift", "noncanonical",
        "request_sha", "nested_mirror", "missing_pair", "renamed_pair", "extra_pair",
        "selected_pair", "member_count_low", "member_count_high", "member_count_bool",
        "malformed_digest", "uppercase_digest", "duplicate_manifest", "duplicate_membership",
        "execution_digest", "profile_binding", "proof_binding", "retries", "replacement",
    ),
)
def test_v13_parent_authorization_projection_rejects_mutations(
    tmp_path: Path, mutation: str
) -> None:
    root, contract = _v13_parent_authorization_tree(tmp_path)
    request = json.loads((root / "runtime" / "parent-authorization-request.json").read_text())
    canonical = True
    pair_id = "pair-01"
    receipt: Mapping[str, object] | None = None
    rows = request["pair_readiness_manifests"]
    if mutation == "missing_request_key":
        del request["execution_context_sha256"]
    elif mutation == "extra_request_key":
        request["unexpected"] = True
    elif mutation == "schema_drift":
        request["schema_version"] = 1
    elif mutation == "noncanonical":
        canonical = False
    elif mutation == "request_sha":
        receipt = {**request, "request_sha256": "00" * 32,
                   "approval_reference": "CERT13 approval", "approved_utc": "2026-08-20T00:00:00+00:00"}
    elif mutation == "nested_mirror":
        receipt = {**request, "pair_readiness_manifests": {}, "request_sha256": "00" * 32,
                   "approval_reference": "CERT13 approval", "approved_utc": "2026-08-20T00:00:00+00:00"}
    elif mutation == "missing_pair":
        request["pair_readiness_manifests"] = {}
    elif mutation == "renamed_pair":
        request["pair_readiness_manifests"] = {"pair-1": rows["pair-01"]}
    elif mutation == "extra_pair":
        request["pair_readiness_manifests"]["pair-02"] = deepcopy(rows["pair-01"])
    elif mutation == "selected_pair":
        pair_id = "pair-02"
    elif mutation == "member_count_low":
        rows["pair-01"]["member_count"] = 6
    elif mutation == "member_count_high":
        rows["pair-01"]["member_count"] = 8
    elif mutation == "member_count_bool":
        rows["pair-01"]["member_count"] = True
    elif mutation == "malformed_digest":
        rows["pair-01"]["manifest_sha256"] = "40" * 31
    elif mutation == "uppercase_digest":
        rows["pair-01"]["membership_digest"] = ("ab" * 32).upper()
    elif mutation == "duplicate_manifest":
        request["pair_count"] = 2
        request["pair_readiness_manifests"]["pair-02"] = deepcopy(rows["pair-01"])
    elif mutation == "duplicate_membership":
        request["pair_count"] = 2
        request["pair_readiness_manifests"]["pair-02"] = {
            **deepcopy(rows["pair-01"]), "manifest_sha256": "60" * 32
        }
    elif mutation == "execution_digest":
        request["execution_context_sha256"] = "not-a-digest"
    elif mutation == "profile_binding":
        request["profile_sha256"] = "70" * 32
    elif mutation == "proof_binding":
        request["topology_proof_sha256"] = "80" * 32
    elif mutation == "retries":
        request["automatic_retries"] = 1
    elif mutation == "replacement":
        request["replacement_policy"] = "replace"
    else:
        raise AssertionError(mutation)
    _write_v13_parent_authorization(
        root, request, receipt=receipt, canonical=canonical, pair_id=pair_id
    )
    with pytest.raises(_validation().FocusedCrashPairValidationError):
        _validation()._validate_v13_parent_authorization_projection(root, contract)


def _required_callable(module: Any, name: str) -> Any:
    value = getattr(module, name, None)
    assert callable(value), f"CERT13 missing production callable {name}"
    return value


def _profile_document(*, replicas: int, quorum: int, reporters: int) -> dict[str, Any]:
    assert (replicas, quorum, reporters) in {(7, 5, 5), (31, 21, 28)}
    profile_id = (
        "n7-f2-q5-two-crash-pair-smoke-v13"
        if replicas == 7
        else "n31-f5-q21-three-crash-pair-v13"
    )
    return {
        "schema_version": 2,
        "profile_id": profile_id,
        "frozen": True,
        "protocol": {
            "N": replicas,
            "f": (replicas - 1) // 3,
            "Q": quorum,
            "fanout": 2 if replicas == 7 else 5,
            "pipeline_stretch": 2,
            "transactions_per_block": 1000,
            "epoch_protocol_mode": "adaptive_v3",
        },
        "timers": {
            "optimization_activation_deadline_seconds": 90,
            "arm_hard_deadline_seconds": 480 if replicas == 7 else 780,
            "stable_phase_seconds": 30,
        },
        "transitions": {
            "survivor_barrier_count": reporters,
            "common_commit_quorum": quorum,
            "activation_readiness_contract": {
                "schema_version": 1,
                "domain": "kauri-focused-v13-certified-activation-v1",
                "certificate_quorum": quorum,
                "required_reporter_count": reporters,
                "epoch1_common_commit_anchor_deadline_seconds": 5,
                "containment_stabilization_seconds": 30,
                "containment_measurement_seconds": 30,
                "minimum_predecessor_residency_ms": 65_000,
                "optimization_activation_budget_seconds": 90,
                "deadline_semantics": "half_open_monotonic_v1",
                "readiness_ledger_schema": "kauri-focused-readiness-ledger-v1",
            },
        },
        "topology": {
            "proof_path": f"topology-proofs/{profile_id}-topology-proof.json",
            "proof_sha256": "71" * 32,
        },
    }


def _identity(epoch: int) -> dict[str, Any]:
    assert epoch in {1, 2}
    predecessor = _DIGESTS["epoch0" if epoch == 1 else "epoch1"]
    successor = _DIGESTS[f"epoch{epoch}"]
    command_height = 100 if epoch == 1 else 200
    return {
        "schema_version": 1,
        "membership_digest": _DIGESTS["membership"],
        "predecessor_boundary_configuration": {
            "epoch_number": epoch - 1,
            "tree_id": 4,
            "epoch_digest": predecessor,
        },
        "predecessor_boundary_generation": 8 if epoch == 1 else 12,
        "successor_configuration": {
            "epoch_number": epoch,
            "tree_id": 0,
            "epoch_digest": successor,
        },
        "successor_activation_generation": 12 if epoch == 1 else 16,
        "command_payload_digest": _DIGESTS[f"command{epoch}"],
        "command_block_height": command_height,
        "command_block_hash": _DIGESTS[f"command_block{epoch}"],
        "readiness_delay_blocks": 5,
        "readiness_height": command_height + 5,
        "readiness_boundary_block_hash": _DIGESTS[f"boundary{epoch}"],
    }


def _readiness_events() -> list[dict[str, Any]]:
    survivors = (2, 3, 4, 5, 6)
    events: list[dict[str, Any]] = []
    source_sequences = {replica: 0 for replica in survivors}
    manager_sequence = 0

    for epoch in (1, 2):
        identity = _identity(epoch)
        epoch_base = epoch * 1_000_000_000
        for ordinal, replica in enumerate(survivors):
            source_sequences[replica] += 1
            prepared_ns = epoch_base + ordinal * 1_000
            events.append(
                {
                    "event_type": "epoch.activation_prepared",
                    "source_kind": "replica",
                    "source_replica_id": replica,
                    "source_instance": f"replica-{replica}-instance",
                    "source_sequence": source_sequences[replica],
                    "source_monotonic_ns": prepared_ns,
                    "payload": {
                        "identity": deepcopy(identity),
                        "vote_fence_engaged": True,
                    },
                }
            )
            source_sequences[replica] += 1
            signed_ns = prepared_ns + 1
            events.append(
                {
                    "event_type": "epoch.activation_ready_signed",
                    "source_kind": "replica",
                    "source_replica_id": replica,
                    "source_instance": f"replica-{replica}-instance",
                    "source_sequence": source_sequences[replica],
                    "source_monotonic_ns": signed_ns,
                    "payload": {
                        "identity": deepcopy(identity),
                        "signer_replica_id": replica,
                        "signer_source_sequence": source_sequences[replica],
                        "signer_monotonic_raw_ns": signed_ns,
                        "vote_fence_engaged": True,
                        "signature": f"{replica + epoch:02x}" * 64,
                    },
                }
            )
            manager_sequence += 1
            events.append(
                {
                    "event_type": "manager.activation_readiness_accepted",
                    "source_kind": "adaptive-manager",
                    "source_instance": "manager-instance",
                    "source_sequence": manager_sequence,
                    "source_monotonic_ns": signed_ns + 100,
                    "payload": {
                        "identity": deepcopy(identity),
                        "authenticated_replica_id": replica,
                        "signer_replica_id": replica,
                    },
                }
            )

        manager_sequence += 1
        certificate_ns = epoch_base + 10_000
        events.append(
            {
                "event_type": "manager.activation_readiness_certificate_assembled",
                "source_kind": "adaptive-manager",
                "source_instance": "manager-instance",
                "source_sequence": manager_sequence,
                "source_monotonic_ns": certificate_ns,
                "payload": {
                    "identity": deepcopy(identity),
                    "certificate_digest": _DIGESTS[f"certificate{epoch}"],
                    "certificate_quorum": 5,
                    "required_reporter_count": 5,
                    "signer_replica_ids": list(survivors),
                },
            }
        )
        for ordinal, replica in enumerate(survivors):
            source_sequences[replica] += 1
            events.append(
                {
                    "event_type": "epoch.activated",
                    "source_kind": "replica",
                    "source_replica_id": replica,
                    "source_instance": f"replica-{replica}-instance",
                    "source_sequence": source_sequences[replica],
                    "source_monotonic_ns": certificate_ns + 1 + ordinal,
                    "payload": {
                        "identity": deepcopy(identity),
                        "certificate_digest": _DIGESTS[f"certificate{epoch}"],
                        "scheduled_readiness_height": identity["readiness_height"],
                        "certificate_apply_committed_height": (
                            identity["readiness_height"] + ordinal
                        ),
                    },
                }
            )
    return events


def _assert_fixture_is_well_formed(events: list[dict[str, Any]]) -> None:
    assert len(events) == 42
    for epoch in (1, 2):
        identity = _identity(epoch)
        assert identity["readiness_height"] == (
            identity["command_block_height"]
            + identity["readiness_delay_blocks"]
        )
        signed = [
            event
            for event in events
            if event["event_type"] == "epoch.activation_ready_signed"
            and event["payload"]["identity"] == identity
        ]
        activated = [
            event
            for event in events
            if event["event_type"] == "epoch.activated"
            and event["payload"]["identity"] == identity
        ]
        assert [event["source_replica_id"] for event in signed] == [2, 3, 4, 5, 6]
        assert [event["source_replica_id"] for event in activated] == [2, 3, 4, 5, 6]
        assert all(event["payload"]["vote_fence_engaged"] is True for event in signed)


class _ForbiddenValue:
    def __getattribute__(self, name: str) -> Any:
        raise AssertionError(f"source-blind reconstruction accessed forbidden {name}")

    def __repr__(self) -> str:
        raise AssertionError("source-blind reconstruction rendered forbidden value")


def test_cert13_synthetic_readiness_fixture_is_internally_coherent() -> None:
    _assert_fixture_is_well_formed(_readiness_events())


def test_v1_v12_archive_identity_stays_exact_before_v13_implementation() -> None:
    runtime = _runtime()
    validation = _validation()
    n7 = runtime.load_focused_profile(N7_V12)
    n31 = runtime.load_focused_profile(N31_V12)
    assert (n7.profile_sha256, n7.topology_proof_sha256) == (
        "54a879d783e071da2fce59773c79439a691ed549b50296c6f1bcea848694e697",
        "dc30f42f28d7228a44efe69f734cceebf965169202923b52c786f6e58e2e81d9",
    )
    assert (n31.profile_sha256, n31.topology_proof_sha256) == (
        "2686e76451dea29018e33c87756b6c722badf82fd0d0c8f3500b860b424164ea",
        "7dbab26b7272d3f31e8bbcb38dc5778fc3666f11d5d5586dae67f499547f3ca2",
    )
    assert runtime._is_v12_profile(n7)
    assert runtime._is_v12_profile(n31)
    assert validation._FCRASH_H_V12_PROFILE_IDS == frozenset(
        {
            "n7-f2-q5-two-crash-pair-smoke-v12",
            "n31-f5-q21-three-crash-pair-v12",
        }
    )
    assert validation._FCRASH_H_V12_PROFILE_IDS.isdisjoint(V13_PROFILE_IDS)


def test_v13_exact_profile_and_cli_boundary_is_declared_without_rebinding_v12() -> None:
    runtime = _runtime()
    validation = _validation()
    runner = _runner()
    assert getattr(runtime, "_FCRASH_H_V13_PROFILE_IDS", None) == V13_PROFILE_IDS, (
        "CERT13 missing exact runtime v13 profile identities"
    )
    assert getattr(validation, "_FCRASH_H_V13_PROFILE_IDS", None) == V13_PROFILE_IDS, (
        "CERT13 missing independent validator v13 profile identities"
    )
    assert getattr(runner, "_V13_PROFILES", None) == {
        "smoke": N7_V13,
        "pair": N31_V13,
        "campaign": N31_V13,
    }, "CERT13 missing exact v13-only CLI execution routing"
    assert set(runner._V13_PROFILES.values()).isdisjoint(
        runner._V12_PROFILES.values()
    )


@pytest.mark.parametrize(
    ("replicas", "quorum", "reporters"), ((7, 5, 5), (31, 21, 28))
)
def test_v13_contract_keeps_protocol_quorum_distinct_from_manager_release_count(
    replicas: int, quorum: int, reporters: int
) -> None:
    validate = _required_callable(_runtime(), "_v13_certified_activation_contract")
    contract = validate(_profile_document(
        replicas=replicas, quorum=quorum, reporters=reporters
    ))
    assert contract["certificate_quorum"] == quorum
    assert contract["required_reporter_count"] == reporters
    assert contract["required_reporter_count"] >= contract["certificate_quorum"]
    assert contract["required_reporter_count"] <= replicas


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("certificate_quorum", 4),
        ("required_reporter_count", 4),
        ("required_reporter_count", 8),
        ("optimization_activation_budget_seconds", 89),
        ("minimum_predecessor_residency_ms", 64_999),
    ),
)
def test_v13_contract_rejects_cardinality_or_frozen_timing_drift(
    field: str, value: int
) -> None:
    runtime = _runtime()
    validate = _required_callable(runtime, "_v13_certified_activation_contract")
    profile = _profile_document(replicas=7, quorum=5, reporters=5)
    profile["transitions"]["activation_readiness_contract"][field] = value
    with pytest.raises(runtime.FocusedCrashPairRuntimeError):
        validate(profile)


def test_v13_readiness_ledger_binds_both_complete_survivor_transitions() -> None:
    build = _required_callable(_runtime(), "_v13_readiness_ledger_document")
    profile = _profile_document(replicas=7, quorum=5, reporters=5)
    events = _readiness_events()
    _assert_fixture_is_well_formed(events)
    ledger = build(profile=profile, events=events)
    assert ledger["schema_version"] == 1
    assert ledger["domain"] == "kauri-focused-readiness-ledger-v1"
    assert ledger["profile_id"] == profile["profile_id"]
    assert ledger["protocol_mode"] == "adaptive_v3"
    assert ledger["certificate_quorum"] == 5
    assert ledger["required_reporter_count"] == 5
    assert [row["successor_epoch_number"] for row in ledger["transitions"]] == [1, 2]
    assert all(row["observed_reporter_ids"] == [2, 3, 4, 5, 6] for row in ledger["transitions"])
    assert all(row["activated_replica_ids"] == [2, 3, 4, 5, 6] for row in ledger["transitions"])


def test_v13_source_blind_reconstruction_precedes_fault_receipt_join() -> None:
    validation = _validation()
    reconstruct = _required_callable(
        validation, "_reconstruct_v13_activation_readiness"
    )
    join = _required_callable(validation, "_join_v13_fault_receipt")
    profile = _profile_document(replicas=7, quorum=5, reporters=5)
    events = _readiness_events()
    _assert_fixture_is_well_formed(events)
    reconstruction = reconstruct(
        profile=profile,
        events=events,
        forbidden_context={
            "fault_receipt": _ForbiddenValue(),
            "runner_verdict": _ForbiddenValue(),
            "process_status": _ForbiddenValue(),
        },
    )
    assert reconstruction["observed_reporter_ids"] == [2, 3, 4, 5, 6]
    assert reconstruction["reconstruction_digest"]

    receipt = {
        "confirmed_target_ids": [0, 1],
        "survivor_replica_ids": [2, 3, 4, 5, 6],
    }
    joined = join(reconstruction=reconstruction, fault_receipt=receipt)
    assert joined["expected_survivor_ids"] == [2, 3, 4, 5, 6]
    assert joined["observed_survivor_ids"] == [2, 3, 4, 5, 6]
    assert joined["exact_survivor_set_match"] is True
    assert joined["source_blind_reconstruction_digest"] == (
        reconstruction["reconstruction_digest"]
    )


@pytest.mark.parametrize(
    ("candidate_offset_ns", "hard_offset_ns", "expected"),
    (
        (0, 100_000_000_000, False),
        (1, 100_000_000_000, True),
        (89_999_999_999, 100_000_000_000, True),
        (90_000_000_000, 100_000_000_000, False),
        (89_999_999_999, 89_999_999_999, False),
    ),
)
def test_v13_e2_budget_is_half_open_and_anchored_to_final_common_command(
    candidate_offset_ns: int, hard_offset_ns: int, expected: bool
) -> None:
    timely = _required_callable(_runtime(), "_v13_e2_activation_is_timely")
    final_common_command_ns = 500_000_000_000
    assert timely(
        final_common_command_ns=final_common_command_ns,
        activation_ns=final_common_command_ns + candidate_offset_ns,
        hard_deadline_ns=final_common_command_ns + hard_offset_ns,
        budget_seconds=90,
    ) is expected


def test_v13_e2_budget_does_not_depend_on_the_earlier_epoch1_activation_anchor() -> None:
    timely = _required_callable(_runtime(), "_v13_e2_activation_is_timely")
    final_common_command_ns = 500_000_000_000
    candidate_ns = final_common_command_ns + 60_000_000_000
    hard_deadline_ns = final_common_command_ns + 100_000_000_000
    baseline = timely(
        final_common_command_ns=final_common_command_ns,
        activation_ns=candidate_ns,
        hard_deadline_ns=hard_deadline_ns,
        budget_seconds=90,
    )
    assert baseline is True
    # An Epoch-1 activation timestamp is deliberately absent from the API: it
    # cannot consume any portion of the independently anchored Epoch-2 budget.
    assert "epoch1" not in timely.__code__.co_varnames
