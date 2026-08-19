"""Focused unit contracts for the v4 post-fault window arm.

These exercise the persisted arm binding in isolation.  They deliberately do
not manufacture a sealed run: the full validator/seal integration is covered
by the native-shaped crash-pair fixtures.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
from typing import Any, Callable

import pytest

RUNTIME = "experiments.adaptive.kauri_experiment.focused_crash_pair_runtime"
VALIDATION = "experiments.adaptive.kauri_experiment.focused_crash_pair_validation"
PROFILE_ROOT = Path(__file__).parents[1] / "profiles"


def _runtime() -> Any:
    return importlib.import_module(RUNTIME)


def _validation() -> Any:
    return importlib.import_module(VALIDATION)


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def _arm_fixture(
    tmp_path: Path,
) -> tuple[Any, Path, dict[str, Any], list[dict[str, Any]], dict[str, Any], list[str]]:
    """Return a minimal independently-valid v4 arm boundary."""

    validation = _validation()
    root = tmp_path / "pair-01" / "control"
    (root / "runtime").mkdir(parents=True)
    (root / "raw").mkdir()
    receipt = {
        "sigkill_outcomes": [
            {"requested_monotonic_ns": 70, "confirmed_monotonic_ns": 80},
            {"requested_monotonic_ns": 71, "confirmed_monotonic_ns": 90},
        ]
    }
    receipt_bytes = _canonical(receipt)
    (root / "raw" / "fault-receipt.json").write_bytes(receipt_bytes)
    output_root = str(tmp_path.resolve())
    parent_request = {
        "schema_version": 1,
        "mode": "smoke",
        "pair_count": 1,
        "profile_sha256": "b" * 64,
        "topology_proof_sha256": "c" * 64,
        "output_root": output_root,
        "automatic_retries": 0,
        "replacement_policy": "none",
        "authorization_nonce": hashlib.sha256(
            f"smoke:1:{output_root}".encode()
        ).hexdigest(),
    }
    parent_request_bytes = _canonical(parent_request)
    request_sha = hashlib.sha256(parent_request_bytes).hexdigest()
    (root / "runtime" / "parent-authorization-request.json").write_bytes(
        parent_request_bytes
    )
    (root / "runtime" / "parent-authorization-receipt.json").write_bytes(
        _canonical(
            {
                **parent_request,
                "request_sha256": request_sha,
                "approval_reference": "test approval",
                "approved_utc": "2026-08-18T00:00:00Z",
            }
        )
    )
    (root / "pair-receipt.json").write_bytes(_canonical({"pair_id": "pair-01"}))
    arm = {
        "schema_version": 1,
        "kind": "kauri-focused-fault-window-arm-v1",
        "run_id": "run-v4",
        "profile_id": "n7-f2-q5-two-crash-pair-smoke-v4",
        "profile_sha256": "b" * 64,
        "topology_proof_sha256": "c" * 64,
        "request_sha256": request_sha,
        "epoch_number": 0,
        "epoch_digest": "d" * 64,
        "fault_receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        "evidence_start_monotonic_ns": 90,
        "prefault_tree_id": 6,
        "required_tree_positions": 2,
        "required_tree_ids": [6, 0],
    }
    path = root / "runtime" / "fault-window-arm.json"
    path.write_bytes(_canonical(arm))
    arm_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    events = [
        {
            "run_id": "run-v4",
            "source_kind": "replica",
            "source_id": "replica-0",
            "source_instance": "replica-0-v4",
            "source_sequence": sequence,
            "source_monotonic_ns": monotonic_ns,
            "event_type": event_type,
            "payload": payload,
        }
        for sequence, monotonic_ns, event_type, payload in [
            (1, 1, "process.started", {}),
            (2, 2, "process.ready", {}),
            *[
                (
                    tree + 3,
                    tree + 3,
                    "adaptive.configuration_active",
                    {"epoch_number": 0, "tree_id": tree, "epoch_digest": "d" * 64},
                )
                for tree in range(7)
            ],
            (
                10,
                92,
                "adaptive.configuration_active",
                {"epoch_number": 0, "tree_id": 0, "epoch_digest": "d" * 64},
            ),
        ]
    ] + [
        {
            "run_id": "run-v4",
            "source_kind": "adaptation_manager",
            "source_id": "adaptive-manager",
            "source_instance": "manager-v4",
            "source_monotonic_ns": 95,
            "event_type": "fault_window_armed",
            "payload": {**arm, "fault_window_arm_sha256": arm_sha},
        }
    ]
    contract = {
        "profile_id": arm["profile_id"],
        "profile_sha256": arm["profile_sha256"],
        "topology_proof_sha256": arm["topology_proof_sha256"],
        "epoch_zero_digest": arm["epoch_digest"],
        "profile": {},
        "members": tuple(range(7)),
        "authoritative_replica_id": 0,
        "transactions_per_block": 1_000,
        "reporter_coverage_plan": {
            "active_tree_id": 6,
            "required_postfault_tree_positions": 2,
            "deadlines_seconds": {"arm_hard_seconds": 330},
        },
    }
    pairs = {
        "--fault-window-arm-path": str(path.resolve()),
        "--fault-window-arm-schema-version": "1",
        "--fault-window-arm-domain": "kauri-focused-fault-window-arm-v1",
        "--fault-window-arm-run-id": "run-v4",
        "--fault-window-arm-profile-id": arm["profile_id"],
        "--fault-window-arm-profile-sha256": arm["profile_sha256"],
        "--fault-window-arm-topology-proof-sha256": arm["topology_proof_sha256"],
        "--fault-window-arm-request-sha256": request_sha,
        "--fault-window-arm-epoch-number": "0",
        "--fault-window-arm-epoch-digest": arm["epoch_digest"],
        "--fault-window-arm-prefault-tree-id": "6",
        "--fault-window-arm-required-tree-positions": "2",
        "--fault-window-arm-deadline-seconds": "330",
    }
    argv = ["manager", *[item for pair in pairs.items() for item in pair]]
    assert validation._is_v4_contract(contract)
    return validation, root, arm, events, contract, argv


def _validate_fixture(tmp_path: Path) -> None:
    validation, root, _arm, events, contract, argv = _arm_fixture(tmp_path)
    validation._validate_fault_window_arm(
        root,
        contract,
        argv,
        json.loads((root / "raw" / "fault-receipt.json").read_bytes()),
        {0: 80, 1: 90},
        events,
        snapshot_audit_ns=100,
    )


def test_v4_arm_minimal_boundary_is_independently_accepted(tmp_path: Path) -> None:
    _validate_fixture(tmp_path)


def test_v4_parent_authorization_accepts_explicit_zero_utc_offset(
    tmp_path: Path,
) -> None:
    validation, root, _arm, events, contract, argv = _arm_fixture(tmp_path)
    receipt_path = root / "runtime" / "parent-authorization-receipt.json"
    receipt = json.loads(receipt_path.read_bytes())
    receipt["approved_utc"] = "2026-08-18T00:00:00+00:00"
    receipt_path.write_bytes(_canonical(receipt))
    validation._validate_fault_window_arm(
        root,
        contract,
        argv,
        json.loads((root / "raw" / "fault-receipt.json").read_bytes()),
        {0: 80, 1: 90},
        events,
        snapshot_audit_ns=100,
    )


def test_v4_arm_validation_is_relocation_safe(tmp_path: Path) -> None:
    validation, root, _arm, events, contract, argv = _arm_fixture(tmp_path)
    archive = tmp_path / "archive"
    archive.mkdir()
    relocated_pair = archive / "pair-01"
    root.parent.rename(relocated_pair)
    relocated_root = relocated_pair / root.name
    validation._validate_fault_window_arm(
        relocated_root,
        contract,
        argv,
        json.loads((relocated_root / "raw" / "fault-receipt.json").read_bytes()),
        {0: 80, 1: 90},
        events,
        snapshot_audit_ns=100,
    )


@pytest.mark.parametrize("mutation", ("missing-position", "skipped-position"))
def test_v4_arm_requires_the_full_authoritative_horizon_before_publication(
    tmp_path: Path, mutation: str
) -> None:
    validation, root, _arm, events, contract, argv = _arm_fixture(tmp_path)
    postfault = next(
        event
        for event in events
        if event["source_id"] == "replica-0"
        and event["event_type"] == "adaptive.configuration_active"
        and event["source_monotonic_ns"] == 92
    )
    if mutation == "missing-position":
        events.remove(postfault)
    else:
        postfault["payload"]["tree_id"] = 1
    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation._validate_fault_window_arm(
            root,
            contract,
            argv,
            json.loads((root / "raw" / "fault-receipt.json").read_bytes()),
            {0: 80, 1: 90},
            events,
            snapshot_audit_ns=100,
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda arm: arm.pop("kind"),
        lambda arm: arm.__setitem__("extra", 1),
        lambda arm: arm.__setitem__("schema_version", True),
        lambda arm: arm.__setitem__("profile_sha256", True),
        lambda arm: arm.__setitem__("epoch_number", True),
        lambda arm: arm.__setitem__("required_tree_positions", True),
        lambda arm: arm.__setitem__("required_tree_ids", [6, 0, 1]),
        lambda arm: arm.__setitem__("required_tree_ids", [6, 6]),
        lambda arm: arm.__setitem__("required_tree_ids", [0, 1]),
        lambda arm: arm.__setitem__("prefault_tree_id", 5),
        lambda arm: arm.__setitem__("evidence_start_monotonic_ns", 89),
        lambda arm: arm.__setitem__("fault_receipt_sha256", "e" * 64),
        lambda arm: arm.__setitem__("request_sha256", "e" * 64),
        lambda arm: arm.__setitem__("epoch_digest", "e" * 64),
    ],
    ids=(
        "missing-key",
        "extra-key",
        "bool-schema",
        "bool-digest",
        "bool-epoch",
        "bool-position-count",
        "wrong-prefix-length",
        "duplicate-prefix",
        "wrong-prefix-order",
        "wrong-prefix-start",
        "before-confirmation-boundary",
        "wrong-receipt",
        "wrong-request",
        "wrong-epoch",
    ),
)
def test_v4_arm_rejects_schema_and_binding_drift(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None]
) -> None:
    validation, root, arm, events, contract, argv = _arm_fixture(tmp_path)
    mutate(arm)
    path = root / "runtime" / "fault-window-arm.json"
    path.write_bytes(_canonical(arm))
    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation._validate_fault_window_arm(
            root,
            contract,
            argv,
            json.loads((root / "raw" / "fault-receipt.json").read_bytes()),
            {0: 80, 1: 90},
            events,
            snapshot_audit_ns=100,
        )


@pytest.mark.parametrize(
    "event_mutation",
    ["wrong-source", "wrong-instance", "too-early", "after-audit", "hash-tamper"],
)
def test_v4_arm_rejects_armed_event_provenance_and_time(
    tmp_path: Path, event_mutation: str
) -> None:
    validation, root, _arm, events, contract, argv = _arm_fixture(tmp_path)
    event = next(
        candidate
        for candidate in events
        if candidate["event_type"] == "fault_window_armed"
    )
    if event_mutation == "wrong-source":
        event["source_id"] = "shadow-manager"
    elif event_mutation == "wrong-instance":
        events.append({**event, "source_instance": "shadow-instance"})
    elif event_mutation == "too-early":
        event["source_monotonic_ns"] = 89
    elif event_mutation == "after-audit":
        event["source_monotonic_ns"] = 100
    else:
        event["payload"] = {**event["payload"], "fault_window_arm_sha256": "f" * 64}
    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation._validate_fault_window_arm(
            root,
            contract,
            argv,
            json.loads((root / "raw" / "fault-receipt.json").read_bytes()),
            {0: 80, 1: 90},
            events,
            snapshot_audit_ns=100,
        )


def _anchor_replay_fixture(
    tmp_path: Path,
) -> tuple[Any, dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Build a compact predecessor-0 v4 replay without a sealed run."""

    validation, _root, arm, events, contract, _argv = _arm_fixture(tmp_path)
    digest = str(arm["epoch_digest"])
    contract["reporter_coverage_plan"] = {
        "active_tree_id": 6,
        "required_postfault_tree_positions": 2,
        "targets": [
            {
                "target_replica_id": 1,
                "first_qualifying_reporters": [
                    {"reporter_id": 2, "tree_id": 6},
                    {"reporter_id": 3, "tree_id": 0},
                ],
            }
        ],
    }

    def accepted(
        *,
        sequence: int,
        observation_id: str,
        reporter: int,
        target: int,
        tree: int,
        outcome: str,
        block: str,
        message: str,
        reporter_ns: int = 1_100,
    ) -> dict[str, Any]:
        return {
            "source_kind": "adaptation_manager",
            "source_id": "adaptive-manager",
            "source_sequence": sequence + 10,
            "source_monotonic_ns": reporter_ns + 1,
            "event_type": "evidence.observation_accepted",
            "payload": {
                "ingestion_sequence": sequence,
                "observation": {
                    "observation_id": observation_id,
                    "reporter_id": reporter,
                    "observed_replica_id": target,
                    "configuration": {
                        "epoch_number": 0,
                        "tree_id": tree,
                        "epoch_digest": digest,
                    },
                    "block_hash": block,
                    "expected_message_type": message,
                    "outcome": outcome,
                    "response_duration_us": 0,
                    "reporter_monotonic_ns": reporter_ns,
                },
            },
        }

    events.extend(
        [
            accepted(
                sequence=1,
                observation_id="a" * 64,
                reporter=2,
                target=1,
                tree=6,
                outcome="on_time",
                block="1" * 64,
                message="direct_vote",
            ),
            accepted(
                sequence=2,
                observation_id="b" * 64,
                reporter=3,
                target=1,
                tree=0,
                outcome="on_time",
                block="2" * 64,
                message="direct_vote",
            ),
            accepted(
                sequence=3,
                observation_id="c" * 64,
                reporter=2,
                target=1,
                tree=6,
                outcome="timeout",
                block="1" * 64,
                message="direct_vote",
                reporter_ns=1_110,
            ),
            accepted(
                sequence=4,
                observation_id="d" * 64,
                reporter=3,
                target=1,
                tree=0,
                outcome="timeout",
                block="2" * 64,
                message="direct_vote",
                reporter_ns=1_120,
            ),
        ]
    )
    audit = {
        "source_sequence": 99,
        "source_monotonic_ns": 2_000,
        "payload": {"baseline_cutoff": 0, "current_cutoff": 4},
    }
    return validation, contract, events, audit


def test_v4_anchor_replay_requires_exact_prefix_and_replays_outstanding_timeouts(
    tmp_path: Path,
) -> None:
    validation, contract, events, audit = _anchor_replay_fixture(tmp_path)
    rows, drawdowns, latest_ns = validation._v4_replay_fault_window_anchors(
        contract, events, baseline_cutoff=0, current_cutoff=4, audit=audit
    )
    assert {(row["reporter_id"], row["tree_id"]) for row in rows} == {(2, 6), (3, 0)}
    assert drawdowns == {"1": -2}
    assert latest_ns == 1_121


def test_v4_anchor_replay_ignores_postaudit_epoch1_ingestion_restart(
    tmp_path: Path,
) -> None:
    validation, contract, events, audit = _anchor_replay_fixture(tmp_path)
    post_epoch = json.loads(
        json.dumps(
            next(
                event
                for event in events
                if event["event_type"] == "evidence.observation_accepted"
            )
        )
    )
    post_epoch["source_sequence"] = audit["source_sequence"] + 1
    post_epoch["source_monotonic_ns"] = audit["source_monotonic_ns"] + 1
    post_epoch["payload"]["ingestion_sequence"] = 1
    post_epoch["payload"]["observation"]["configuration"] = {
        "epoch_number": 1,
        "tree_id": 0,
        "epoch_digest": "f" * 64,
    }
    events.append(post_epoch)
    rows, drawdowns, latest_ns = validation._v4_replay_fault_window_anchors(
        contract, events, baseline_cutoff=0, current_cutoff=4, audit=audit
    )
    assert {(row["reporter_id"], row["tree_id"]) for row in rows} == {
        (2, 6),
        (3, 0),
    }
    assert drawdowns == {"1": -2}
    assert latest_ns == 1_121


def test_v4_anchor_replay_rejects_postaudit_pred0_ingestion_restart(
    tmp_path: Path,
) -> None:
    validation, contract, events, audit = _anchor_replay_fixture(tmp_path)
    post_epoch = json.loads(
        json.dumps(
            next(
                event
                for event in events
                if event["event_type"] == "evidence.observation_accepted"
            )
        )
    )
    post_epoch["source_sequence"] = audit["source_sequence"] + 1
    post_epoch["source_monotonic_ns"] = audit["source_monotonic_ns"] + 1
    post_epoch["payload"]["ingestion_sequence"] = 1
    events.append(post_epoch)
    with pytest.raises(validation.FocusedCrashPairValidationError, match="audit"):
        validation._v4_replay_fault_window_anchors(
            contract, events, baseline_cutoff=0, current_cutoff=4, audit=audit
        )


def test_v4_anchor_replay_does_not_require_an_unreachable_root_anchor(
    tmp_path: Path,
) -> None:
    validation, contract, events, audit = _anchor_replay_fixture(tmp_path)
    coverage = contract["reporter_coverage_plan"]
    coverage["targets"][0]["first_qualifying_reporters"] = [
        {"reporter_id": 2, "tree_id": 6}
    ]
    events[:] = [
        event
        for event in events
        if event["event_type"] != "evidence.observation_accepted"
        or event["payload"]["observation"]["configuration"]["tree_id"] != 0
    ]
    audit["payload"]["current_cutoff"] = 3
    rows, drawdowns, latest_ns = validation._v4_replay_fault_window_anchors(
        contract, events, baseline_cutoff=0, current_cutoff=3, audit=audit
    )
    assert {(row["reporter_id"], row["tree_id"]) for row in rows} == {(2, 6)}
    assert drawdowns == {"1": -1}
    assert latest_ns == 1_111


@pytest.mark.parametrize(
    "mutation",
    ("missing_anchor", "preboundary_anchor", "nonprefix_anchor"),
)
def test_v4_anchor_replay_rejects_unanchored_or_preboundary_evidence(
    tmp_path: Path, mutation: str
) -> None:
    validation, contract, events, audit = _anchor_replay_fixture(tmp_path)
    accepted = [
        event
        for event in events
        if event["event_type"] == "evidence.observation_accepted"
    ]
    if mutation == "missing_anchor":
        events.remove(accepted[1])
    elif mutation == "preboundary_anchor":
        accepted[0]["payload"]["observation"]["reporter_monotonic_ns"] = 89
    elif mutation == "nonprefix_anchor":
        accepted[1]["payload"]["observation"]["configuration"]["tree_id"] = 1
    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation._v4_replay_fault_window_anchors(
            contract, events, baseline_cutoff=0, current_cutoff=4, audit=audit
        )


def test_v4_anchor_replay_ignores_nonanchor_for_guard_but_replays_global_score(
    tmp_path: Path,
) -> None:
    validation, contract, events, audit = _anchor_replay_fixture(tmp_path)
    timeout = next(
        event
        for event in events
        if event["event_type"] == "evidence.observation_accepted"
        and event["payload"]["observation"]["outcome"] == "timeout"
    )
    unanchored = json.loads(json.dumps(timeout))
    unanchored["source_sequence"] = 20
    unanchored["source_monotonic_ns"] = 1_130
    unanchored["payload"]["ingestion_sequence"] = 5
    unanchored["payload"]["observation"]["observation_id"] = "e" * 64
    unanchored["payload"]["observation"]["block_hash"] = "f" * 64
    events.append(unanchored)
    late = json.loads(json.dumps(unanchored))
    late["source_sequence"] = 21
    late["source_monotonic_ns"] = 1_131
    late["payload"]["ingestion_sequence"] = 6
    late["payload"]["observation"]["outcome"] = "late"
    events.append(late)
    audit["payload"]["current_cutoff"] = 6
    rows, drawdowns, _latest_ns = validation._v4_replay_fault_window_anchors(
        contract, events, baseline_cutoff=0, current_cutoff=6, audit=audit
    )
    assert {(row["reporter_id"], row["tree_id"]) for row in rows} == {
        (2, 6),
        (3, 0),
    }
    assert drawdowns == {"1": -2}


def test_v4_anchor_replay_requires_exact_late_compensation(tmp_path: Path) -> None:
    validation, contract, events, audit = _anchor_replay_fixture(tmp_path)
    timeout = next(
        event
        for event in events
        if event["event_type"] == "evidence.observation_accepted"
        and event["payload"]["observation"]["outcome"] == "timeout"
    )
    late = json.loads(json.dumps(timeout))
    late["source_sequence"] = 20
    late["source_monotonic_ns"] = 130
    late["payload"]["ingestion_sequence"] = 5
    late["payload"]["observation"]["outcome"] = "late"
    late["payload"]["observation"]["reporter_id"] = 99
    events.append(late)
    audit["payload"]["current_cutoff"] = 5
    with pytest.raises(validation.FocusedCrashPairValidationError):
        validation._v4_replay_fault_window_anchors(
            contract, events, baseline_cutoff=0, current_cutoff=5, audit=audit
        )


def test_v4_anchor_replay_uses_manager_sequence_for_equal_timestamps(
    tmp_path: Path,
) -> None:
    validation, contract, events, audit = _anchor_replay_fixture(tmp_path)
    for event in events:
        if event["event_type"] == "evidence.observation_accepted":
            event["source_monotonic_ns"] = audit["source_monotonic_ns"]
    rows, _drawdowns, _latest_ns = validation._v4_replay_fault_window_anchors(
        contract, events, baseline_cutoff=0, current_cutoff=4, audit=audit
    )
    assert len(rows) == 2


def test_v4_runtime_refuses_replacement_and_runtime_tamper(tmp_path: Path) -> None:
    runtime = _runtime()
    target = tmp_path / "runtime" / "fault-window-arm.json"
    target.parent.mkdir()
    arm = {
        "schema_version": 1,
        "kind": "kauri-focused-fault-window-arm-v1",
        "run_id": "run",
        "profile_id": "n7-f2-q5-two-crash-pair-smoke-v4",
        "profile_sha256": "a" * 64,
        "topology_proof_sha256": "b" * 64,
        "request_sha256": "c" * 64,
        "epoch_number": 0,
        "epoch_digest": "d" * 64,
        "fault_receipt_sha256": "e" * 64,
        "evidence_start_monotonic_ns": 1,
        "prefault_tree_id": 6,
        "required_tree_positions": 2,
        "required_tree_ids": [6, 0],
    }
    digest = runtime._publish_fault_window_arm(target, arm)
    assert digest == hashlib.sha256(target.read_bytes()).hexdigest()
    target.write_bytes(_canonical({**arm, "run_id": "tampered"}))
    with pytest.raises(runtime.FocusedCrashPairRuntimeError):
        runtime._publish_fault_window_arm(target, arm)


def test_v4_child_receipts_keep_their_canonical_digest_while_arm_binds_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parent approval binds the arm, while child receipts remain self-consistent."""

    runtime = _runtime()
    profile = runtime.load_focused_profile(
        PROFILE_ROOT / "n7-f2-q5-two-crash-pair-smoke-v4.json"
    )

    def generate_identities(
        *_args: object, config_directory: Path, **_kwargs: object
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        (config_directory / "bls-identities.txt").write_text(
            "synthetic BLS identities\n", encoding="utf-8"
        )
        (config_directory / "tls-identities.txt").write_text(
            "synthetic TLS identities\n", encoding="utf-8"
        )
        return (
            [{"pub": f"pub-{i}", "sec": f"sec-{i}"} for i in range(7)],
            [
                {"crt": f"crt-{i}", "sec": f"tls-{i}", "cid": f"cid-{i}"}
                for i in range(8)
            ],
        )

    monkeypatch.setattr(
        runtime,
        "_generate_arm_identities",
        generate_identities,
    )
    build = tmp_path / "build"
    build.mkdir()
    (build / runtime.profiled_fault_runtime.BUILD_PROVENANCE_FILENAME).write_bytes(
        _canonical({"schema_version": 1, "revision": "a" * 40})
    )
    parent_output = str((tmp_path / "results").resolve())
    parent_request = {
        "schema_version": 1,
        "mode": "smoke",
        "pair_count": 1,
        "profile_sha256": profile.profile_sha256,
        "topology_proof_sha256": profile.topology_proof_sha256,
        "output_root": parent_output,
        "automatic_retries": 0,
        "replacement_policy": "none",
        "authorization_nonce": hashlib.sha256(
            f"smoke:1:{parent_output}".encode("utf-8")
        ).hexdigest(),
    }
    parent_sha = hashlib.sha256(_canonical(parent_request)).hexdigest()
    parent_preflight = {
        **parent_request,
        "request_sha256": parent_sha,
        "execution_authorized": False,
        "launch_permitted": False,
    }
    parent_receipt = {
        **parent_request,
        "request_sha256": parent_sha,
        "approval_reference": "test",
        "approved_utc": "2026-08-18T00:00:00Z",
    }
    configuration = runtime.FocusedLaunchBackend().materialize_arm_configuration(
        {
            "profile": profile,
            "pair_seed": 41_720,
            "output_root": tmp_path / "results",
            "build_directory": build,
            "binaries": {
                "app": Path("/build/hotstuff-app"),
                "manager": Path("/build/adaptation-manager"),
                "client": Path("/build/hotstuff-client"),
                "keygen": Path("/build/hotstuff-keygen"),
                "tls_keygen": Path("/build/hotstuff-tls-keygen"),
            },
            "pair_issuer_allocations": {
                "pair-01": {
                    "public_key": "01",
                    "adaptive": {"public_key": "01", "private_key": "02"},
                }
            },
            "preflight_receipt": parent_preflight,
            "authorization_receipt": parent_receipt,
        },
        pair_ordinal=1,
        arm="adaptive",
    )
    child_preflight = json.loads(
        (Path(configuration["run_directory"]) / "preflight.json").read_bytes()
    )
    request = {
        key: child_preflight[key]
        for key in (
            "schema_version",
            "profile_sha256",
            "topology_proof_sha256",
            "pair_id",
            "slot_id",
            "automatic_retries",
            "replacement_policy",
        )
    }
    child_sha = hashlib.sha256(_canonical(request)).hexdigest()
    assert child_sha != parent_sha
    assert child_preflight["request_sha256"] == child_sha
    child_authorization = json.loads(
        (Path(configuration["run_directory"]) / "authorization.json").read_bytes()
    )
    assert child_authorization["request_sha256"] == child_sha
    assert configuration["parent_request_sha256"] == parent_sha
    pairs = dict(
        zip(
            configuration["manager_command"][1::2],
            configuration["manager_command"][2::2],
            strict=True,
        )
    )
    assert pairs["--fault-window-arm-request-sha256"] == parent_sha


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("n7-f2-q5-two-crash-pair-smoke-v4.json", [6, 0, 1, 2, 3, 4]),
        (
            "n31-f5-q21-three-crash-pair-v4.json",
            [20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 0, 1, 2, 3, 4],
        ),
    ],
)
def test_v4_profiles_freeze_exact_cyclic_arm_prefixes(
    name: str, expected: list[int]
) -> None:
    profile = json.loads((PROFILE_ROOT / name).read_text(encoding="utf-8"))
    assert profile["fault_window_arm"]["ordered_tree_prefix"] == expected
    assert profile["fault_window_arm"]["required_postfault_tree_positions"] == len(
        expected
    )


@pytest.mark.parametrize("version", ("v1", "v2", "v3"))
def test_archived_profiles_remain_arm_free(version: str) -> None:
    profile = json.loads(
        (PROFILE_ROOT / f"n7-f2-q5-two-crash-pair-smoke-{version}.json").read_text(
            encoding="utf-8"
        )
    )
    assert "fault_window_arm" not in profile


@pytest.mark.parametrize(
    ("reason", "expected"),
    (
        ("fault_window_arm_missing", True),
        ("fault_window_arm_invalid", True),
        ("fault_window_arm_io_failure", True),
        ("fault_window_arm_unknown", False),
        ("fault_window_arm_missing ", False),
    ),
)
def test_v4_arm_terminal_reasons_are_typed_diagnostics(
    reason: str, expected: bool
) -> None:
    """New native arm failures must remain explicit failed-run diagnostics.

    This is intentionally a red contract until native terminal vocabulary is
    wired into both independent parsers.  A generic successful terminal must
    never silently absorb an arm-read failure.
    """

    payload = {"reason": reason, "controller_failure": None}
    assert (
        _runtime()._validate_controller_failure_terminal(
            payload, require_for_unhealthy=True
        )
        is expected
    )
    assert (
        _validation()._validate_controller_failure_terminal(
            payload, require_for_unhealthy=True
        )
        is expected
    )
