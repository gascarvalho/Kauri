"""Independent, fail-closed raw validation for one prospective W18 run.

The runner receipt is only a lifecycle record.  This validator reopens the
sealed materialization and all retained raw files; it never treats a successful
process exit as a throughput result.  In particular, a missing independent
raw-validation authority is an ``INCOMPLETE`` result, not a partial pass.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from pathlib import Path
import stat
from typing import Any, Callable, Mapping

from . import operator_capacity_consumption_audit as consumption_audit
from . import operator_capacity_native_replay as native_replay
from . import operator_capacity_v3_backend as backend
from . import operator_capacity_v3_baseline_replay as baseline_replay
from .operator_capacity_preflight import _EXPECTED_QUOTA_PROFILE


N = 31
WINDOW_NS = 30 * 1_000_000_000
_HEX = frozenset("0123456789abcdef")
_RECEIPT = "kauri-n31-operator-capacity-v3-local-shakedown-receipt-v1"
_AUTHORITY = "kauri-n31-operator-capacity-v3-raw-validation-authority-v1"
_RESULT = "kauri-n31-operator-capacity-v3-raw-validation-v1"


class RawValidationError(ValueError):
    pass


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read(path: Path, label: str, maximum: int = 8 * 1024 * 1024) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise RawValidationError(f"{label} is not a readable regular file") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > maximum:
            raise RawValidationError(f"{label} is not a bounded regular file")
        raw = b""
        while len(raw) < before.st_size:
            chunk = os.read(fd, before.st_size - len(raw))
            if not chunk:
                raise RawValidationError(f"{label} changed during read")
            raw += chunk
        if os.read(fd, 1) or os.fstat(fd) != before:
            raise RawValidationError(f"{label} changed during read")
        return raw
    finally:
        os.close(fd)


def _pairs(label: str):
    def decode(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise RawValidationError(f"{label} repeats JSON field {key}")
            result[key] = value
        return result
    return decode


def _json(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_pairs(label),
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RawValidationError(f"{label} is not strict ASCII JSON") from exc
    if not isinstance(value, dict):
        raise RawValidationError(f"{label} is not an object")
    return value


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii") + b"\n"


def _hex(value: object, label: str, length: int = 64) -> str:
    if not isinstance(value, str) or len(value) != length or any(c not in _HEX for c in value):
        raise RawValidationError(f"{label} is not lower-case hexadecimal")
    return value


def _stream_events(path: Path, *, kind: str, source_id: str, run_id: str) -> list[dict[str, Any]]:
    raw = _read(path, f"{source_id} event stream")
    if not raw.endswith(b"\n"):
        raise RawValidationError(f"{source_id} event stream has incomplete framing")
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        event = _json(line, f"{source_id} event")
        if (event.get("event_schema_version") != 1 or event.get("run_id") != run_id or
                event.get("source_kind") != kind or event.get("source_id") != source_id):
            raise RawValidationError(f"{source_id} event envelope differs")
        events.append(event)
    if not events:
        raise RawValidationError(f"{source_id} event stream is empty")
    return events


def _failure(code: str, detail: str) -> dict[str, object]:
    return {"schema_version": 1, "kind": _RESULT, "verdict": "INCOMPLETE",
            "claim_eligible": False, "figure_eligible": False,
            "failure_code": code, "detail": detail,
            "complete_common_commit_count": 0}


def _validate_materialized_identity_for_raw(
    *, root: Path, manager_argv: list[str], manifest: Mapping[str, object],
) -> str:
    """Run the same identity gate on the honest raw-validation path."""
    try:
        value = backend.validate_materialized_public_identity(
            root=root, manager_argv=manager_argv,
            receipt_sha256=manifest.get("identity_parity_receipt_sha256"),
            expected_fingerprint=manifest.get("public_identity_fingerprint"),
            source_revision=manifest.get("revision"))
    except backend.OperatorCapacityV3BackendError as exc:
        raise RawValidationError(str(exc)) from exc
    return value


def _validate_retained_manager_argv_for_raw(
    *, root: Path, manager_argv: list[str], manifest: Mapping[str, object],
) -> None:
    """Bind the post-run argv to the frozen grammar before replaying raw data."""
    try:
        backend.validate_materialized_manager_argv(
            root=root, manager_argv=manager_argv,
            expected_sha256=manifest.get("manager_argv_sha256"))
    except backend.OperatorCapacityV3BackendError as exc:
        raise RawValidationError(str(exc)) from exc


def _validate_materialized_workload_for_raw(manifest: Mapping[str, object]) -> dict[str, object]:
    try:
        return backend.validate_materialized_synthetic_workload(manifest)
    except backend.OperatorCapacityV3BackendError as exc:
        raise RawValidationError(str(exc)) from exc


def _authority(root: Path, receipt_raw: bytes, authority_path: Path | None) -> dict[str, Any]:
    if authority_path is None:
        raise RawValidationError("independent raw-validation authority was not supplied")
    authority_path = Path(authority_path).resolve()
    try:
        authority_path.relative_to(root)
    except ValueError:
        pass
    else:
        raise RawValidationError("raw-validation authority must be retained outside runner-owned output")
    raw = _read(authority_path, "raw-validation authority", 128 * 1024)
    value = _json(raw, "raw-validation authority")
    required = {"schema_version", "kind", "runner_receipt_sha256", "materialization_manifest_sha256",
                "event_stream_sha256", "cpu_quota_contract_sha256", "cpu_quota_launch_sha256",
                "cpu_quota_samples_sha256", "cpu_quota_rounds_sha256",
                "cpu_quota_frozen_contract_sha256",
                "stage_a_verifier_receipt", "stage_b_verifier_receipt", "pins"}
    if set(value) != required or value.get("schema_version") != 1 or value.get("kind") != _AUTHORITY:
        raise RawValidationError("raw-validation authority schema is absent or differs")
    if value["runner_receipt_sha256"] != _sha(receipt_raw):
        raise RawValidationError("raw-validation authority is not bound to runner receipt")
    hashes = value["event_stream_sha256"]
    expected = {"manager", *(f"replica-{replica}" for replica in range(N))}
    if not isinstance(hashes, dict) or set(hashes) != expected:
        raise RawValidationError("raw-validation authority does not pin manager plus 31 streams")
    for label, digest in hashes.items():
        _hex(digest, f"pinned {label} stream hash")
    for key in ("materialization_manifest_sha256", "cpu_quota_contract_sha256",
                "cpu_quota_launch_sha256", "cpu_quota_samples_sha256", "cpu_quota_rounds_sha256"):
        _hex(value[key], key)
    _hex(value["cpu_quota_frozen_contract_sha256"], "cpu_quota_frozen_contract_sha256")
    if (value["stage_a_verifier_receipt"] != "runtime/stage-a-verifier-receipt.json" or
            value["stage_b_verifier_receipt"] != "runtime/stage-b-verifier-receipt.json"):
        raise RawValidationError("raw-validation authority verifier receipt paths are not exact")
    return value


def _validate_cpu(root: Path, authority: Mapping[str, Any], *, start: int, end: int,
                  cluster_physical_regime: str | None = None) -> None:
    frozen_raw = _read(root / "runtime/frozen-cpu-quota-contract.json", "frozen CPU quota contract")
    contract_raw = _read(root / "runtime/cpu-quota-contract.json", "CPU quota contract")
    launch_raw = _read(root / "runtime/cpu-quota-launch.json", "CPU quota launch")
    sample_path = root / "raw/cpu-quota-samples.jsonl"
    rounds_path = root / "raw/cpu-quota-monitor-rounds.jsonl"
    if _sha(frozen_raw) != authority["cpu_quota_frozen_contract_sha256"]:
        raise RawValidationError("frozen CPU quota contract differs from authority pin")
    if _sha(contract_raw) != authority["cpu_quota_contract_sha256"]:
        raise RawValidationError("CPU quota contract differs from authority pin")
    if _sha(launch_raw) != authority["cpu_quota_launch_sha256"]:
        raise RawValidationError("CPU quota launch differs from authority pin")
    frozen_contract = _json(frozen_raw, "frozen CPU quota contract")
    contract = _json(contract_raw, "CPU quota contract")
    launch = _json(launch_raw, "CPU quota launch")
    if _canonical(frozen_contract) != _canonical(contract):
        raise RawValidationError("runtime CPU quota contract differs semantically from frozen input")
    expected = _EXPECTED_QUOTA_PROFILE
    if cluster_physical_regime is not None:
        from .operator_capacity_v3_cluster_profiles import expected_quota_profile
        expected = expected_quota_profile(cluster_physical_regime)
    if _canonical(contract) != _canonical(expected):
        raise RawValidationError("CPU quota contract differs from frozen W18 profile")
    assignments = contract.get("assignments")
    if (set(contract) != {"schema_version", "contract_id", "enabled", "figure_eligible",
                          "launcher", "manager_visibility", "sampling_interval_ms", "base_profile_id",
                          "base_profile_sha256", "base_profile_canonical_sha256", "assignments"} or
            contract["schema_version"] != 1 or contract["enabled"] is not True or
            contract["figure_eligible"] is not False or contract["manager_visibility"] != "none" or
            contract["launcher"] != "systemd-user-scope-cpu-quota-v1" or
            contract["sampling_interval_ms"] != 1000 or
            contract["contract_id"] != expected["contract_id"] or
            contract["base_profile_id"] != expected["base_profile_id"] or
            not isinstance(assignments, list) or len(assignments) != N):
        raise RawValidationError("CPU quota contract is not the frozen 31-replica profile")
    for replica, assignment in enumerate(assignments):
        expected_class = expected["assignments"][replica]["capacity_class"]
        expected_percent = expected["assignments"][replica]["cpu_quota_percent"]
        if (not isinstance(assignment, dict) or
                assignment != {"replica_id": replica, "capacity_class": expected_class,
                               "cpu_quota_percent": expected_percent}):
            raise RawValidationError("CPU quota assignment differs from frozen profile")
    launches = launch.get("replicas")
    if (set(launch) != {"schema_version", "launcher", "contract_id", "contract_sha256",
                       "manager_visibility", "replicas"} or launch["schema_version"] != 1 or
            launch["launcher"] != contract["launcher"] or launch["contract_id"] != contract["contract_id"] or
            launch["contract_sha256"] != _sha(frozen_raw) or
            launch["manager_visibility"] != "none" or not isinstance(launches, list) or
            len(launches) != N):
        raise RawValidationError("CPU launch is not bound to the frozen contract")
    launched: dict[int, dict[str, Any]] = {}
    for row in launches:
        if not isinstance(row, dict) or set(row) != {
            "replica_id", "cpu_quota_percent", "unit", "control_group", "cpu_stat_path",
            "owned_pid", "owned_pgid", "cgroup_pids", "active_state", "sub_state",
            "cpu_quota_per_second_usec",
        }:
            raise RawValidationError("CPU launch row schema drifted")
        replica = row["replica_id"]
        if (type(replica) is not int or replica not in range(N) or replica in launched or
                row["cpu_quota_percent"] != expected["assignments"][replica]["cpu_quota_percent"] or
                row["cpu_quota_per_second_usec"] != row["cpu_quota_percent"] * 10_000 or
                row["active_state"] != "active" or row["sub_state"] not in {"running", "start"} or
                not isinstance(row["unit"], str) or not row["unit"] or
                not isinstance(row["control_group"], str) or not row["control_group"].startswith("/") or
                not isinstance(row["cpu_stat_path"], str) or not row["cpu_stat_path"].endswith("/cpu.stat") or
                type(row["owned_pid"]) is not int or row["owned_pid"] <= 0 or
                row["owned_pgid"] != row["owned_pid"] or
                not isinstance(row["cgroup_pids"], list) or row["owned_pid"] not in row["cgroup_pids"]):
            raise RawValidationError("CPU launch row does not prove owned active quota")
        launched[replica] = row
    if set(launched) != set(range(N)) or len({row["unit"] for row in launched.values()}) != N:
        raise RawValidationError("CPU launches do not own exactly 31 distinct scopes")
    if _sha(_read(sample_path, "CPU quota samples")) != authority["cpu_quota_samples_sha256"]:
        raise RawValidationError("CPU quota samples differ from authority pin")
    if _sha(_read(rounds_path, "CPU quota monitor rounds")) != authority["cpu_quota_rounds_sha256"]:
        raise RawValidationError("CPU quota monitor rounds differ from authority pin")
    by_time: dict[int, dict[int, dict[str, int]]] = defaultdict(dict)
    stat_keys: set[str] | None = None
    for line in _read(sample_path, "CPU quota samples").splitlines():
        row = _json(line, "CPU quota sample")
        replica, timestamp = row.get("replica_id"), row.get("source_monotonic_ns")
        if (set(row) != {"schema_version", "source_monotonic_ns", "replica_id", "cpu_quota_percent",
                         "unit", "control_group", "cpu_stat_path", "cpu_quota_per_second_usec",
                         "active_state", "sub_state", "cpu_stat"} or
                row.get("schema_version") != 1 or type(replica) is not int or replica not in launched or
                type(timestamp) is not int or timestamp <= 0 or replica in by_time[timestamp]):
            raise RawValidationError("CPU quota sample schema or frozen assignment differs")
        launch_row = launched[replica]
        stat_row = row.get("cpu_stat")
        if (row["cpu_quota_percent"] != launch_row["cpu_quota_percent"] or
                row["cpu_quota_per_second_usec"] != launch_row["cpu_quota_per_second_usec"] or
                any(row[key] != launch_row[key] for key in ("unit", "control_group", "cpu_stat_path")) or
                row["active_state"] != "active" or row["sub_state"] not in {"running", "start"} or
                not isinstance(stat_row, dict) or
                not {"usage_usec", "user_usec", "system_usec"}.issubset(stat_row) or
                any(type(value) is not int or value < 0 for value in stat_row.values())):
            raise RawValidationError("CPU sample differs from owned scope or accounting")
        if stat_keys is None:
            stat_keys = set(stat_row)
        elif set(stat_row) != stat_keys:
            raise RawValidationError("CPU accounting keys changed during measurement")
        by_time[timestamp][replica] = stat_row
    timestamps = sorted(by_time)
    if (not timestamps or any(set(by_time[timestamp]) != set(range(N)) for timestamp in timestamps)):
        raise RawValidationError("CPU sample round does not cover all 31 replicas")
    interval = 1_000_000_000
    before = [timestamp for timestamp in timestamps if timestamp <= start]
    after = [timestamp for timestamp in timestamps if timestamp >= end]
    inside = [timestamp for timestamp in timestamps if start <= timestamp <= end]
    if (not before or not after or len(inside) < 2 or
            start - before[-1] > 3 * interval or after[0] - end > 3 * interval):
        raise RawValidationError("CPU sampling does not span the complete E1 window")
    relevant = [timestamp for timestamp in timestamps if before[-1] <= timestamp <= after[0]]
    if any(right - left > 3 * interval for left, right in zip(relevant, relevant[1:])):
        raise RawValidationError("CPU sampling has a gap exceeding three frozen intervals")
    for replica in range(N):
        previous: dict[str, int] | None = None
        for timestamp in relevant:
            current = by_time[timestamp][replica]
            if previous is not None and any(current[key] < previous[key] for key in stat_keys or ()):
                raise RawValidationError("CPU accounting counter regressed")
            previous = current
        if by_time[after[0]][replica]["usage_usec"] <= by_time[before[-1]][replica]["usage_usec"]:
            raise RawValidationError("replica CPU usage did not advance across E1 measurement")
    rounds = [_json(line, "CPU quota monitor round") for line in _read(rounds_path, "CPU quota monitor rounds").splitlines()]
    round_keys = {"schema_version", "round_ordinal", "scheduled_monotonic_ns", "started_monotonic_ns",
                  "sample_monotonic_ns", "finished_monotonic_ns", "duration_ns",
                  "start_lateness_ns", "completion_overrun_ns"}
    if len(rounds) != len(timestamps):
        raise RawValidationError("CPU monitor round count differs from full sample groups")
    for ordinal, row in enumerate(rounds):
        if (set(row) != round_keys or row.get("schema_version") != 1 or
                row.get("round_ordinal") != ordinal or row.get("sample_monotonic_ns") != timestamps[ordinal] or
                any(type(row.get(key)) is not int or row[key] < 0 for key in round_keys - {"schema_version"}) or
                row["finished_monotonic_ns"] < row["started_monotonic_ns"] or
                row["duration_ns"] != row["finished_monotonic_ns"] - row["started_monotonic_ns"]):
            raise RawValidationError("CPU monitor round is not consistent with raw samples")


def validate_operator_capacity_v3_raw(
    root: Path, *, authority_path: Path | None = None,
    independently_recompute_verifiers: Callable[[Path, Mapping[str, Any]], Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """Return a non-claim-bearing complete result, or an honest incomplete one."""
    try:
        root = Path(root).resolve()
        receipt_path = root / "runtime/local-shakedown-receipt.json"
        receipt_raw = _read(receipt_path, "runner receipt", 64 * 1024)
        receipt = _json(receipt_raw, "runner receipt")
        receipt_keys = {"schema_version", "kind", "verdict", "claim_eligible", "figure_eligible",
                        "automatic_retries", "execution_request_sha256", "manager_exit_code", "failure",
                        "cleanup", "fresh_native_stage_a_receipt_sha256", "raw_validation_required",
                        "e1_measurement_window", "manager_exit_code_after_cleanup",
                        "manager_success_terminal_verified"}
        if (set(receipt) != receipt_keys or receipt.get("schema_version") != 1 or receipt.get("kind") != _RECEIPT or
                receipt.get("verdict") != "PROCESS_COMPLETED_PENDING_RAW_VALIDATION" or
                receipt.get("claim_eligible") is not False or receipt.get("figure_eligible") is not False or
                receipt.get("automatic_retries") != 0 or receipt.get("manager_exit_code") not in (None, 0) or
                not isinstance(receipt.get("manager_exit_code_after_cleanup"), int) or
                receipt.get("manager_success_terminal_verified") is not True or
                receipt.get("failure") is not None or receipt.get("raw_validation_required") is not True):
            raise RawValidationError("runner receipt is not one successful no-retry pending-validation run")
        authority = _authority(root, receipt_raw, authority_path)
        if independently_recompute_verifiers is None:
            raise RawValidationError("independent Stage-A and Stage-B verifier recomputation was not supplied")
        return _validate_evidence(root, receipt=receipt, authority=authority,
            independently_recompute_verifiers=independently_recompute_verifiers)
    except (RawValidationError, consumption_audit.ConsumptionAuditError, native_replay.NativeReplayError,
            baseline_replay.BaselineReplayError, KeyError, TypeError) as exc:
        return _failure("RAW_CONTRACT_INCOMPLETE", str(exc))


def _validate_evidence(root, *, receipt, authority, independently_recompute_verifiers,
                       cluster_physical_regime=None):
    """Shared protocol/metric proof; callers must close their own launch authority."""
    manifest_raw = _read(root / "materialization-manifest.json", "materialization manifest", 256 * 1024)
    if _sha(manifest_raw) != authority["materialization_manifest_sha256"]:
        raise RawValidationError("materialization manifest differs from authority pin")
    manifest = _json(manifest_raw, "materialization manifest")
    if manifest.get("verdict") != "MATERIALIZED_NO_EXECUTION" or manifest.get("protocol") != {"N": 31, "Q": 21, "tree_count": 21}:
        raise RawValidationError("materialization manifest is not frozen W18 N31")
    workload = _validate_materialized_workload_for_raw(manifest)
    artifacts = manifest.get("artifact_sha256")
    if not isinstance(artifacts, dict):
        raise RawValidationError("materialization artifacts are absent")
    required_config = {"config/hotstuff.gen.conf", *(f"config/replica-{replica}.conf" for replica in range(N))}
    if not required_config.issubset(artifacts) or any(
            _sha(_read(root / relative, f"materialized {relative}")) != artifacts[relative]
            for relative in required_config):
        raise RawValidationError("effective synthetic workload configuration differs from manifest")
    try:
        try:
            manager_argv = json.loads(_read(root / "runtime/manager-argv.json", "retained manager argv").decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RawValidationError("retained manager argv is invalid") from exc
        if not isinstance(manager_argv, list) or not all(isinstance(value, str) for value in manager_argv):
            raise RawValidationError("retained manager argv is invalid")
        _validate_retained_manager_argv_for_raw(
            root=root, manager_argv=manager_argv, manifest=manifest)
        backend.validate_synthetic_main_config(
            _read(root / "config/hotstuff.gen.conf", "materialized main config"), root=root,
            manager_argv=manager_argv)
        public_identity_fingerprint = _validate_materialized_identity_for_raw(
            root=root, manager_argv=manager_argv, manifest=manifest)
    except backend.OperatorCapacityV3BackendError as exc:
        raise RawValidationError(str(exc)) from exc
    if (_sha(_read(root / "config/stage-a-envelope.wire", "Stage-A envelope"))
            != manifest.get("stage_a_envelope_sha256") or
            _sha(_read(root / str(authority["stage_a_verifier_receipt"]), "Stage-A verifier receipt"))
            != manifest.get("stage_a_verifier_receipt_sha256")):
        raise RawValidationError("Stage-A materialization authority differs from manifest")
    window = receipt["e1_measurement_window"]
    if not isinstance(window, dict) or set(window) != {"activated_replica_ids", "all_replica_e1_activation_events", "all_replica_e1_activation_monotonic_ns", "post_e1_window_start_monotonic_ns", "post_e1_window_end_monotonic_ns", "post_e1_commit"}:
        raise RawValidationError("runner receipt lacks the frozen complete E1 window schema")
    start, end = window["post_e1_window_start_monotonic_ns"], window["post_e1_window_end_monotonic_ns"]
    if (window["activated_replica_ids"] != list(range(N)) or type(start) is not int or type(end) is not int or end - start != WINDOW_NS or window["all_replica_e1_activation_monotonic_ns"] != start):
        raise RawValidationError("E1 window is not exact all-31 activation plus fixed 30 seconds")
    activation_events = window["all_replica_e1_activation_events"]
    if not isinstance(activation_events, list) or len(activation_events) != N:
        raise RawValidationError("runner receipt lacks all 31 E1 activation events")
    activation_digests = {event.get("epoch_digest") for event in activation_events if isinstance(event, dict)}
    if len(activation_digests) != 1:
        raise RawValidationError("runner E1 activation events do not share one exact digest")
    epoch1_digest = _hex(next(iter(activation_digests)), "E1 activation digest")
    run_id = authority["pins"].get("run_id") if isinstance(authority["pins"], dict) else None
    if not isinstance(run_id, str) or not run_id:
        raise RawValidationError("raw-validation authority pins lack run ID")
    manager_raw = _read(root / "raw/manager-events.jsonl", "manager event stream")
    if _sha(manager_raw) != authority["event_stream_sha256"]["manager"]:
        raise RawValidationError("manager stream differs from authority pin")
    manager_events = _stream_events(root / "raw/manager-events.jsonl", kind="adaptation_manager",
                   source_id="adaptive-manager", run_id=run_id)
    streams: dict[int, list[dict[str, Any]]] = {}
    for replica in range(N):
        path = root / f"raw/replica-{replica}.jsonl"
        raw = _read(path, f"replica-{replica} event stream")
        if _sha(raw) != authority["event_stream_sha256"][f"replica-{replica}"]:
            raise RawValidationError(f"replica-{replica} stream differs from authority pin")
        streams[replica] = _stream_events(path, kind="replica", source_id=f"replica-{replica}", run_id=run_id)
    pins = authority["pins"]
    if not isinstance(pins, dict):
        raise RawValidationError("raw-validation authority pins are invalid")
    chain = consumption_audit.audit_consumption_chain(
        stage_a_wire=root / "config/stage-a-envelope.wire", stage_b_wire=root / "raw/stage-b-authorization.wire",
        successor_bundle=root / "transitions/e0-to-e1-operator-capacity/successor.bundle", consumption_record=root / "raw/consumption.json",
        stage_a_verifier_receipt=root / str(authority["stage_a_verifier_receipt"]),
        stage_b_verifier_receipt=root / str(authority["stage_b_verifier_receipt"]), pins=pins)
    baseline = baseline_replay.replay_operator_capacity_baseline(
        manager_events, consumption_record=_json(
            _read(root / "raw/consumption.json", "consumption record"), "consumption record"),
        manager_argv=manager_argv)
    recomputed = independently_recompute_verifiers(root, authority)
    if (not isinstance(recomputed, Mapping) or set(recomputed) != {"stage_a_receipt_sha256", "stage_b_receipt_sha256"} or
            recomputed["stage_a_receipt_sha256"] != _sha(_read(root / str(authority["stage_a_verifier_receipt"]), "Stage-A verifier receipt")) or
            recomputed["stage_b_receipt_sha256"] != _sha(_read(root / str(authority["stage_b_verifier_receipt"]), "Stage-B verifier receipt"))):
        raise RawValidationError("independent native verifier recomputation differs from retained receipts")
    commits = native_replay.replay_post_e1_common_commits(streams, run_id=run_id,
        epoch1_digest=epoch1_digest, window_start_ns=start, window_end_ns=end)
    if any(commit.transaction_count != 1 for commit in commits):
        raise RawValidationError("counted E1 commits do not retain one synthetic command each")
    _validate_cpu(root, authority, start=start, end=end,
                  cluster_physical_regime=cluster_physical_regime)
    cleanup = receipt["cleanup"]
    if not isinstance(cleanup, dict) or cleanup.get("stop_quota_monitor") != "completed" or cleanup.get("terminate_manager_and_replicas") != "completed" or not isinstance(cleanup.get("terminate_owned_replica_scopes"), dict) or not isinstance(cleanup.get("verify_scope_cleanup"), dict) or cleanup["terminate_owned_replica_scopes"].get("complete") is not True or cleanup["verify_scope_cleanup"].get("complete") is not True or len(cleanup["terminate_owned_replica_scopes"].get("units", [])) != N or len(cleanup["verify_scope_cleanup"].get("units", [])) != N:
        raise RawValidationError("runner cleanup receipt is incomplete")
    return {"schema_version": 1, "kind": _RESULT, "verdict": "COMPLETE_NO_CLAIM",
            "claim_eligible": False, "figure_eligible": False, "automatic_retries": 0,
            "complete_common_commit_count": len(commits), "common_commits": [item.__dict__ for item in commits],
            "measurement_window": {"start_monotonic_ns": start, "end_monotonic_ns": end},
            "workload_boundary": "all-31 common committed-block cadence under a pinned replica-local synthetic drive; transaction_count is a nonempty-slot invariant, not TPS or unique transactions",
            "synthetic_workload": workload,
            "public_identity_fingerprint": public_identity_fingerprint,
            "consumption_chain": chain, "baseline_replay": baseline}
