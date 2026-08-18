"""Independent source-blind validation for focused N31 crash-pair evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from typing import Any

from experiments.adaptive import run_n31_crash_pair_campaign as campaign_contracts

from . import factorial_validation
from .profiled_fault_archive import EvidenceSealError, verify_evidence_seal

_PROFILE_KEYS = {
    "schema_version",
    "profile_id",
    "frozen",
    "execution_class",
    "campaign_member",
    "figure_eligible",
    "protocol",
    "topology",
    "fault",
    "matched_inputs",
    "transitions",
    "timers",
    "measurement",
    "performance",
    "thresholds",
    "ports",
    "campaign",
    "blinding",
}
_PROFILE_KEYS_V2 = _PROFILE_KEYS | {"evidence_guard"}
_FCRASH_H_V3_PROFILE_IDS = frozenset(
    {
        "n7-f2-q5-two-crash-pair-smoke-v3",
        "n31-f5-q21-three-crash-pair-v3",
    }
)
_EVENT_KEYS = {
    "event_schema_version",
    "run_id",
    "source_kind",
    "source_id",
    "source_instance",
    "source_sequence",
    "source_monotonic_ns",
    "event_type",
    "payload",
}
_TARGETS = (22, 23, 24)
_MEMBERS = tuple(range(31))
_SURVIVORS = tuple(replica for replica in _MEMBERS if replica not in _TARGETS)
_QUORUM = 21
_FANOUT = 5
_PIPELINE_STRETCH = 2
_EPOCH_ZERO_DIGEST = "145fac093343fa9cff20fcf49d85ad5443e93db14146f7854b17e28cf44f6d7a"
_NATIVE_RESPONSIVENESS_POLICY = {
    "schema_version": 1,
    "policy_version": "adaptive-v2-controller-responsiveness-v1",
    "attempt_window": 32,
    "minimum_attempts": 2,
    "minimum_response_rate_ppm": 750_000,
    "maximum_timeout_rate_ppm": 250_000,
    "trailing_timeout_streak": 2,
    "latency_percentile_basis_points": 5_000,
}
_MAIN_CONFIG_KEYS = {
    "aggregation-timeout",
    "async_blocks",
    "base-timeout",
    "block-size",
    "client-ip",
    "epoch-change-issuer-id",
    "epoch-change-issuer-public-key",
    "epoch-change-maximum-activation-delay",
    "epoch-change-maximum-ancestry-blocks",
    "epoch-change-maximum-block-extra-bytes",
    "epoch-change-minimum-activation-delay",
    "epoch-manager-address",
    "epoch-manager-tls-cert",
    "epoch-protocol-mode",
    "fan-out",
    "leader-activation-grace",
    "leader-progress-timeout",
    "max-rep-msg",
    "nworker",
    "pace-maker",
    "piped_latency",
    "prop-delay",
    "proposer",
    "replica",
    "repnworker",
    "stat-period",
    "tree-generation",
    "tree-switch-period",
}


class FocusedCrashPairValidationError(ValueError):
    """A sealed focused arm, pair, or campaign is not independently valid."""


def _error(message: str) -> None:
    raise FocusedCrashPairValidationError(message)


def _authoritative_lifecycle_instance(
    events: Sequence[Mapping[str, Any]], expected_source: str
) -> str:
    """Bind the authoritative commit source to one sealed lifecycle instance."""

    lifecycle = [
        event
        for event in events
        if event.get("event_type") in {"process.started", "process.ready"}
        and event.get("source_id") == expected_source
    ]
    if len(lifecycle) != 2 or {event.get("event_type") for event in lifecycle} != {
        "process.started",
        "process.ready",
    }:
        _error("sealed authoritative progress lacks an exact lifecycle binding")
    if any(event.get("source_kind") != "replica" for event in lifecycle):
        _error("sealed authoritative progress lifecycle kind drifted")
    instances = {event.get("source_instance") for event in lifecycle}
    if len(instances) != 1 or not isinstance(next(iter(instances)), str):
        _error("sealed authoritative progress lifecycle instance is ambiguous")
    return str(next(iter(instances)))


def _canonical(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise FocusedCrashPairValidationError("evidence is not canonical JSON") from exc


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _error(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _error(f"{label} must be a sequence")
    return value


def _integer(value: object, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _error(f"{label} must be an integer >= {minimum}")
    return value


def _uint64(value: object, label: str, minimum: int = 0) -> int:
    result = _integer(value, label, minimum)
    if result > (1 << 64) - 1:
        _error(f"{label} exceeds uint64")
    return result


def _digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        _error(f"{label} is not a lowercase SHA-256 digest")
    return value


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        _error(f"{label} is absent or not a regular file")
    try:
        return _mapping(json.loads(path.read_bytes()), label)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise FocusedCrashPairValidationError(f"{label} is invalid JSON") from exc


def _profile_identity(profile: Mapping[str, Any]) -> dict[str, Any]:
    identity = json.loads(json.dumps(dict(profile)))
    topology = identity.get("topology")
    if not isinstance(topology, dict):
        _error("profile topology is malformed")
    topology.pop("proof_sha256", None)
    return identity


def _cyclic_parent(
    replica_count: int, fanout: int, root: int, target: int
) -> int | None:
    position = (target - root) % replica_count
    if position == 0:
        return None
    return (root + (position - 1) // fanout) % replica_count


def _derive_reporter_coverage_plan(
    profile: Mapping[str, Any],
    *,
    members: tuple[int, ...],
    targets: tuple[int, ...],
    fault_threshold: int,
    fanout: int,
    active_tree: int,
) -> dict[str, object]:
    guard = _mapping(profile.get("evidence_guard"), "profile evidence guard")
    timers = _mapping(profile.get("timers"), "profile timers")
    expected_guard_keys = {
        "schedule",
        "tree_switch_period_blocks",
        "horizon_tree_positions",
        "required_qualifying_reporters",
        "minimum_timeouts_per_reporter",
        "minimum_score_drop",
    }
    if profile["profile_id"] in _FCRASH_H_V3_PROFILE_IDS:
        expected_guard_keys.add("required_postfault_tree_positions")
    expected_timer_keys = {
        "adaptation_interval_seconds",
        "stable_phase_seconds",
        "readiness_timeout_seconds",
        "manager_convergence_timeout_seconds",
        "nonresponse_evidence_deadline_seconds",
        "containment_activation_deadline_seconds",
        "optimization_activation_deadline_seconds",
        "arm_hard_deadline_seconds",
    }
    if set(guard) != expected_guard_keys or set(timers) != expected_timer_keys:
        _error("FCRASH-H guard or timer schema drifted")
    required = fault_threshold + 1
    target_rows: list[dict[str, object]] = []
    reporter_sets: list[set[int]] = []
    horizon = 0
    count = len(members)
    for target in targets:
        seen: set[int] = set()
        first: list[dict[str, int]] = []
        for offset in range(count):
            tree_id = (active_tree + offset) % count
            if tree_id in targets:
                continue
            reporter = _cyclic_parent(count, fanout, tree_id, target)
            if reporter is None or reporter in targets or reporter in seen:
                continue
            seen.add(reporter)
            first.append(
                {
                    "tree_position": offset + 1,
                    "tree_id": tree_id,
                    "reporter_id": reporter,
                }
            )
            if len(first) == required:
                break
        if len(first) != required:
            _error("FCRASH-H has insufficient honest reporter coverage")
        horizon = max(horizon, first[-1]["tree_position"])
        reporter_sets.append({row["reporter_id"] for row in first})
        target_rows.append(
            {
                "target_replica_id": target,
                "authenticated_reporter_ids": [],
                "first_qualifying_reporters": first,
            }
        )
    common = set.intersection(*reporter_sets)
    if len(common) != required:
        _error("FCRASH-H targets lack one common honest reporter set")
    common_ids = sorted(common)
    for row in target_rows:
        row["authenticated_reporter_ids"] = common_ids
    expected_period = 2 if profile["profile_id"] in _FCRASH_H_V3_PROFILE_IDS else count
    expected_guard = {
        "schedule": "native_cyclic_epoch_zero",
        "tree_switch_period_blocks": expected_period,
        "horizon_tree_positions": horizon,
        "required_qualifying_reporters": required,
        "minimum_timeouts_per_reporter": 2,
        "minimum_score_drop": 2 * required,
    }
    if profile["profile_id"] in _FCRASH_H_V3_PROFILE_IDS:
        expected_guard["required_postfault_tree_positions"] = horizon
    if dict(guard) != expected_guard:
        _error("FCRASH-H frozen evidence guard differs from topology derivation")
    deadlines = {
        "evidence_seconds": _integer(
            timers.get("nonresponse_evidence_deadline_seconds"),
            "nonresponse evidence deadline",
            1,
        ),
        "epoch1_activation_seconds": _integer(
            timers.get("containment_activation_deadline_seconds"),
            "containment activation deadline",
            1,
        ),
        "optimization_activation_seconds": _integer(
            timers.get("optimization_activation_deadline_seconds"),
            "optimization activation deadline",
            1,
        ),
        "arm_hard_seconds": _integer(
            timers.get("arm_hard_deadline_seconds"), "arm hard deadline", 1
        ),
    }
    if (
        deadlines["evidence_seconds"] >= deadlines["epoch1_activation_seconds"]
        or deadlines["epoch1_activation_seconds"] >= deadlines["arm_hard_seconds"]
        or deadlines["optimization_activation_seconds"] >= deadlines["arm_hard_seconds"]
    ):
        _error("FCRASH-H phase deadlines are not strictly nested")
    return {
        "schema_version": 1,
        "profile_id": profile["profile_id"],
        "active_tree_id": active_tree,
        "horizon_tree_positions": horizon,
        "required_qualifying_reporters": required,
        "minimum_timeouts_per_reporter": 2,
        "minimum_score_drop": 2 * required,
        **(
            {
                "required_postfault_tree_positions": horizon,
                "nominal_commit_horizon": horizon * expected_period,
            }
            if profile["profile_id"] in _FCRASH_H_V3_PROFILE_IDS
            else {}
        ),
        "deadlines_seconds": deadlines,
        "stable_phase_seconds": _integer(
            timers.get("stable_phase_seconds"), "stable phase", 1
        ),
        "readiness_timeout_seconds": _integer(
            timers.get("readiness_timeout_seconds"), "readiness timeout", 1
        ),
        "manager_convergence_timeout_seconds": _integer(
            timers.get("manager_convergence_timeout_seconds"),
            "manager convergence timeout",
            1,
        ),
        "targets": target_rows,
    }


def _inside_deadline(origin_ns: int, candidate_ns: int, seconds: int) -> bool:
    return origin_ns <= candidate_ns < origin_ns + seconds * 1_000_000_000


def validate_fcrash_h_evidence(
    contract: Mapping[str, object], witness: Mapping[str, object]
) -> None:
    """Validate one independently reconstructed FCRASH-H causal witness."""

    coverage = _mapping(
        contract.get("reporter_coverage_plan"), "reporter coverage plan"
    )
    expected_keys = {
        "fault_monotonic_ns",
        "nonresponse_monotonic_ns",
        "snapshot_audit_monotonic_ns",
        "epoch1_activation_monotonic_ns",
        "epoch2_activation_monotonic_ns",
        "timeout_observations",
        "guard_drawdowns",
    }
    required_progress = coverage.get("required_postfault_tree_positions")
    if required_progress is not None:
        expected_keys.add("postfault_progress")
    if set(witness) != expected_keys:
        _error("FCRASH-H witness schema drifted")
    fault_ns = _integer(witness.get("fault_monotonic_ns"), "fault timestamp")
    nonresponse_ns = _integer(
        witness.get("nonresponse_monotonic_ns"), "nonresponse timestamp"
    )
    snapshot_audit_ns = _integer(
        witness.get("snapshot_audit_monotonic_ns"), "snapshot audit timestamp"
    )
    epoch1_ns = _integer(
        witness.get("epoch1_activation_monotonic_ns"), "Epoch 1 activation"
    )
    epoch2_raw = witness.get("epoch2_activation_monotonic_ns")
    epoch2_ns = (
        None if epoch2_raw is None else _integer(epoch2_raw, "Epoch 2 activation")
    )
    deadlines = _mapping(coverage.get("deadlines_seconds"), "coverage deadlines")
    if (
        not _inside_deadline(
            fault_ns,
            snapshot_audit_ns,
            _integer(deadlines.get("evidence_seconds"), "evidence deadline", 1),
        )
        or not _inside_deadline(
            fault_ns,
            epoch1_ns,
            _integer(
                deadlines.get("epoch1_activation_seconds"),
                "Epoch 1 activation deadline",
                1,
            ),
        )
        or nonresponse_ns >= snapshot_audit_ns
        or snapshot_audit_ns >= epoch1_ns
        or (
            epoch2_ns is not None
            and (
                epoch2_ns <= epoch1_ns
                or not _inside_deadline(
                    epoch1_ns,
                    epoch2_ns,
                    _integer(
                        deadlines.get("optimization_activation_seconds"),
                        "Epoch 2 activation deadline",
                        1,
                    ),
                )
            )
        )
    ):
        _error("FCRASH-H causal timestamp or deadline drifted")
    expected = {
        int(row["target_replica_id"]): {
            _integer(reporter["reporter_id"], "coverage reporter"): _integer(
                reporter["tree_id"], "coverage first qualifying tree"
            )
            for reporter in _sequence(
                row["first_qualifying_reporters"], "first qualifying reporters"
            )
        }
        for row in _sequence(coverage.get("targets"), "coverage targets")
    }
    expected_trees = {
        (
            int(row["target_replica_id"]),
            int(first["reporter_id"]),
        ): int(first["tree_id"])
        for row in _sequence(coverage.get("targets"), "coverage targets")
        for first in _sequence(
            _mapping(row, "coverage target").get("first_qualifying_reporters"),
            "first qualifying reporters",
        )
    }
    counts = {
        target: {reporter: 0 for reporter in reporters}
        for target, reporters in expected.items()
    }
    for raw in _sequence(witness.get("timeout_observations"), "timeout observations"):
        observation = _mapping(raw, "timeout observation")
        if set(observation) != {
            "epoch_number",
            "tree_id",
            "observed_replica_id",
            "reporter_id",
            "outcome",
            "compensated",
            "source_monotonic_ns",
        }:
            _error("timeout observation schema drifted")
        target = _integer(observation.get("observed_replica_id"), "timeout target")
        reporter = _integer(observation.get("reporter_id"), "timeout reporter")
        timestamp = _integer(
            observation.get("source_monotonic_ns"), "timeout timestamp"
        )
        if (
            observation.get("epoch_number") != 0
            or observation.get("tree_id") != expected_trees.get((target, reporter))
            or observation.get("outcome") != "timeout"
            or observation.get("compensated") is not False
            or target not in counts
            or reporter not in counts[target]
            or not (fault_ns < timestamp <= nonresponse_ns)
        ):
            _error("timeout observation is not exact post-fault evidence")
        counts[target][reporter] += 1
    minimum = _integer(
        coverage.get("minimum_timeouts_per_reporter"),
        "minimum timeouts per reporter",
        1,
    )
    if any(
        count < minimum for reporters in counts.values() for count in reporters.values()
    ):
        _error("FCRASH-H reporter timeout coverage is incomplete")
    drawdowns = _mapping(witness.get("guard_drawdowns"), "guard drawdowns")
    minimum_drop = _integer(coverage.get("minimum_score_drop"), "minimum score drop", 1)
    if set(drawdowns) != {str(target) for target in expected} or any(
        type(drawdowns[str(target)]) is not int
        or int(drawdowns[str(target)]) > -minimum_drop
        for target in expected
    ):
        _error("FCRASH-H score drawdown is incomplete")
    if required_progress is not None:
        progress = _mapping(witness.get("postfault_progress"), "post-fault progress")
        required_count = _integer(
            required_progress, "required post-fault commit horizon", 1
        )
        if set(progress) != {
            "required_tree_positions",
            "actual_tree_positions",
            "starting_tree_id",
            "observed_tree_ids",
        } or (
            _integer(
                progress.get("required_tree_positions"), "progress required count", 1
            )
            != required_count
            or _integer(
                progress.get("actual_tree_positions"), "progress actual count", 1
            )
            < required_count
            or not isinstance(progress.get("observed_tree_ids"), list)
            or len(progress["observed_tree_ids"])
            != _integer(
                progress.get("actual_tree_positions"), "progress actual count", 1
            )
        ):
            _error("FCRASH-H post-fault authoritative progress is incomplete")


def _fcrash_h_postfault_progress(
    contract: Mapping[str, object],
    events: Sequence[Mapping[str, Any]],
    *,
    fault_ns: int,
    prefault_ns: int,
    audit_ns: int,
) -> dict[str, object]:
    """Independently reconstruct the v3 progress witness from sealed raw events."""

    coverage = _mapping(
        contract.get("reporter_coverage_plan"), "reporter coverage plan"
    )
    required_positions = _integer(
        coverage.get("required_postfault_tree_positions"),
        "required post-fault tree positions",
        1,
    )
    observer = _integer(
        contract.get("authoritative_replica_id"), "authoritative observer", 0
    )
    expected_source = f"replica-{observer}"
    expected_instance = _authoritative_lifecycle_instance(events, expected_source)
    expected_digest = str(contract["epoch_zero_digest"])
    transactions_per_block = _integer(
        contract.get("transactions_per_block"), "transactions per block", 1
    )
    members = tuple(int(member) for member in contract["members"])
    member_sources = {f"replica-{member}" for member in members}
    if any(
        event["source_kind"] == "replica"
        and event["source_id"] in member_sources
        and event["event_type"] == "adaptive.configuration_active"
        and prefault_ns
        <= _integer(event["source_monotonic_ns"], "configuration timestamp")
        <= fault_ns
        for event in events
    ):
        _error("configuration changed during the atomic fault batch")
    start_events = [
        event
        for event in events
        if event["source_kind"] == "replica"
        and event["source_id"] == expected_source
        and event["source_instance"] == expected_instance
        and event["event_type"] == "adaptive.configuration_active"
        and _integer(event["source_monotonic_ns"], "configuration timestamp")
        < prefault_ns
    ]
    if not start_events:
        _error("sealed authoritative progress lacks a pre-fault configuration")
    historical_configurations = sorted(
        start_events,
        key=lambda event: (
            _integer(event["source_sequence"], "configuration sequence", 1),
            _integer(event["source_monotonic_ns"], "configuration timestamp"),
        ),
    )
    for position, event in enumerate(historical_configurations):
        payload = _mapping(event["payload"], "pre-fault configuration")
        epoch = _integer(payload.get("epoch_number"), "configuration epoch")
        tree = _integer(payload.get("tree_id"), "configuration tree")
        if (
            epoch != 0
            or payload.get("epoch_digest") != expected_digest
            or tree != members[position % len(members)]
        ):
            _error("sealed authoritative progress historical configuration drifted")
    start = max(
        start_events,
        key=lambda event: (
            _integer(event["source_sequence"], "configuration sequence", 1),
            _integer(event["source_monotonic_ns"], "configuration timestamp"),
        ),
    )
    start_payload = _mapping(start["payload"], "pre-fault configuration")
    starting_tree = _integer(start_payload.get("tree_id"), "starting tree")
    if (
        _integer(start_payload.get("epoch_number"), "starting epoch") != 0
        or start_payload.get("epoch_digest") != expected_digest
        or starting_tree != int(coverage["active_tree_id"])
    ):
        _error("sealed authoritative progress pre-fault configuration drifted")
    activations = sorted(
        [
            event
            for event in events
            if event["source_kind"] == "replica"
            and event["source_id"] == expected_source
            and event["source_instance"] == expected_instance
            and event["event_type"] == "adaptive.configuration_active"
            and fault_ns
            < _integer(event["source_monotonic_ns"], "configuration timestamp")
            < audit_ns
        ],
        key=lambda event: (
            _integer(event["source_sequence"], "configuration sequence", 1),
            _integer(event["source_monotonic_ns"], "configuration timestamp"),
        ),
    )
    # The frozen horizon starts with the configuration already active at the
    # fault boundary; only H-1 later activations are required.
    observed_trees: list[int] = [starting_tree]
    for position, event in enumerate(activations, start=1):
        payload = _mapping(event["payload"], "post-fault configuration")
        tree = _integer(payload.get("tree_id"), "activated tree")
        if (
            _integer(payload.get("epoch_number"), "activated epoch") != 0
            or payload.get("epoch_digest") != expected_digest
            or tree != members[(members.index(starting_tree) + position) % len(members)]
        ):
            _error("sealed authoritative progress cyclic configuration drifted")
        observed_trees.append(tree)
    if len(observed_trees) < required_positions:
        _error("FCRASH-H post-fault tree positions are incomplete")
    configurations = [
        (
            _integer(event["source_sequence"], "configuration sequence", 1),
            _integer(event["source_monotonic_ns"], "configuration timestamp"),
            _integer(
                _mapping(event["payload"], "pre-fault configuration").get("tree_id"),
                "configuration tree",
            ),
        )
        for event in historical_configurations
    ] + [
        (
            _integer(event["source_sequence"], "configuration sequence", 1),
            _integer(event["source_monotonic_ns"], "configuration timestamp"),
            tree,
        )
        for event, tree in zip(activations, observed_trees[1:], strict=True)
    ]
    for event in events:
        if event["event_type"] != "block.committed":
            continue
        if (
            event["source_kind"] != "replica"
            or event["source_id"] != expected_source
            or event["source_instance"] != expected_instance
        ):
            continue
        timestamp = _integer(event["source_monotonic_ns"], "progress commit timestamp")
        if not fault_ns < timestamp < audit_ns:
            continue
        payload = _mapping(event["payload"], "authoritative progress commit")
        proof = _mapping(payload.get("decision_proof"), "progress decision proof")
        if set(payload) != {
            "block_height",
            "block_hash",
            "parent_hash",
            "transaction_count",
            "commit_batch_index",
            "designated_observer",
            "decision_proof",
            "view_generation",
        }:
            _error("sealed authoritative progress commit schema drifted")
        if set(proof) != {
            "epoch_number",
            "tree_id",
            "epoch_digest",
            "block_hash",
        }:
            _error("sealed authoritative progress proof schema drifted")
        block_hash = _digest(payload.get("block_hash"), "progress commit hash")
        proof_tree = _integer(proof.get("tree_id"), "progress proof tree")
        proof_epoch = _integer(proof.get("epoch_number"), "progress proof epoch")
        _uint64(payload.get("commit_batch_index"), "progress commit batch index")
        view_generation = _uint64(
            payload.get("view_generation"), "progress view generation", 1
        )
        if (
            payload.get("designated_observer") is not True
            or _uint64(payload.get("transaction_count"), "progress transactions")
            not in {0, transactions_per_block}
            or proof.get("block_hash") != block_hash
            or proof_epoch != 0
            or proof.get("epoch_digest") != expected_digest
        ):
            _error("sealed authoritative progress commit invariants drifted")
        commit_key = (
            _integer(event["source_sequence"], "progress source sequence", 1),
            timestamp,
        )
        if view_generation > len(configurations):
            _error("sealed authoritative progress commit generation is not activated")
        generation_configuration = configurations[view_generation - 1]
        if (
            generation_configuration[:2] > commit_key
            or generation_configuration[2] != proof_tree
        ):
            _error("sealed authoritative progress commit is not causally activated")
    return {
        "required_tree_positions": required_positions,
        "actual_tree_positions": len(observed_trees),
        "starting_tree_id": starting_tree,
        "observed_tree_ids": observed_trees,
    }


def _fcrash_h_witness_from_events(
    contract: Mapping[str, object],
    events: Sequence[Mapping[str, Any]],
    fault_receipt: Mapping[str, Any],
    activations1: Sequence[Mapping[str, Any]],
    activations2: Sequence[Mapping[str, Any]],
) -> dict[str, object]:
    coverage = _mapping(
        contract.get("reporter_coverage_plan"), "reporter coverage plan"
    )
    confirmations = [
        _integer(
            _mapping(outcome, "SIGKILL outcome").get("confirmed_monotonic_ns"),
            "fault confirmation",
        )
        for outcome in _sequence(
            fault_receipt.get("sigkill_outcomes"), "SIGKILL outcomes"
        )
    ]
    fault_ns = max(confirmations)
    prefault_ns = min(
        _integer(
            _mapping(outcome, "SIGKILL outcome").get("requested_monotonic_ns"),
            "fault request",
        )
        for outcome in _sequence(
            fault_receipt.get("sigkill_outcomes"), "SIGKILL outcomes"
        )
    )
    audits = [
        event
        for event in events
        if event["source_kind"] == "adaptation_manager"
        and event["source_id"] == "adaptive-manager"
        and event["event_type"] == "adaptive_v2_evidence_snapshot"
        and _mapping(event["payload"], "snapshot audit").get("predecessor_epoch_number")
        == 0
    ]
    if len(audits) != 1:
        _error("FCRASH-H lacks one predecessor-0 native snapshot audit")
    cutoff = _integer(
        _mapping(audits[0]["payload"], "snapshot audit").get("current_cutoff"),
        "snapshot current cutoff",
        1,
    )
    baseline_cutoff = _integer(
        _mapping(audits[0]["payload"], "snapshot audit").get("baseline_cutoff"),
        "snapshot baseline cutoff",
    )
    accepted: list[tuple[int, Mapping[str, Any]]] = []
    latest: dict[str, tuple[int, Mapping[str, Any], int, int]] = {}
    for event in events:
        if (
            event["source_kind"] != "adaptation_manager"
            or event["source_id"] != "adaptive-manager"
            or event["event_type"] != "evidence.observation_accepted"
        ):
            continue
        payload = _mapping(event["payload"], "accepted evidence")
        sequence = _integer(
            payload.get("ingestion_sequence"), "evidence ingestion sequence", 1
        )
        if sequence <= baseline_cutoff or sequence > cutoff:
            continue
        observation = _mapping(payload.get("observation"), "accepted observation")
        configuration = _mapping(
            observation.get("configuration"), "observation configuration"
        )
        if (
            configuration.get("epoch_number") != 0
            or configuration.get("epoch_digest") != contract["epoch_zero_digest"]
        ):
            continue
        observation_id = observation.get("observation_id")
        if not isinstance(observation_id, str):
            _error("accepted observation ID is malformed")
        previous = latest.get(observation_id)
        if previous is not None and sequence <= previous[0]:
            _error("accepted observation transition regressed")
        latest[observation_id] = (
            sequence,
            observation,
            _integer(event["source_monotonic_ns"], "evidence acceptance time"),
            _integer(
                observation.get("reporter_monotonic_ns"),
                "evidence reporter time",
            ),
        )
        accepted.append((sequence, observation))
    expected = {
        int(row["target_replica_id"]): {
            _integer(reporter["reporter_id"], "coverage reporter"): _integer(
                reporter["tree_id"], "coverage first qualifying tree"
            )
            for reporter in _sequence(
                row["first_qualifying_reporters"], "first qualifying reporters"
            )
        }
        for row in _sequence(coverage.get("targets"), "coverage targets")
    }
    rows: list[dict[str, object]] = []
    drawdowns = {target: 0 for target in expected}
    outstanding: dict[str, tuple[int, int]] = {}
    for _sequence_number, observation in sorted(accepted, key=lambda row: row[0]):
        observation_id = str(observation["observation_id"])
        reporter = int(observation["reporter_id"])
        target = int(observation["observed_replica_id"])
        outcome = str(observation["outcome"])
        if outcome == "timeout":
            if observation_id in outstanding:
                _error("accepted timeout attempt is duplicated")
            outstanding[observation_id] = (reporter, target)
            if target in drawdowns:
                drawdowns[target] -= 1
        elif outcome == "on_time":
            if target in drawdowns and drawdowns[target] < 0:
                drawdowns[target] += 1
        elif outcome == "late":
            previous = outstanding.pop(observation_id, None)
            if previous is not None:
                if previous != (reporter, target):
                    _error("accepted late evidence changed its attempt identity")
                if target in drawdowns and drawdowns[target] < 0:
                    drawdowns[target] += 1
    for (
        _ingestion_sequence,
        observation,
        accepted_ns,
        reporter_ns,
    ) in latest.values():
        target = observation.get("observed_replica_id")
        reporter = observation.get("reporter_id")
        if (
            observation.get("outcome") != "timeout"
            or type(target) is not int
            or type(reporter) is not int
            or target not in expected
            or reporter not in expected[target]
            or _integer(
                _mapping(
                    observation.get("configuration"), "observation configuration"
                ).get("tree_id"),
                "observation tree",
            )
            != expected[target][reporter]
        ):
            continue
        rows.append(
            {
                "epoch_number": 0,
                "tree_id": _integer(
                    _mapping(
                        observation.get("configuration"),
                        "observation configuration",
                    ).get("tree_id"),
                    "observation tree",
                ),
                "observed_replica_id": target,
                "reporter_id": reporter,
                "outcome": "timeout",
                "compensated": False,
                "source_monotonic_ns": max(accepted_ns, reporter_ns),
            }
        )
    if not rows:
        _error("FCRASH-H contains no qualifying timeout evidence")
    witness = {
        "fault_monotonic_ns": fault_ns,
        "nonresponse_monotonic_ns": max(
            int(row["source_monotonic_ns"]) for row in rows
        ),
        "snapshot_audit_monotonic_ns": _integer(
            audits[0]["source_monotonic_ns"], "snapshot audit timestamp"
        ),
        "epoch1_activation_monotonic_ns": max(
            _integer(event["source_monotonic_ns"], "Epoch 1 activation time")
            for event in activations1
        ),
        "epoch2_activation_monotonic_ns": (
            None
            if not activations2
            else max(
                _integer(event["source_monotonic_ns"], "Epoch 2 activation time")
                for event in activations2
            )
        ),
        "timeout_observations": rows,
        "guard_drawdowns": {
            str(target): drawdown for target, drawdown in sorted(drawdowns.items())
        },
    }
    if coverage.get("required_postfault_tree_positions") is not None:
        witness["postfault_progress"] = _fcrash_h_postfault_progress(
            contract,
            events,
            fault_ns=fault_ns,
            prefault_ns=prefault_ns,
            audit_ns=_integer(
                audits[0]["source_monotonic_ns"], "snapshot audit timestamp"
            ),
        )
    return witness


def _validate_prefault_active_configuration(
    contract: Mapping[str, object],
    events: Sequence[Mapping[str, Any]],
    fault_ns: int,
) -> None:
    latest: dict[int, Mapping[str, Any]] = {}
    for event in events:
        source_id = str(event["source_id"])
        if (
            event["source_kind"] != "replica"
            or not source_id.startswith("replica-")
            or event["event_type"] != "adaptive.configuration_active"
            or int(event["source_monotonic_ns"]) >= fault_ns
        ):
            continue
        replica = int(source_id.removeprefix("replica-"))
        previous = latest.get(replica)
        if previous is None or (
            _integer(event["source_sequence"], "configuration sequence", 1),
            _integer(event["source_monotonic_ns"], "configuration timestamp"),
        ) > (
            _integer(previous["source_sequence"], "configuration sequence", 1),
            _integer(previous["source_monotonic_ns"], "configuration timestamp"),
        ):
            latest[replica] = event
    members = tuple(int(member) for member in contract["members"])
    if set(latest) != set(members):
        _error("FCRASH-H pre-fault active configuration lacks every member")
    for replica in members:
        payload = _mapping(latest[replica]["payload"], "active configuration")
        if {
            key: payload.get(key) for key in ("epoch_number", "tree_id", "epoch_digest")
        } != {
            "epoch_number": 0,
            "tree_id": _mapping(
                contract["reporter_coverage_plan"], "reporter coverage plan"
            )["active_tree_id"],
            "epoch_digest": contract["epoch_zero_digest"],
        }:
            _error("FCRASH-H pre-fault active configuration drifted")


def validation_contract_from_profile(root: Path) -> dict[str, object]:
    """Derive the independent N7/N31 contract from sealed frozen inputs."""

    profile = _read_json(root / "profile.json", "focused profile")
    schema_version = profile.get("schema_version")
    expected_keys = _PROFILE_KEYS_V2 if schema_version == 2 else _PROFILE_KEYS
    if (
        set(profile) != expected_keys
        or schema_version not in {1, 2}
        or profile.get("frozen") is not True
    ):
        _error("focused profile schema or identity drifted")
    profile_id = profile.get("profile_id")
    if profile_id not in {
        "n7-f2-q5-two-crash-pair-smoke-v1",
        "n31-f5-q21-three-crash-pair-v1",
        "n7-f2-q5-two-crash-pair-smoke-v2",
        "n31-f5-q21-three-crash-pair-v2",
        "n7-f2-q5-two-crash-pair-smoke-v3",
        "n31-f5-q21-three-crash-pair-v3",
    }:
        _error("focused profile identity is not reviewed")
    protocol = _mapping(profile.get("protocol"), "profile protocol")
    count = _integer(protocol.get("N"), "profile replica count", 1)
    threshold = _integer(protocol.get("f"), "profile fault threshold")
    quorum = _integer(protocol.get("Q"), "profile quorum", 1)
    fanout = _integer(protocol.get("fanout"), "profile fanout", 1)
    pipeline = _integer(protocol.get("pipeline_stretch"), "profile pipeline stretch", 1)
    if count != 3 * threshold + 1 or quorum != 2 * threshold + 1:
        _error("focused profile protocol identity drifted")
    members = tuple(range(count))
    topology = _mapping(profile.get("topology"), "profile topology")
    targets = tuple(
        _integer(value, "profile target")
        for value in _sequence(
            topology.get("reviewed_target_replica_ids"), "profile targets"
        )
    )
    if (
        not targets
        or len(set(targets)) != len(targets)
        or not set(targets).issubset(members)
    ):
        _error("focused profile topology identity drifted")
    epoch_zero_digest = _digest(
        topology.get("epoch_zero_digest"), "profile epoch-zero digest"
    )
    active_tree = _integer(topology.get("active_tree_id"), "active tree")
    if active_tree not in members:
        _error("active tree is outside membership")
    measurement = _mapping(profile.get("measurement"), "profile measurement")
    observer = _integer(
        measurement.get("authoritative_replica_id"), "authoritative observer"
    )
    if observer not in members or observer in targets:
        _error("authoritative observer must be a survivor")
    fault = _mapping(profile.get("fault"), "profile fault")
    transitions = _mapping(profile.get("transitions"), "profile transitions")
    if (
        fault.get("target_count") != len(targets)
        or transitions.get("common_commit_quorum") != quorum
        or transitions.get("survivor_barrier_count") != count - len(targets)
    ):
        _error("profile fault or transition cardinality drifted")
    relative = topology.get("proof_path")
    if (
        not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
    ):
        _error("topology proof path is unsafe")
    proof_path = root / relative
    if proof_path.is_symlink() or not proof_path.is_file():
        _error("topology proof is absent")
    proof_bytes = proof_path.read_bytes()
    proof_sha = _digest(topology.get("proof_sha256"), "topology proof digest")
    if _sha_bytes(proof_bytes) != proof_sha:
        _error("topology proof bytes drifted")
    profile_sha = _hash(_profile_identity(profile))
    proof = _mapping(json.loads(proof_bytes), "topology proof")
    proof_keys = {
        "schema_version",
        "source",
        "profile_id",
        "profile_sha256",
        "epoch_zero_digest",
        "active_tree_id",
        "fanout",
        "root_replica_id",
        "bfs_member_order",
        "members",
        "internal_descendant_sets",
        "target_derivation",
    }
    order = [members[(active_tree + offset) % count] for offset in range(count)]
    if (
        proof.get("source") != "native_epoch_profile_digest"
        or proof.get("profile_sha256") != profile_sha
        or proof.get("epoch_zero_digest") != epoch_zero_digest
        or proof.get("active_tree_id") != active_tree
        or proof.get("fanout") != fanout
        or set(proof) != proof_keys
        or proof.get("schema_version") != 1
        or proof.get("profile_id") != profile.get("profile_id")
        or proof.get("root_replica_id") != active_tree
        or proof.get("bfs_member_order") != order
        or _mapping(proof.get("target_derivation"), "target derivation").get(
            "selected_target_replica_ids"
        )
        != list(targets)
    ):
        _error("topology proof is not bound to the native focused tree")
    children = {
        index: tuple(
            child
            for child in range(index * fanout + 1, index * fanout + fanout + 1)
            if child < count
        )
        for index in members
    }

    def subtree(index: int) -> tuple[int, ...]:
        return tuple(
            member
            for child in children[index]
            for member in (order[child], *subtree(child))
        )

    depths = [0] * count
    for index in range(1, count):
        depths[index] = depths[(index - 1) // fanout] + 1
    expected_members = [
        {
            "replica_id": replica,
            "bfs_index": index,
            "depth": depths[index],
            "role": (
                "root" if index == 0 else "internal" if children[index] else "leaf"
            ),
        }
        for index, replica in enumerate(order)
    ]
    expected_descendants = {
        str(order[index]): list(subtree(index))
        for index, child_ids in children.items()
        if child_ids
    }
    nonroot_internal = [
        index for index, child_ids in children.items() if index and child_ids
    ]
    deepest_depth = max(depths[index] for index in nonroot_internal)
    deepest = [
        order[index] for index in nonroot_internal if depths[index] == deepest_depth
    ]
    target_descendants = [set(expected_descendants[str(target)]) for target in targets]
    disjoint = all(
        left.isdisjoint(right)
        for position, left in enumerate(target_descendants)
        for right in target_descendants[position + 1 :]
    )
    if (
        proof.get("members") != expected_members
        or proof.get("internal_descendant_sets") != expected_descendants
        or proof.get("target_derivation")
        != {
            "deepest_member_ids": deepest,
            "selected_target_replica_ids": list(targets),
            "pairwise_disjoint": True,
        }
        or not disjoint
    ):
        _error("topology proof roles, depths, or descendants drifted")
    survivors = tuple(member for member in members if member not in targets)
    result: dict[str, object] = {
        "profile": profile,
        "profile_sha256": profile_sha,
        "topology_proof_sha256": proof_sha,
        "profile_id": profile_id,
        "members": members,
        "quorum": quorum,
        "fault_threshold": threshold,
        "fanout": fanout,
        "pipeline_stretch": pipeline,
        "targets": targets,
        "fault_target_count": len(targets),
        "manager_blinding_target_count": len(targets),
        "survivors": survivors,
        "control_transition_count": len(
            _sequence(transitions.get("control"), "control transitions")
        ),
        "adaptive_transition_count": len(
            _sequence(transitions.get("adaptive"), "adaptive transitions")
        ),
        "authoritative_replica_id": observer,
        "authoritative_source_id": f"replica-{observer}",
        "epoch_zero_digest": epoch_zero_digest,
        "phase_names": tuple(
            _sequence(measurement.get("phase_names"), "measurement phases")
        ),
        "bucket_width_seconds": _integer(
            measurement.get("bucket_width_seconds"), "bucket width", 1
        ),
        "transactions_per_block": _integer(
            protocol.get("transactions_per_block"), "transactions per block", 1
        ),
        "figure_eligible": profile.get("figure_eligible") is True,
    }
    if schema_version == 2:
        result["reporter_coverage_plan"] = _derive_reporter_coverage_plan(
            profile,
            members=members,
            targets=targets,
            fault_threshold=threshold,
            fanout=fanout,
            active_tree=active_tree,
        )
    return result


def _expected_treegen_payload(contract: Mapping[str, object]) -> bytes:
    members = tuple(int(member) for member in contract["members"])
    fanout = int(contract["fanout"])
    pipeline = int(contract["pipeline_stretch"])
    lines = [
        " ".join(
            (
                f"fan:{fanout}",
                f"pipe:{pipeline}",
                *(str(replica) for replica in members[offset:] + members[:offset]),
            )
        )
        for offset in range(len(members))
    ]
    return ("\n".join(lines) + "\n").encode("ascii")


def _validate_runtime_configuration(root: Path, contract: Mapping[str, object]) -> None:
    treegen_path = root / "treegen.conf"
    if treegen_path.is_symlink() or not treegen_path.is_file():
        _error("client tree configuration is absent")
    if treegen_path.read_bytes() != _expected_treegen_payload(contract):
        _error("client tree configuration differs from the frozen topology")

    main_path = root / "config" / "main.conf"
    if main_path.is_symlink() or not main_path.is_file():
        _error("main runtime configuration is absent")
    try:
        payload = main_path.read_bytes().decode("ascii")
    except UnicodeDecodeError as exc:
        raise FocusedCrashPairValidationError(
            "main runtime configuration is not canonical ASCII"
        ) from exc
    if not payload.endswith("\n"):
        _error("main runtime configuration is not newline terminated")
    options: dict[str, list[str]] = {}
    for line in payload.splitlines():
        if " = " not in line:
            _error("main runtime configuration contains a malformed line")
        key, value = line.split(" = ", 1)
        normalized_key = key.strip()
        normalized_value = value.strip()
        if (
            not normalized_key
            or not normalized_value
            or line != f"{normalized_key} = {normalized_value}"
        ):
            _error("main runtime configuration is not canonical")
        key, value = normalized_key, normalized_value
        options.setdefault(key, []).append(value)
    if set(options) != _MAIN_CONFIG_KEYS:
        _error("main runtime configuration key set drifted")
    if any(key != "replica" and len(values) != 1 for key, values in options.items()):
        _error("main runtime configuration duplicates a singleton option")
    required = {
        "block-size": str(contract["transactions_per_block"]),
        "fan-out": str(contract["fanout"]),
        "async_blocks": str(contract["pipeline_stretch"]),
        "aggregation-timeout": "1.0",
        "leader-progress-timeout": "8.0",
        "leader-activation-grace": "1.0",
        "tree-generation": "default",
        "tree-switch-period": str(
            2
            if _mapping(contract.get("profile"), "focused profile").get("profile_id")
            in _FCRASH_H_V3_PROFILE_IDS
            else len(tuple(contract["members"]))
        ),
        "epoch-protocol-mode": "adaptive_v2",
        "epoch-change-minimum-activation-delay": "5",
        "epoch-change-maximum-activation-delay": "5",
    }
    if any(options.get(key) != [value] for key, value in required.items()):
        _error("main runtime topology or timer configuration drifted")
    if {"conf", "default_epoch"}.intersection(options):
        _error("main runtime overrides the sealed client tree configuration")
    if len(options.get("replica", ())) != len(tuple(contract["members"])):
        _error("main runtime replica membership cardinality drifted")


def _validated_profile(root: Path) -> tuple[Mapping[str, Any], str, str]:
    contract = validation_contract_from_profile(root)
    return (
        _mapping(contract["profile"], "focused profile"),
        str(contract["profile_sha256"]),
        str(contract["topology_proof_sha256"]),
    )


def _read_jsonl(path: Path, source_kind: str) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        _error(f"{source_kind} event stream is absent")
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_bytes().splitlines(), start=1):
        if not line:
            _error(f"{source_kind} event stream contains an empty record")
        try:
            event = _mapping(json.loads(line), f"event line {line_number}")
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise FocusedCrashPairValidationError(
                "event stream is invalid JSONL"
            ) from exc
        if (
            set(event) != _EVENT_KEYS
            or event.get("event_schema_version") != 1
            or event.get("source_kind") != source_kind
            or not isinstance(event.get("run_id"), str)
            or not isinstance(event.get("source_id"), str)
            or not isinstance(event.get("source_instance"), str)
            or not isinstance(event.get("event_type"), str)
            or not isinstance(event.get("payload"), Mapping)
        ):
            _error("runtime event envelope schema or source kind drifted")
        _integer(event.get("source_sequence"), "source sequence", 1)
        _integer(event.get("source_monotonic_ns"), "source timestamp")
        events.append(dict(event))
    return events


def _validate_sources(
    root: Path,
) -> tuple[list[dict[str, Any]], list[list[str]]]:
    events = [
        *_read_jsonl(root / "raw" / "replica-events.jsonl", "replica"),
        *_read_jsonl(
            root / "raw" / "adaptive-manager-events.jsonl",
            "adaptation_manager",
        ),
        *_read_jsonl(root / "raw" / "client-events.jsonl", "client"),
    ]
    by_source: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for event in events:
        key = (
            str(event["source_kind"]),
            str(event["source_id"]),
            str(event["source_instance"]),
        )
        by_source.setdefault(key, []).append(event)
    if not by_source or len({str(event["run_id"]) for event in events}) != 1:
        _error("raw sources do not belong to one run")
    source_ids: set[tuple[str, str]] = set()
    for (kind, source_id, _instance), source_events in by_source.items():
        if (kind, source_id) in source_ids:
            _error("one source ID spans multiple source instances")
        source_ids.add((kind, source_id))
        sequences = [int(event["source_sequence"]) for event in source_events]
        timestamps = [int(event["source_monotonic_ns"]) for event in source_events]
        if sequences != list(range(1, len(source_events) + 1)):
            _error("source sequence is not contiguous")
        if timestamps != sorted(timestamps):
            _error("source monotonic time regressed")
    inventory = [list(source) for source in sorted(by_source)]
    recorded_sources = [
        list(source)
        for source in sorted({(kind, source_id) for kind, source_id, _ in by_source})
    ]
    recorded_inventory = _read_json(
        root / "runtime" / "source-inventory.json", "source inventory"
    )
    if recorded_inventory.get("sources") != recorded_sources:
        _error("recorded source inventory differs from raw envelopes")
    return events, inventory


def _decode_bundle(
    path: Path,
    issuer: str,
    epoch_number: int,
    contract: Mapping[str, object],
) -> tuple[bytes, Any]:
    if path.is_symlink() or not path.is_file():
        _error(f"epoch {epoch_number} native bundle is absent")
    wire = path.read_bytes()
    try:
        decoded = factorial_validation.decode_epoch_change_bundle(
            wire, issuer_public_key=issuer
        )
    except factorial_validation.FactorialValidationError as exc:
        raise FocusedCrashPairValidationError(
            f"epoch {epoch_number} native bundle is invalid"
        ) from exc
    if decoded.epoch_number != epoch_number or len(decoded.trees) != contract["quorum"]:
        _error(f"epoch {epoch_number} bundle identity drifted")
    return wire, decoded


def _canonical_tree_members(
    root: int, contract: Mapping[str, object]
) -> tuple[int, ...]:
    survivors = tuple(contract["survivors"])
    targets = tuple(contract["targets"])
    fanout = int(contract["fanout"])
    internal = tuple(replica for replica in survivors if replica != root)[:fanout]
    leaves = tuple(replica for replica in survivors if replica not in (root, *internal))
    return (root, *internal, *leaves, *targets)


def _validate_trees(
    decoded: Any,
    roots: Sequence[int],
    label: str,
    contract: Mapping[str, object],
) -> None:
    quorum = int(contract["quorum"])
    if tuple(tree.tree_id for tree in decoded.trees) != tuple(range(quorum)):
        _error(f"{label} tree IDs drifted")
    if tuple(tree.members[0] for tree in decoded.trees) != tuple(roots):
        _error(f"{label} roots drifted")
    is_v3 = (
        _mapping(contract["profile"], "focused profile").get("profile_id")
        in _FCRASH_H_V3_PROFILE_IDS
    )
    members = tuple(_integer(member, "tree member") for member in contract["members"])
    first_leaf = (len(members) - 2) // int(contract["fanout"]) + 1
    for tree, root in zip(decoded.trees, roots, strict=True):
        if (
            tree.fanout != contract["fanout"]
            or tree.pipeline_stretch != contract["pipeline_stretch"]
            or tuple(tree.wait_exempt) != tuple(contract["targets"])
        ):
            _error(f"{label} native placement structure drifted")
        if is_v3:
            if (
                len(tree.members) != len(members)
                or set(tree.members) != set(members)
                or tuple(tree.members[:1]) != (root,)
                or any(
                    target not in tree.members[first_leaf:]
                    for target in contract["targets"]
                )
            ):
                _error(f"{label} native placement structure drifted")
        elif tuple(tree.members) != _canonical_tree_members(root, contract):
            _error(f"{label} native placement structure drifted")


def _containment_roots(
    ranked_ids: Sequence[int], contract: Mapping[str, object]
) -> tuple[int, ...]:
    """Mirror native containment placement without reordering healthy roots."""

    baseline = tuple(range(int(contract["quorum"])))
    eligible = tuple(
        _integer(replica, "containment ranked replica") for replica in ranked_ids
    )
    if len(set(eligible)) != len(eligible):
        _error("containment ranking duplicates an eligible replica")
    preserved = {root for root in baseline if root in eligible}
    replacements = iter(replica for replica in eligible if replica not in preserved)
    roots: list[int] = []
    for root in baseline:
        if root in preserved:
            roots.append(root)
            continue
        try:
            roots.append(next(replacements))
        except StopIteration as exc:
            raise FocusedCrashPairValidationError(
                "containment ranking cannot fill every baseline root slot"
            ) from exc
    if len(set(roots)) != len(roots):
        _error("containment placement repeats a root")
    return tuple(roots)


def _command_payload(decoded: Any, payload: Mapping[str, Any]) -> dict[str, object]:
    height = _integer(payload.get("command_block_height"), "command block height")
    delay = decoded.command.activation_delay_blocks
    expected = {
        "command_block_height": height,
        "command_block_hash": _digest(
            payload.get("command_block_hash"), "command hash"
        ),
        "payload_digest": decoded.command.payload_digest,
        "predecessor_epoch_number": decoded.epoch_number - 1,
        "predecessor_epoch_digest": decoded.previous_epoch_digest,
        "successor_epoch_number": decoded.epoch_number,
        "successor_epoch_digest": decoded.epoch_digest,
        "activation_delay_blocks": delay,
        "activation_height": height + delay,
    }
    if dict(payload) != expected:
        _error("committed epoch command differs from the native bundle")
    return expected


def _validate_transition(
    events: Sequence[Mapping[str, Any]],
    decoded: Any,
    contract: Mapping[str, object],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    commands = [
        event
        for event in events
        if event["event_type"] == "epoch.command_committed"
        and event["payload"].get("successor_epoch_number") == decoded.epoch_number
    ]
    activations = [
        event
        for event in events
        if event["event_type"] == "epoch.activated"
        and event["payload"].get("epoch_number") == decoded.epoch_number
    ]
    command_sources = {str(event["source_id"]) for event in commands}
    activation_sources = {str(event["source_id"]) for event in activations}
    expected_sources = {f"replica-{replica}" for replica in contract["survivors"]}
    if (
        len(commands) != len(activations) != 0
        or len(commands) != len(contract["survivors"])
        or command_sources != expected_sources
        or activation_sources != expected_sources
    ):
        _error("transition does not contain one command and activation per survivor")
    expected_command: dict[str, object] | None = None
    for event in commands:
        parsed = _command_payload(decoded, _mapping(event["payload"], "command"))
        if expected_command is None:
            expected_command = parsed
        elif parsed != expected_command:
            _error("survivors disagree on the epoch command")
    assert expected_command is not None
    expected_activation = {
        "epoch_number": decoded.epoch_number,
        "tree_id": 0,
        "epoch_digest": decoded.epoch_digest,
        "activation_height": expected_command["activation_height"],
    }
    commands_by_source = {str(event["source_id"]): event for event in commands}
    for activation in activations:
        if dict(activation["payload"]) != expected_activation:
            _error("survivor activation identity drifted")
        command = commands_by_source[str(activation["source_id"])]
        if int(activation["source_monotonic_ns"]) <= int(
            command["source_monotonic_ns"]
        ):
            _error("survivor activation precedes its command")
    return commands, activations


def _observation_id(observation: Mapping[str, Any]) -> str:
    configuration = _mapping(observation.get("configuration"), "observation config")
    outcome = observation.get("outcome")
    outcome_code = 1 if outcome in {"on_time", "timeout"} else -1
    if outcome_code < 0:
        _error("evidence observation outcome is unsupported")
    payload = b"".join(
        (
            b"kauri-response-observation-v1",
            _integer(observation.get("reporter_id"), "reporter").to_bytes(2, "big"),
            _integer(
                observation.get("observed_replica_id"), "observed replica"
            ).to_bytes(2, "big"),
            _integer(configuration.get("epoch_number"), "observation epoch").to_bytes(
                4, "big"
            ),
            _integer(configuration.get("tree_id"), "observation tree").to_bytes(
                4, "big"
            ),
            bytes.fromhex(
                _digest(configuration.get("epoch_digest"), "observation epoch digest")
            ),
            bytes.fromhex(
                _digest(observation.get("block_hash"), "observation block hash")
            ),
            outcome_code.to_bytes(1, "big"),
        )
    )
    return _sha_bytes(payload)


def reconstruct_focused_ranking(
    events: Sequence[Mapping[str, Any]],
    *,
    membership_replica_ids: Sequence[int],
    predecessor_epoch_number: int,
    predecessor_epoch_digest: str,
    baseline_evidence_cutoff: int,
    current_evidence_cutoff: int,
    policy: Mapping[str, Any],
    seed: int,
    suffix_only: bool,
) -> dict[str, object]:
    """Replay the native scorer and expose its exact eligible ordering."""

    try:
        replay = factorial_validation.replay_native_adaptation_snapshot(
            events,
            membership_replica_ids=membership_replica_ids,
            predecessor_epoch_number=predecessor_epoch_number,
            predecessor_epoch_digest=predecessor_epoch_digest,
            baseline_evidence_cutoff=baseline_evidence_cutoff,
            current_evidence_cutoff=current_evidence_cutoff,
            policy=policy,
            seed=seed,
            suffix_only=suffix_only,
        )
    except factorial_validation.FactorialValidationError as exc:
        raise FocusedCrashPairValidationError(
            "native adaptation snapshot replay rejected"
        ) from exc
    ranking = tuple(
        _mapping(row, "native ranking row")
        for row in _sequence(replay.get("ranking"), "native ranking")
    )
    minimum_attempts = _integer(
        policy.get("minimum_attempts"), "ranking minimum attempts", 1
    )
    if any(
        row.get("eligible") is True
        and _integer(row.get("attempt_count"), "ranking attempt count")
        < minimum_attempts
        for row in ranking
    ):
        _error("ranking contains a replica below the minimum attempt count")
    return {
        **dict(replay),
        "ranked_ids": [
            _integer(row.get("replica_id"), "ranked replica")
            for row in ranking
            if row.get("eligible") is True
        ],
    }


def epoch_structural_projection(decoded: Any) -> dict[str, object]:
    """Project only matched, arm-independent native Epoch 1 structure."""

    return {
        "issuer_id": decoded.command.issuer_id,
        "successor_epoch_number": decoded.command.successor_epoch_number,
        "command_predecessor_epoch_digest": decoded.command.predecessor_epoch_digest,
        "activation_delay_blocks": decoded.command.activation_delay_blocks,
        "epoch_number": decoded.epoch_number,
        "previous_epoch_digest": decoded.previous_epoch_digest,
        "membership_digest": decoded.membership_digest,
        "generation_seed": decoded.generation_seed,
        "policy_version": decoded.policy_version,
        "trees": [asdict(tree) for tree in decoded.trees],
    }


def _ranking(
    events: Sequence[Mapping[str, Any]],
    epoch1: Any,
    contract: Mapping[str, object],
    *,
    predecessor_epoch: int,
) -> tuple[list[int], list[str], tuple[int, ...], str | None, int | None, int | None]:
    manager_events = [
        event for event in events if event["source_kind"] == "adaptation_manager"
    ]
    accepted = [
        event
        for event in manager_events
        if event["event_type"] == "evidence.observation_accepted"
        and _mapping(
            _mapping(
                _mapping(event["payload"], "accepted evidence payload").get(
                    "observation"
                ),
                "accepted observation",
            ).get("configuration"),
            "observation configuration",
        ).get("epoch_number")
        == predecessor_epoch
    ]
    members = tuple(contract["members"])
    survivors = tuple(contract["survivors"])
    expected_targets = tuple(contract["targets"])
    if not accepted:
        _error("ranking evidence is absent")
    has_snapshot_audit = any(
        event["event_type"] == "adaptive_v2_evidence_snapshot"
        for event in manager_events
    )
    replay_snapshot_id: str | None = None
    replay_cutoff: int | None = None
    replay_audit_ns: int | None = None
    audited_eligible_ranking: tuple[int, ...] | None = None
    if has_snapshot_audit:
        all_audit_events = [
            event
            for event in manager_events
            if event["event_type"] == "adaptive_v2_evidence_snapshot"
        ]
        audit_events = [
            event
            for event in all_audit_events
            if _mapping(event["payload"], "native ranking audit").get(
                "predecessor_epoch_number"
            )
            == predecessor_epoch
        ]
        if len(audit_events) != 1:
            _error("ranking evidence lacks one audit for the selected predecessor")
        audit = _mapping(audit_events[0]["payload"], "native ranking audit")
        audited_eligible_ranking = tuple(
            _integer(replica, "native audit eligible replica")
            for replica in _sequence(
                audit.get("eligible_ranking"), "native audit eligible ranking"
            )
        )
        replay_audit_ns = _integer(
            audit_events[0]["source_monotonic_ns"], "native ranking audit timestamp"
        )
        audited_epoch = _integer(
            audit.get("predecessor_epoch_number"), "ranking predecessor epoch"
        )
        if audited_epoch == 0:
            replay_digest = str(contract["epoch_zero_digest"])
            baseline_cutoff = _integer(
                audit.get("baseline_cutoff"), "ranking baseline cutoff"
            )
            suffix_only = False
        elif audited_epoch == 1:
            replay_digest = epoch1.epoch_digest
            baseline_cutoff = _integer(
                audit.get("baseline_cutoff"), "ranking baseline cutoff"
            )
            suffix_only = True
        else:
            _error("ranking audit is not bound to Epoch 0 or Epoch 1")
        current_cutoff = _integer(
            audit.get("current_cutoff"), "ranking current cutoff", 1
        )
        replay = reconstruct_focused_ranking(
            manager_events,
            membership_replica_ids=members,
            predecessor_epoch_number=audited_epoch,
            predecessor_epoch_digest=replay_digest,
            baseline_evidence_cutoff=baseline_cutoff,
            current_evidence_cutoff=current_cutoff,
            policy=_NATIVE_RESPONSIVENESS_POLICY,
            seed=_integer(epoch1.generation_seed, "Epoch 1 generation seed"),
            suffix_only=suffix_only,
        )
        replay_snapshot_id = _digest(
            replay.get("snapshot_id"), "ranking replay snapshot ID"
        )
        replay_cutoff = current_cutoff
        for other_epoch in {0, 1} - {audited_epoch}:
            other_audits = [
                _mapping(candidate["payload"], "native ranking audit")
                for candidate in all_audit_events
                if _mapping(candidate["payload"], "native ranking audit").get(
                    "predecessor_epoch_number"
                )
                == other_epoch
            ]
            if len(other_audits) > 1:
                _error("ranking evidence duplicates a predecessor audit")
            if not other_audits:
                continue
            other_audit = other_audits[0]
            reconstruct_focused_ranking(
                manager_events,
                membership_replica_ids=members,
                predecessor_epoch_number=other_epoch,
                predecessor_epoch_digest=(
                    str(contract["epoch_zero_digest"])
                    if other_epoch == 0
                    else epoch1.epoch_digest
                ),
                baseline_evidence_cutoff=_integer(
                    other_audit.get("baseline_cutoff"),
                    "ranking baseline cutoff",
                ),
                current_evidence_cutoff=_integer(
                    other_audit.get("current_cutoff"),
                    "ranking current cutoff",
                    1,
                ),
                policy=_NATIVE_RESPONSIVENESS_POLICY,
                seed=_integer(epoch1.generation_seed, "Epoch 1 generation seed"),
                suffix_only=other_epoch == 1,
            )
        ranked = list(replay["ranked_ids"])
    else:
        if len(accepted) != len(members):
            _error("ranking evidence lacks native replay audit and full membership")
        observed: set[int] = set()
        responsive: list[tuple[int, int]] = []
        for ingestion_sequence, event in enumerate(accepted, start=1):
            payload = _mapping(event["payload"], "accepted evidence payload")
            observation = _mapping(payload.get("observation"), "accepted observation")
            replica = _integer(
                observation.get("observed_replica_id"), "observed replica"
            )
            configuration = _mapping(
                observation.get("configuration"), "observation configuration"
            )
            if (
                payload.get("ingestion_sequence") != ingestion_sequence
                or replica in observed
                or replica not in members
                or configuration.get("epoch_number") != 1
                or configuration.get("tree_id") != 0
                or configuration.get("epoch_digest") != epoch1.epoch_digest
                or observation.get("observation_id") != _observation_id(observation)
            ):
                _error("legacy ranking evidence identity drifted")
            observed.add(replica)
            if observation.get("outcome") == "on_time":
                responsive.append(
                    (
                        _integer(
                            observation.get("response_duration_us"),
                            "response duration",
                        ),
                        replica,
                    )
                )
            elif observation.get("response_duration_us") != 0:
                _error("timeout observation contains a response duration")
        if observed != set(members):
            _error("legacy ranking evidence membership drifted")
        ranked = [replica for _latency, replica in sorted(responsive)]
    timeout_targets = tuple(sorted(set(members) - set(ranked)))
    if len(ranked) != len(survivors) or timeout_targets != expected_targets:
        _error("ranking eligibility does not identify exactly the focused nonresponses")
    if (
        audited_eligible_ranking is not None
        and _mapping(contract["profile"], "focused profile").get("profile_id")
        in _FCRASH_H_V3_PROFILE_IDS
    ):
        expected_audit_roots = (
            _containment_roots(ranked, contract)
            if predecessor_epoch == 0
            else tuple(ranked[: int(contract["quorum"])])
        )
        if audited_eligible_ranking != expected_audit_roots:
            _error("native audit eligible ranking drifted")
    observation_ids = sorted(
        str(
            _mapping(
                _mapping(event["payload"], "accepted evidence payload").get(
                    "observation"
                ),
                "accepted observation",
            ).get("observation_id")
        )
        for event in accepted
    )
    return (
        ranked,
        observation_ids,
        timeout_targets,
        replay_snapshot_id,
        replay_cutoff,
        replay_audit_ns,
    )


def _select_latest_common_commit(
    commits: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
    contract: Mapping[str, object],
) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]]:
    survivor_sources = {f"replica-{replica}" for replica in contract["survivors"]}
    eligible: list[tuple[Mapping[str, Any], list[Mapping[str, Any]]]] = []
    for commit in commits:
        payload = _mapping(commit["payload"], "authoritative commit")
        identity = {
            key: payload.get(key)
            for key in (
                "block_height",
                "block_hash",
                "parent_hash",
                "transaction_count",
                "commit_batch_index",
            )
        }
        matching = [
            event
            for event in observations
            if event["source_kind"] == "replica"
            and event["source_id"] in survivor_sources
            and dict(_mapping(event["payload"], "common commit observation"))
            == identity
        ]
        if len({str(event["source_id"]) for event in matching}) >= int(
            contract["quorum"]
        ):
            eligible.append((commit, matching))
    if not eligible:
        _error("common commit does not contain matching survivor witnesses")
    latest_height = max(
        _integer(
            _mapping(commit["payload"], "authoritative commit").get("block_height"),
            "commit height",
            1,
        )
        for commit, _matching in eligible
    )
    latest = [
        item
        for item in eligible
        if _mapping(item[0]["payload"], "authoritative commit").get("block_height")
        == latest_height
    ]
    if len(latest) != 1:
        _error("latest common commit identity is ambiguous")
    return latest[0]


def _commit_reconstruction(
    root: Path,
    events: Sequence[Mapping[str, Any]],
    epoch1: Any,
    epoch2: Any | None,
    contract: Mapping[str, object],
) -> tuple[list[Mapping[str, Any]], dict[str, object]]:
    commits = [event for event in events if event["event_type"] == "block.committed"]
    authoritative_source = str(contract["authoritative_source_id"])
    member_sources = {f"replica-{member}" for member in contract["members"]}
    if any(
        event["source_kind"] != "replica" or event["source_id"] not in member_sources
        for event in commits
    ):
        _error("raw evidence contains a non-member committed block")
    authoritative_commits = [
        event for event in commits if event["source_id"] == authoritative_source
    ]
    if len(authoritative_commits) < 4:
        _error("raw evidence lacks the minimum authoritative commit chain")
    profile = _mapping(contract.get("profile"), "focused profile")
    is_v3 = profile.get("profile_id") in _FCRASH_H_V3_PROFILE_IDS
    configurations_by_source_epoch: dict[
        tuple[str, int], list[tuple[tuple[int, int], int]]
    ] = {}
    if is_v3:
        instances = {
            source: _authoritative_lifecycle_instance(events, source)
            for source in member_sources
        }
        if any(
            event.get("source_instance") != instances[str(event["source_id"])]
            for event in commits
        ):
            _error("committed block is not lifecycle-bound")
        epoch_trees = {
            0: tuple(range(len(tuple(contract["members"])))),
            1: tuple(tree.tree_id for tree in epoch1.trees),
        }
        if epoch2 is not None:
            epoch_trees[2] = tuple(tree.tree_id for tree in epoch2.trees)
        allowed_digests = {
            0: str(contract["epoch_zero_digest"]),
            1: epoch1.epoch_digest,
        }
        if epoch2 is not None:
            allowed_digests[2] = epoch2.epoch_digest
        expected_indexes = {
            (source, epoch): 0 for source in member_sources for epoch in allowed_digests
        }
        config_events = sorted(
            [
                event
                for event in events
                if event["event_type"] == "adaptive.configuration_active"
                and event["source_kind"] == "replica"
                and event["source_id"] in member_sources
                and event.get("source_instance") == instances[str(event["source_id"])]
            ],
            key=lambda event: (
                str(event["source_id"]),
                _integer(event["source_sequence"], "configuration sequence", 1),
                _integer(event["source_monotonic_ns"], "configuration timestamp"),
            ),
        )
        for event in config_events:
            source = str(event["source_id"])
            payload = _mapping(event["payload"], "committed-block configuration")
            epoch = _integer(payload.get("epoch_number"), "configuration epoch")
            tree = _integer(payload.get("tree_id"), "configuration tree")
            if (
                epoch not in allowed_digests
                or payload.get("epoch_digest") != allowed_digests[epoch]
                or not epoch_trees[epoch]
                or tree != epoch_trees[epoch][expected_indexes[(source, epoch)]]
            ):
                _error("committed-block cyclic configuration drifted")
            configurations_by_source_epoch.setdefault((source, epoch), []).append(
                (
                    (
                        _integer(event["source_sequence"], "configuration sequence", 1),
                        _integer(
                            event["source_monotonic_ns"], "configuration timestamp"
                        ),
                    ),
                    tree,
                )
            )
            expected_indexes[(source, epoch)] = (
                expected_indexes[(source, epoch)] + 1
            ) % len(epoch_trees[epoch])
        if {
            epoch
            for source, epoch in configurations_by_source_epoch
            if source == authoritative_source
        } != set(allowed_digests):
            _error("v3 authoritative commit chain lacks active epoch configurations")
    commits.sort(
        key=(
            (
                lambda event: _integer(
                    event["source_sequence"], "commit source sequence", 1
                )
            )
            if is_v3
            else (
                lambda event: _integer(
                    event["payload"].get("block_height"), "commit height", 1
                )
            )
        )
    )
    prior_hash: str | None = None
    prior_height: int | None = None
    prior_epoch = 0
    prior_sequence: int | None = None
    for event in commits:
        is_authoritative = event["source_id"] == authoritative_source
        payload = _mapping(event["payload"], "authoritative commit")
        if set(payload) != {
            "block_height",
            "block_hash",
            "parent_hash",
            "transaction_count",
            "commit_batch_index",
            "designated_observer",
            "decision_proof",
            "view_generation",
        }:
            _error("authoritative commit schema drifted")
        height = _integer(payload.get("block_height"), "commit height", 1)
        block_hash = _digest(payload.get("block_hash"), "commit hash")
        if is_v3:
            _digest(payload.get("parent_hash"), "commit parent hash")
        transactions = _uint64(payload.get("transaction_count"), "transactions")
        commit_batch_index = _uint64(
            payload.get("commit_batch_index"), "commit batch index"
        )
        view_generation = _uint64(
            payload.get("view_generation"), "commit view generation", 1
        )
        proof = _mapping(payload.get("decision_proof"), "decision proof")
        if set(proof) != {
            "epoch_number",
            "tree_id",
            "epoch_digest",
            "block_hash",
        }:
            _error("authoritative commit decision proof schema drifted")
        sequence = _integer(event["source_sequence"], "commit source sequence", 1)
        if (
            (
                is_authoritative
                and is_v3
                and prior_sequence is not None
                and sequence <= prior_sequence
            )
            or (
                is_authoritative
                and prior_height is not None
                and (height != prior_height + 1 if is_v3 else height <= prior_height)
            )
            or (
                is_authoritative
                and prior_hash is not None
                and payload.get("parent_hash") != prior_hash
            )
            or payload.get("designated_observer")
            is not (event["source_id"] == authoritative_source)
            or (not is_v3 and commit_batch_index != 0)
            or (not is_v3 and view_generation != 1)
            or (
                transactions not in {0, int(contract["transactions_per_block"])}
                if is_v3
                else transactions % 5 != 0
            )
            or proof.get("block_hash") != block_hash
        ):
            _error("authoritative commit chain or workload identity drifted")
        expected_epoch = _integer(proof.get("epoch_number"), "commit epoch")
        allowed_digests = {
            0: str(contract["epoch_zero_digest"]),
            1: epoch1.epoch_digest,
        }
        if epoch2 is not None:
            allowed_digests[2] = epoch2.epoch_digest
        if (
            expected_epoch not in allowed_digests
            or (is_authoritative and expected_epoch < prior_epoch)
            or proof.get("epoch_digest") != allowed_digests[expected_epoch]
        ):
            _error("authoritative commit decision proof drifted")
        tree = _integer(proof.get("tree_id"), "commit tree")
        if not is_v3 and tree != 0:
            _error("authoritative commit decision proof drifted")
        if is_v3:
            commit_key = (
                sequence,
                _integer(event["source_monotonic_ns"], "commit timestamp"),
            )
            timeline = configurations_by_source_epoch.get(
                (str(event["source_id"]), expected_epoch)
            )
            if timeline is None:
                _error("committed block lacks an active epoch configuration")
            generation_base = (expected_epoch << 32) + 1
            if view_generation < generation_base:
                _error("authoritative commit generation is not activated")
            rotation_ordinal = view_generation - generation_base
            if rotation_ordinal >= len(timeline):
                _error("authoritative commit generation is not activated")
            generation_configuration = timeline[rotation_ordinal]
            if (
                generation_configuration[0] > commit_key
                or generation_configuration[1] != tree
            ):
                _error("authoritative commit is not bound to its active generation")
        if is_authoritative:
            prior_epoch = expected_epoch
            prior_hash = block_hash
            prior_height = height
            prior_sequence = sequence
    observations = [
        event
        for event in events
        if event["event_type"] == "block.commit_observed"
        and event["source_kind"] == "replica"
    ]
    _select_latest_common_commit(authoritative_commits, observations, contract)
    phase_document = _read_json(
        root / "derived" / "phase-windows.json", "phase windows"
    )
    recorded = _sequence(phase_document.get("phases"), "phase windows")
    phase_names = tuple(str(name) for name in contract["phase_names"])
    width_ns = int(contract["bucket_width_seconds"]) * 1_000_000_000
    if recorded:
        if len(recorded) != len(phase_names):
            _error("phase window count drifted")
        windows: list[tuple[str, int, int]] = []
        for name, value in zip(phase_names, recorded, strict=True):
            row = _mapping(value, "phase window")
            start = _integer(row.get("start_ns"), "phase start")
            end = _integer(row.get("end_ns"), "phase end", 1)
            if row.get("phase") != name or end - start != width_ns:
                _error("phase window identity or duration drifted")
            windows.append((name, start, end))
        if any(right[1] != left[2] for left, right in zip(windows, windows[1:])):
            _error("phase windows are not contiguous")
    else:
        first_ns = min(
            int(event["source_monotonic_ns"]) for event in authoritative_commits
        )
        origin = first_ns - (first_ns % width_ns)
        windows = [
            (name, origin + index * width_ns, origin + (index + 1) * width_ns)
            for index, name in enumerate(phase_names)
        ]
    phase_rows: list[dict[str, object]] = []
    for phase, start, end in windows:
        transaction_count = sum(
            int(event["payload"]["transaction_count"])
            for event in authoritative_commits
            if start <= int(event["source_monotonic_ns"]) < end
        )
        if transaction_count <= 0:
            _error("throughput phase has no authoritative committed transactions")
        phase_rows.append(
            {
                "phase": phase,
                "start_ns": start,
                "end_ns": end,
                "transactions": transaction_count,
                "mean_milli_tps": transaction_count
                * 1_000_000_000_000
                // (end - start),
            }
        )
    return authoritative_commits, {
        "phases": phase_rows,
        "late_window_throughput_milli_tps": phase_rows[-1]["mean_milli_tps"],
    }


_MANAGER_SINGLETON_OPTIONS = {
    "--listen",
    "--tls-privkey",
    "--tls-cert",
    "--issuer-id",
    "--issuer-private-key",
    "--activation-delay-blocks",
    "--convergence-deadline-seconds",
    "--tree-fanout",
    "--pipeline-stretch",
    "--shape-candidate-fanouts",
    "--shape-deterministic-seed",
    "--responsiveness-policy-version",
    "--required-nonresponsive",
    "--responsiveness-attempt-window",
    "--responsiveness-minimum-attempts",
    "--responsiveness-minimum-response-rate-ppm",
    "--responsiveness-maximum-timeout-rate-ppm",
    "--responsiveness-trailing-timeout-streak",
    "--responsiveness-latency-percentile-basis-points",
    "--structured-event-run-id",
    "--structured-event-source-instance",
    "--structured-event-output",
}
_MANAGER_REPEATABLE_OPTIONS = {"--transition-request", "--bundle-output", "--replica"}


def _validate_manager_boundary(
    contract: Mapping[str, object],
    argv: Sequence[Any],
    manager_input: Mapping[str, Any],
    manager_events: Sequence[Mapping[str, Any]],
    *,
    transition_count: int,
) -> None:
    arguments = tuple(argv)
    if (
        not arguments
        or any(not isinstance(value, str) for value in arguments)
        or Path(arguments[0]).name != "adaptation-manager"
        or set(manager_input)
        != {"input_source", "requested_argv", "observed_argv", "stdin"}
        or manager_input.get("input_source") != "normalized_manager_launch_boundary_v1"
        or manager_input.get("requested_argv") != list(arguments)
        or manager_input.get("observed_argv") != list(arguments)
        or manager_input.get("stdin") != "closed"
    ):
        _error("manager launch boundary schema or identity drifted")
    counts: dict[str, int] = {}
    position = 1
    while position < len(arguments):
        option = arguments[position]
        if option not in _MANAGER_SINGLETON_OPTIONS | _MANAGER_REPEATABLE_OPTIONS:
            _error("manager launch boundary contains an unknown option")
        if position + 1 >= len(arguments) or arguments[position + 1].startswith("--"):
            _error("manager launch boundary contains an unpaired option")
        counts[option] = counts.get(option, 0) + 1
        position += 2
    if any(counts.get(option, 0) > 1 for option in _MANAGER_SINGLETON_OPTIONS):
        _error("manager launch boundary repeats a singleton option")
    if counts.get("--replica") != len(tuple(contract["members"])):
        _error("manager launch boundary does not contain the exact membership")
    expected_transition_count = (
        transition_count
        if _mapping(contract["profile"], "focused profile").get("profile_id")
        in _FCRASH_H_V3_PROFILE_IDS
        else int(contract["adaptive_transition_count"])
    )
    if counts.get("--transition-request") != expected_transition_count:
        _error("manager launch boundary transition cardinality drifted")
    if counts.get("--bundle-output") != counts.get("--transition-request"):
        _error("manager launch boundary bundle output cardinality drifted")
    try:
        factorial_validation.validate_manager_blinding(arguments, manager_events)
    except factorial_validation.FactorialValidationError as exc:
        raise FocusedCrashPairValidationError("manager boundary is not blind") from exc


def _aggregate_child_provenance(
    trusted_provenance: object, directory: Path, *, expected_arm: str | None = None
) -> Mapping[str, Any]:
    aggregate = _mapping(trusted_provenance, "aggregate trusted provenance")
    if (
        set(aggregate) != {"schema_version", "children"}
        or aggregate.get("schema_version") != 1
    ):
        _error("aggregate trusted provenance schema drifted")
    children = _mapping(aggregate.get("children"), "trusted child provenance")
    if expected_arm is not None:
        if expected_arm not in {"control", "adaptive"} or set(children) != {
            "control",
            "adaptive",
        }:
            _error("aggregate trusted provenance arm binding drifted")
        raw_entry = children[expected_arm]
        entry = _mapping(raw_entry, "trusted child entry")
        if set(entry) != {"tree_sha256", "seal_sha256", "provenance"}:
            _error("trusted child entry schema drifted")
        seal = verify_evidence_seal(directory)
        if (
            entry.get("tree_sha256") != seal.tree_sha256
            or entry.get("seal_sha256") != seal.seal_sha256
        ):
            _error("trusted provenance arm entry does not bind its child")
        provenance = _mapping(entry.get("provenance"), "child provenance")
        if (
            provenance.get("evidence_tree_sha256") != seal.tree_sha256
            or provenance.get("evidence_seal_sha256") != seal.seal_sha256
        ):
            _error("trusted child provenance seal binding drifted")
        return provenance
    seal = verify_evidence_seal(directory)
    matches: list[Mapping[str, Any]] = []
    for raw_entry in children.values():
        entry = _mapping(raw_entry, "trusted child entry")
        if set(entry) != {"tree_sha256", "seal_sha256", "provenance"}:
            _error("trusted child entry schema drifted")
        if (
            entry.get("tree_sha256") == seal.tree_sha256
            and entry.get("seal_sha256") == seal.seal_sha256
        ):
            provenance = _mapping(entry.get("provenance"), "child provenance")
            if (
                provenance.get("evidence_tree_sha256") != seal.tree_sha256
                or provenance.get("evidence_seal_sha256") != seal.seal_sha256
            ):
                _error("trusted child provenance seal binding drifted")
            matches.append(provenance)
    if len(matches) != 1:
        _error("trusted provenance does not bind exactly one child")
    return matches[0]


def _validate_receipts(
    root: Path, profile_sha: str, proof_sha: str
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    manifest = _read_json(root / "manifest.json", "arm manifest")
    build = _read_json(root / "runtime" / "build-provenance.json", "build provenance")
    effective = _read_json(
        root / "runtime" / "effective-runtime.json", "runtime identity"
    )
    pair = _read_json(root / "pair-receipt.json", "pair receipt")
    if (
        set(manifest)
        != {
            "schema_version",
            "profile_sha256",
            "build_sha256",
            "pair_id",
            "pair_seed",
            "slot_id",
        }
        or set(build) != {"revision", "build_sha256"}
        or set(effective) != {"profile_sha256", "pair_seed"}
        or set(pair)
        != {
            "schema_version",
            "pair_id",
            "slot_id",
            "automatic_retries",
            "replacement_policy",
        }
        or manifest.get("schema_version") != 1
        or manifest.get("profile_sha256") != profile_sha
        or manifest.get("build_sha256") != build.get("build_sha256")
        or manifest.get("pair_seed") != effective.get("pair_seed")
        or effective.get("profile_sha256") != profile_sha
        or manifest.get("pair_id") != pair.get("pair_id")
        or manifest.get("slot_id") != pair.get("slot_id")
        or pair.get("automatic_retries") != 0
        or pair.get("replacement_policy") != "none"
    ):
        _error("arm manifest, build, runtime, or pair identity drifted")
    preflight = _read_json(root / "preflight.json", "preflight receipt")
    authorization = _read_json(root / "authorization.json", "authorization receipt")
    request_keys = {
        "schema_version",
        "profile_sha256",
        "topology_proof_sha256",
        "pair_id",
        "slot_id",
        "automatic_retries",
        "replacement_policy",
    }
    request = {key: preflight.get(key) for key in request_keys}
    request_sha = _sha_bytes(_canonical(request))
    if (
        set(preflight)
        != request_keys | {"request_sha256", "execution_authorized", "launch_permitted"}
        or set(authorization)
        != request_keys | {"request_sha256", "approval_reference", "approved_utc"}
        or preflight.get("request_sha256") != request_sha
        or preflight.get("execution_authorized") is not False
        or preflight.get("launch_permitted") is not False
        or authorization.get("request_sha256") != request_sha
        or any(authorization.get(key) != value for key, value in request.items())
        or request.get("profile_sha256") != profile_sha
        or request.get("topology_proof_sha256") != proof_sha
        or request.get("automatic_retries") != 0
        or request.get("replacement_policy") != "none"
        or not isinstance(authorization.get("approval_reference"), str)
        or not isinstance(authorization.get("approved_utc"), str)
    ):
        _error("preflight or authorization receipt is not exact and bound")
    return manifest, pair, build


def _validate_atomic_fault_receipt(
    contract: Mapping[str, object], receipt: Mapping[str, Any]
) -> None:
    plan = _mapping(receipt.get("fault_plan"), "fault plan")
    records = tuple(
        _mapping(value, "process record")
        for value in _sequence(receipt.get("process_records"), "process records")
    )
    outcomes = tuple(
        _mapping(value, "SIGKILL outcome")
        for value in _sequence(receipt.get("sigkill_outcomes"), "SIGKILL outcomes")
    )
    journal = tuple(
        _mapping(value, "fault journal event")
        for value in _sequence(receipt.get("fault_journal"), "fault journal")
    )
    targets = tuple(contract["targets"])
    expected_actions = [
        {
            "fault_id": f"crash-replica-{replica}",
            "kind": "replica_group_sigkill",
            "replica_id": replica,
        }
        for replica in targets
    ]
    if (
        set(plan) != {"schema_version", "seed", "scenario", "actions"}
        or plan.get("schema_version") != 1
        or plan.get("actions") != expected_actions
        or _mapping(plan.get("scenario"), "fault scenario")
        != {
            "replica_ids": list(contract["members"]),
            "quorum": contract["quorum"],
            "crash_budget": contract["fault_threshold"],
            "successor_bundle_retry_limit": 1,
        }
        or len(records) != len(targets)
        or len(outcomes) != len(targets)
        or len(journal) != 2 * len(targets)
    ):
        _error("atomic fault plan or evidence cardinality drifted")
    plan_sha = _sha_bytes(_canonical(plan).rstrip(b"\n"))
    requested: list[int] = []
    confirmed: list[int] = []
    for replica, record, outcome in zip(targets, records, outcomes, strict=True):
        identity = {
            "name": f"replica-{replica}",
            "replica_id": replica,
            "pid": record.get("pid"),
            "pgid": record.get("pgid"),
        }
        if (
            dict(record) != identity
            or type(record.get("pid")) is not int
            or record.get("pid") != record.get("pgid")
            or any(outcome.get(key) != value for key, value in identity.items())
            or outcome.get("fault_id") != f"crash-replica-{replica}"
            or outcome.get("signal_number") != 9
            or outcome.get("returncode") != -9
        ):
            _error("SIGKILL outcome does not match its owned process group")
        requested.append(
            _integer(outcome.get("requested_monotonic_ns"), "fault request")
        )
        confirmed.append(
            _integer(outcome.get("confirmed_monotonic_ns"), "fault confirmation")
        )
    if max(requested) >= min(confirmed):
        _error("SIGKILL batch was not requested atomically before confirmation")
    expected_lifecycles = ["started"] * len(targets) + ["terminal"] * len(targets)
    if [event.get("lifecycle") for event in journal] != expected_lifecycles:
        _error("fault journal does not contain one ordered terminal per target")
    for sequence, event in enumerate(journal):
        replica = targets[sequence % len(targets)]
        if (
            event.get("schema_version") != 1
            or event.get("source_id") != "fault-orchestrator"
            or event.get("source_sequence") != sequence
            or event.get("plan_sha256") != plan_sha
            or event.get("fault_id") != f"crash-replica-{replica}"
            or (
                sequence >= len(targets)
                and _mapping(event.get("outcome"), "fault terminal outcome").get(
                    "status"
                )
                != "succeeded"
            )
        ):
            _error("fault journal identity or terminal outcome drifted")


def validate_sealed_arm(
    run_directory: Path,
    *,
    trusted_provenance: object,
) -> dict[str, object]:
    """Reconstruct one sealed arm from raw sources before joining fault truth."""

    if not isinstance(trusted_provenance, Mapping):
        _error("trusted provenance must be an exact object")
    root = Path(run_directory)
    if root.is_symlink() or not root.is_dir():
        _error("sealed arm directory is absent")
    try:
        seal = verify_evidence_seal(root)
    except (EvidenceSealError, OSError) as exc:
        raise FocusedCrashPairValidationError("arm evidence seal rejected") from exc
    contract = validation_contract_from_profile(root)
    profile_sha = str(contract["profile_sha256"])
    proof_sha = str(contract["topology_proof_sha256"])
    manifest, pair_receipt, build = _validate_receipts(root, profile_sha, proof_sha)
    expected_provenance = {
        "schema_version": 1,
        "revision": build.get("revision"),
        "build_sha256": build.get("build_sha256"),
        "profile_sha256": profile_sha,
        "topology_proof_sha256": proof_sha,
        "evidence_tree_sha256": seal.tree_sha256,
        "evidence_seal_sha256": seal.seal_sha256,
    }
    if dict(trusted_provenance) != expected_provenance:
        _error("trusted provenance is not exact or child-seal-bound")
    _validate_runtime_configuration(root, contract)
    events, source_inventory = _validate_sources(root)

    issuer_path = root / "raw" / "issuer-public-key.txt"
    if issuer_path.is_symlink() or not issuer_path.is_file():
        _error("issuer public key is absent")
    issuer = issuer_path.read_text(encoding="utf-8").strip()
    if len(issuer) not in {66, 130}:
        _error("issuer public key encoding is malformed")
    try:
        issuer_bytes = bytes.fromhex(issuer)
    except ValueError as exc:
        raise FocusedCrashPairValidationError(
            "issuer public key encoding is malformed"
        ) from exc
    epoch1_wire, epoch1 = _decode_bundle(
        root / "raw" / "epoch1.bundle", issuer, 1, contract
    )
    if epoch1.previous_epoch_digest != contract["epoch_zero_digest"]:
        _error("Epoch 1 predecessor identity drifted")
    epoch2_path = root / "raw" / "epoch2.bundle"
    epoch2_wire: bytes | None = None
    epoch2: Any | None = None
    if epoch2_path.exists():
        epoch2_wire, epoch2 = _decode_bundle(epoch2_path, issuer, 2, contract)
        if epoch2.previous_epoch_digest != epoch1.epoch_digest:
            _error("Epoch 2 is not chained to Epoch 1")

    commands1, activations1 = _validate_transition(events, epoch1, contract)
    (
        containment_ranked_ids,
        containment_observation_ids,
        containment_timeout_targets,
        epoch1_snapshot_id,
        epoch1_cutoff,
        epoch1_audit_ns,
    ) = _ranking(events, epoch1, contract, predecessor_epoch=0)
    if epoch1_snapshot_id is not None and (
        epoch1.evidence_snapshot_id != epoch1_snapshot_id
        or epoch1.evidence_cutoff != epoch1_cutoff
    ):
        _error("Epoch 1 bundle is not bound to its native snapshot audit")
    epoch1_roots = (
        _containment_roots(containment_ranked_ids, contract)
        if _mapping(contract["profile"], "focused profile").get("profile_id")
        in _FCRASH_H_V3_PROFILE_IDS
        else tuple(range(int(contract["quorum"])))
    )
    _validate_trees(epoch1, epoch1_roots, "Epoch 1", contract)
    if epoch2 is None:
        if any(
            event["event_type"] in {"epoch.command_committed", "epoch.activated"}
            and event["payload"].get(
                "successor_epoch_number", event["payload"].get("epoch_number")
            )
            == 2
            for event in events
        ):
            _error("control arm contains an Epoch 2 transition")
        commands2: list[Mapping[str, Any]] = []
        activations2: list[Mapping[str, Any]] = []
    else:
        (
            ranked_ids,
            observation_ids,
            timeout_targets,
            epoch2_snapshot_id,
            epoch2_cutoff,
            epoch2_audit_ns,
        ) = _ranking(events, epoch1, contract, predecessor_epoch=1)
        if epoch2_snapshot_id is None or (
            epoch2.evidence_snapshot_id != epoch2_snapshot_id
            or epoch2.evidence_cutoff != epoch2_cutoff
        ):
            _error("Epoch 2 bundle is not bound to its native snapshot audit")
        if timeout_targets != containment_timeout_targets:
            _error("containment and optimization evidence disagree on fault targets")
        _validate_trees(
            epoch2,
            tuple(ranked_ids[: int(contract["quorum"])]),
            "Epoch 2",
            contract,
        )
        commands2, activations2 = _validate_transition(events, epoch2, contract)
        if (
            epoch2_audit_ns is None
            or min(
                _integer(event["source_monotonic_ns"], "Epoch 2 command timestamp")
                for event in commands2
            )
            <= epoch2_audit_ns
        ):
            _error("Epoch 2 command does not follow its native snapshot audit")
        if epoch2_audit_ns is None:
            _error("Epoch 2 native snapshot audit is absent")
        authoritative_source = str(contract["authoritative_source_id"])
        activation1_ns = max(
            _integer(event["source_monotonic_ns"], "Epoch 1 activation timestamp")
            for event in activations1
        )
        predecessor_commits = [
            event
            for event in events
            if event["event_type"] == "block.committed"
            and event["source_kind"] == "replica"
            and event["source_id"] == authoritative_source
            and _integer(event["source_monotonic_ns"], "predecessor commit timestamp")
            > activation1_ns
            and _integer(event["source_monotonic_ns"], "predecessor commit timestamp")
            < epoch2_audit_ns
            and _mapping(
                _mapping(event["payload"], "predecessor commit").get("decision_proof"),
                "predecessor decision proof",
            ).get("epoch_number")
            == 1
        ]
        _predecessor_commit, predecessor_observations = _select_latest_common_commit(
            predecessor_commits,
            [
                event
                for event in events
                if event["event_type"] == "block.commit_observed"
                and _integer(
                    event["source_monotonic_ns"], "common commit observation timestamp"
                )
                < epoch2_audit_ns
            ],
            contract,
        )
        observation_times = [
            _integer(
                event["source_monotonic_ns"], "common commit observation timestamp"
            )
            for event in predecessor_observations
        ]
        if (
            min(observation_times) <= activation1_ns
            or max(observation_times) >= epoch2_audit_ns
            or min(
                _integer(event["source_monotonic_ns"], "Epoch 2 command timestamp")
                for event in commands2
            )
            <= epoch2_audit_ns
        ):
            _error("Epoch 2 command precedes the fresh common-commit window")
    if epoch2 is None:
        ranked_ids = containment_ranked_ids
        observation_ids = containment_observation_ids
        timeout_targets = containment_timeout_targets

    commits, measurements = _commit_reconstruction(
        root, events, epoch1, epoch2, contract
    )
    if timeout_targets != tuple(contract["targets"]):
        _error("source-blind nonresponse reconstruction drifted")
    replica_sources = {
        int(str(event["source_id"]).removeprefix("replica-"))
        for event in events
        if event["source_kind"] == "replica"
    }
    members = set(contract["members"])
    survivors = set(contract["survivors"])
    if not survivors.issubset(replica_sources) or not replica_sources.issubset(members):
        _error("raw replica sources differ from the reconstructed membership")

    fault_receipt = _read_json(root / "raw" / "fault-receipt.json", "fault receipt")
    if (
        set(fault_receipt)
        != {
            "schema_version",
            "fault_plan",
            "process_records",
            "sigkill_outcomes",
            "fault_journal",
        }
        or fault_receipt.get("schema_version") != 1
    ):
        _error("fault receipt schema drifted")
    confirmations = {
        _integer(outcome.get("replica_id"), "fault outcome replica"): _integer(
            outcome.get("confirmed_monotonic_ns"), "fault confirmation"
        )
        for outcome in _sequence(fault_receipt["sigkill_outcomes"], "SIGKILL outcomes")
    }
    for event in events:
        if event["source_kind"] != "replica":
            continue
        replica = int(str(event["source_id"]).removeprefix("replica-"))
        if (
            replica in confirmations
            and int(event["source_monotonic_ns"]) > confirmations[replica]
        ):
            _error("crashed replica emitted an event after confirmed SIGKILL")
    _validate_atomic_fault_receipt(contract, fault_receipt)
    if "reporter_coverage_plan" in contract:
        fault_ns = max(confirmations.values())
        prefault_ns = min(
            _integer(
                _mapping(outcome, "SIGKILL outcome").get("requested_monotonic_ns"),
                "fault request",
            )
            for outcome in _sequence(
                fault_receipt["sigkill_outcomes"], "SIGKILL outcomes"
            )
        )
        _validate_prefault_active_configuration(contract, events, prefault_ns)
        coverage_witness = _fcrash_h_witness_from_events(
            contract,
            events,
            fault_receipt,
            activations1,
            activations2,
        )
        validate_fcrash_h_evidence(contract, coverage_witness)
        audit_ns = _integer(
            coverage_witness.get("snapshot_audit_monotonic_ns"),
            "snapshot audit timestamp",
        )
        if epoch1_audit_ns != audit_ns:
            _error("Epoch 1 replay audit timestamp drifted")
        if (
            min(
                _integer(event["source_monotonic_ns"], "Epoch 1 command timestamp")
                for event in commands1
            )
            <= audit_ns
        ):
            _error("Epoch 1 command does not follow its native snapshot audit")

    launch = _read_json(root / "runtime" / "launch-arguments.json", "launch arguments")
    observed = _read_json(
        root / "runtime" / "manager-observed-argv.json", "observed manager argv"
    )
    manager_input = _read_json(root / "runtime" / "manager-input.json", "manager input")
    if launch.get("manager_argv") != observed.get("argv"):
        _error("requested and observed manager argv differ")
    _validate_manager_boundary(
        contract,
        _sequence(observed.get("argv"), "observed manager argv"),
        manager_input,
        [event for event in events if event["source_kind"] == "adaptation_manager"],
        transition_count=1 if epoch2 is None else 2,
    )

    cleanup = _read_json(root / "cleanup.json", "cleanup result")
    if cleanup.get("complete") is not True:
        _error("arm cleanup is incomplete")
    commit_identity = [
        {
            "source_id": event["source_id"],
            "source_instance": event["source_instance"],
            "source_sequence": event["source_sequence"],
            "payload": event["payload"],
        }
        for event in commits
    ]
    all_commands = [*commands1, *commands2]
    all_activations = [*activations1, *activations2]
    epoch_identity = {
        "epoch1_bundle_sha256": _sha_bytes(epoch1_wire),
        "epoch2_bundle_sha256": (
            None if epoch2_wire is None else _sha_bytes(epoch2_wire)
        ),
        "issuer_public_key_sha256": _sha_bytes(issuer_bytes),
        "commands": [event["payload"] for event in all_commands],
        "activations": [event["payload"] for event in all_activations],
    }
    ranking_identity = {
        "source_epoch_digest": epoch1.epoch_digest,
        "observation_ids": observation_ids,
        "ranked_eligible_replica_ids": ranked_ids,
    }
    return {
        "schema_version": 1,
        "verdict": "PASS",
        "outcome": "PASS",
        "integrity_valid": True,
        "claim_slot": True,
        "source_blind_reconstruction": True,
        "fault_receipt_joined_after_reconstruction": True,
        "reconstructed_from_raw_evidence": True,
        "fault_receipt_joined": True,
        "native_bundles_decoded": True,
        "runtime_graph_validated": True,
        "ranking_reconstructed_from_raw": True,
        "epoch2_present": epoch2 is not None,
        "profile_sha256": profile_sha,
        "topology_proof_sha256": proof_sha,
        "build_sha256": build.get("build_sha256"),
        "issuer_public_key_sha256": _sha_bytes(issuer_bytes),
        "pair_id": manifest.get("pair_id"),
        "slot_id": manifest.get("slot_id"),
        "pair_seed": manifest.get("pair_seed"),
        "arm": "adaptive" if epoch2 is not None else "control",
        "epoch1_structure_sha256": _hash(epoch_structural_projection(epoch1)),
        "source_inventory_sha256": _hash(source_inventory),
        "authoritative_commit_identity_sha256": _hash(commit_identity),
        "epoch_identity_sha256": _hash(epoch_identity),
        "ranking_identity_sha256": _hash(ranking_identity),
        "fault_receipt_sha256": _hash(fault_receipt),
        "authoritative_commit_count": len(commits),
        "scientific_measurements": measurements,
        "child": {
            "path": str(root),
            "run_id": root.name,
            "evidence_tree_sha256": seal.tree_sha256,
            "evidence_seal_sha256": seal.seal_sha256,
        },
    }


def validate_sealed_pair(
    pair_directory: Path,
    *,
    trusted_provenance: object,
) -> dict[str, object]:
    root = Path(pair_directory)
    if not isinstance(trusted_provenance, Mapping):
        _error("aggregate trusted provenance must be an exact object")
    try:
        verify_evidence_seal(root)
    except (EvidenceSealError, OSError) as exc:
        raise FocusedCrashPairValidationError("pair evidence seal rejected") from exc
    receipt = _read_json(root / "pair-receipt.json", "sealed pair receipt")
    children = _sequence(receipt.get("children"), "sealed pair children")
    if (
        receipt.get("schema_version") != 1
        or receipt.get("automatic_retries") != 0
        or receipt.get("replacement_policy") != "none"
        or len(children) != 2
    ):
        _error("sealed pair receipt schema or no-retry contract drifted")
    results: dict[str, Mapping[str, Any]] = {}
    for child in children:
        entry = _mapping(child, "pair child")
        relative = entry.get("path")
        if (
            not isinstance(relative, str)
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or entry.get("arm") not in {"control", "adaptive"}
        ):
            _error("pair child path or arm is invalid")
        arm = str(entry["arm"])
        result = validate_sealed_arm(
            root / relative,
            trusted_provenance=_aggregate_child_provenance(
                trusted_provenance, root / relative, expected_arm=arm
            ),
        )
        if (
            result["arm"] != arm
            or result["pair_id"] != receipt.get("pair_id")
            or entry.get("tree_sha256") != result["child"]["evidence_tree_sha256"]
            or entry.get("seal_sha256") != result["child"]["evidence_seal_sha256"]
            or arm in results
        ):
            _error("pair child identity or seal drifted")
        results[arm] = result
    if set(results) != {"control", "adaptive"}:
        _error("sealed pair does not contain one child per arm")
    control = results["control"]
    adaptive = results["adaptive"]
    if (
        control["build_sha256"] != adaptive["build_sha256"]
        or control["pair_seed"] != adaptive["pair_seed"]
        or control["profile_sha256"] != adaptive["profile_sha256"]
        or control["issuer_public_key_sha256"] != adaptive["issuer_public_key_sha256"]
        or control["epoch1_structure_sha256"] != adaptive["epoch1_structure_sha256"]
    ):
        _error("sealed pair matched inputs or Epoch 1 structure drifted")
    control_tps = int(
        control["scientific_measurements"]["late_window_throughput_milli_tps"]
    )
    adaptive_tps = int(
        adaptive["scientific_measurements"]["late_window_throughput_milli_tps"]
    )
    outcome = (
        "FAVORABLE"
        if adaptive_tps > control_tps
        else "UNFAVORABLE" if adaptive_tps < control_tps else "NEUTRAL"
    )
    return {
        "schema_version": 1,
        "verdict": "PASS",
        "pair_id": receipt.get("pair_id"),
        "scientific_outcome": outcome,
        "retained": True,
        "automatic_retries": 0,
        "replacement_policy": "none",
        "children": [dict(results["control"]), dict(results["adaptive"])],
    }


def validate_sealed_campaign(
    campaign_directory: Path,
    *,
    trusted_provenance: object,
) -> dict[str, object]:
    root = Path(campaign_directory)
    if not isinstance(trusted_provenance, Mapping):
        _error("aggregate trusted provenance must be an exact object")
    try:
        verify_evidence_seal(root)
    except (EvidenceSealError, OSError) as exc:
        raise FocusedCrashPairValidationError(
            "campaign evidence seal rejected"
        ) from exc
    plan = _read_json(root / "plan.json", "campaign plan")
    ledger_path = root / "campaign-ledger.jsonl"
    if ledger_path.is_symlink() or not ledger_path.is_file():
        _error("campaign ledger is absent")
    try:
        ledger = [json.loads(line) for line in ledger_path.read_text().splitlines()]
        campaign_contracts.validate_campaign_ledger(plan, ledger)
    except (json.JSONDecodeError, campaign_contracts.N31CrashPairCampaignError) as exc:
        raise FocusedCrashPairValidationError("campaign ledger rejected") from exc
    children: list[dict[str, object]] = []
    for slot, record in zip(plan["slots"], ledger, strict=True):
        directory = root / "children" / str(slot["slot_id"])
        result = validate_sealed_arm(
            directory,
            trusted_provenance=_aggregate_child_provenance(
                trusted_provenance, directory
            ),
        )
        children.append(
            {
                "slot_id": slot["slot_id"],
                "pair_id": slot["pair_id"],
                "arm": slot["arm"],
                "child_tree_sha256": record["child_tree_sha256"],
                "child_seal_sha256": record["child_seal_sha256"],
                "sealed_child_directory": directory,
                "source_inventory_sha256": result["source_inventory_sha256"],
                "authoritative_commit_identity_sha256": result[
                    "authoritative_commit_identity_sha256"
                ],
                "epoch_identity_sha256": result["epoch_identity_sha256"],
                "ranking_identity_sha256": result["ranking_identity_sha256"],
            }
        )
    try:

        def validate_isolated_child(
            directory: Path, *, trusted_provenance: object
        ) -> Mapping[str, Any]:
            return validate_sealed_arm(
                directory,
                trusted_provenance=_aggregate_child_provenance(
                    trusted_provenance, directory
                ),
            )

        source_blind = campaign_contracts.validate_campaign_source_blind(
            plan,
            children,
            ledger_records=ledger,
            validate_child=validate_isolated_child,
            trusted_provenance=trusted_provenance,
        )
    except campaign_contracts.N31CrashPairCampaignError as exc:
        raise FocusedCrashPairValidationError("source-blind campaign rejected") from exc
    pairs = [
        {
            **dict(pair),
            "retained": True,
        }
        for pair in source_blind["pair_verdicts"]
    ]
    return {
        "schema_version": 1,
        "verdict": (
            "PASS" if source_blind["campaign_acceptance"] == "ACCEPTED" else "FAIL"
        ),
        "terminal_slot_count": len(ledger),
        "pair_count": len(pairs),
        "automatic_retries": 0,
        "replacement_policy": "none",
        "pairs": pairs,
        "figure_eligible": source_blind["figure_eligible"],
        "ledger_head_sha256": source_blind["ledger_head_sha256"],
    }


__all__ = [
    "FocusedCrashPairValidationError",
    "epoch_structural_projection",
    "reconstruct_focused_ranking",
    "validate_sealed_arm",
    "validate_sealed_campaign",
    "validate_sealed_pair",
]
