#!/usr/bin/env python3
"""Run the seven-replica fixed-tree leaf-crash development smoke."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import statistics
import subprocess
import sys
import time
from typing import Iterable, Mapping, NamedTuple, Sequence
import uuid
from xml.sax.saxutils import escape


MARKER_TOKEN = "KAURI_DEMO"
REPUTATION_MARKER_TOKEN = "KAURI_REPUTATION"
MANAGER_PORT = 50500
FORBIDDEN_COMMAND_TOKENS = frozenset(
    {"killall", "pkill", "sudo", "ssh"}
)
FATAL_TEXT = re.compile(
    r"(?:terminate called|segmentation fault|uncaught exception|"
    r"fatal error|\[fatal\]|assertion .* failed|abort(?:ed)?)",
    re.IGNORECASE,
)
PROTOCOL_REJECTION_TEXT = re.compile(
    r"(?:(?:invalid|malformed) adaptive epoch consensus message|"
    r"rejecting (?:malformed|invalid)\b[^\n]*|dropping invalid block\b)",
    re.IGNORECASE,
)
FULL_HASH = re.compile(r"^[0-9a-fA-F]{64}$")


class SmokeError(RuntimeError):
    """A deterministic smoke precondition or validation failure."""


class CommitEvent(NamedTuple):
    source_replica: int
    replica: int
    height: int
    block_hash: str
    tx_count: int
    monotonic_ns: int
    epoch: int
    tree: int
    root: int


class ThroughputBucket(NamedTuple):
    bucket_index: int
    bucket_start_s: float
    bucket_end_s: float
    bucket_width_s: float
    commit_count: int
    tx_count: int
    tps: float
    phase: str
    is_grace: bool


class ThroughputVerdict(NamedTuple):
    classification: str
    baseline_median_tps: float
    post_median_tps: float
    recovery_ratio: float
    max_zero_interval_s: float
    reasons: tuple[str, ...]


class ReputationVerdict(NamedTuple):
    passed: bool
    reasons: tuple[str, ...]
    updates: tuple[Mapping[str, object], ...]


def calibrate_event_clock(
    anchor_event_ns: int, observed_runner_ns: int
) -> int:
    """Return the additive mapping from runner to C++ event-clock time."""
    if anchor_event_ns < 0 or observed_runner_ns < 0:
        raise SmokeError("monotonic clock samples cannot be negative")
    return anchor_event_ns - observed_runner_ns


def runner_to_event_clock(runner_ns: int, offset_ns: int) -> int:
    """Map one runner monotonic timestamp into the C++ event-clock domain."""
    mapped = runner_ns + offset_ns
    if runner_ns < 0 or mapped < 0:
        raise SmokeError("mapped monotonic timestamp cannot be negative")
    return mapped


class ProcessRecord:
    def __init__(
        self,
        name: str,
        pid: int,
        pgid: int,
        command: Sequence[str],
        log_path: Path,
        process: subprocess.Popen[bytes],
        log_handle: object,
        *,
        replica: int | None = None,
    ) -> None:
        self.name = name
        self.pid = pid
        self.pgid = pgid
        self.command = tuple(command)
        self.log_path = log_path
        self.process = process
        self.log_handle = log_handle
        self.replica = replica

    def manifest_entry(self) -> dict[str, object]:
        return_code = self.process.poll()
        entry: dict[str, object] = {
            "name": self.name,
            "pid": self.pid,
            "pgid": self.pgid,
            "command": list(self.command),
            "log": str(self.log_path),
            "replica": self.replica,
            "running": return_code is None,
        }
        if return_code is not None:
            entry["exit"] = process_exit_status(return_code)
        return entry


def parse_commit_marker(
    line: str, source_replica: int
) -> CommitEvent | None:
    """Parse an authoritative real-commit marker from one replica log."""
    offset = line.find(MARKER_TOKEN)
    if offset < 0:
        return None
    payload = line[offset + len(MARKER_TOKEN) :].strip()
    try:
        tokens = shlex.split(payload)
    except ValueError as exc:
        raise SmokeError(f"invalid commit marker quoting: {exc}") from exc
    if not tokens or tokens[0] != "commit":
        return None
    fields: dict[str, str] = {}
    for token in tokens[1:]:
        if "=" not in token:
            raise SmokeError(f"invalid commit marker token: {token!r}")
        key, value = token.split("=", 1)
        if not key or not value or key in fields:
            raise SmokeError(f"invalid commit marker field: {token!r}")
        fields[key] = value
    required = {
        "replica",
        "height",
        "epoch",
        "tree",
        "root",
        "hash",
        "tx_count",
        "monotonic_ns",
    }
    missing = sorted(required - set(fields))
    if missing:
        raise SmokeError(
            "commit marker is missing required fields: " + ", ".join(missing)
        )
    try:
        replica = int(fields["replica"])
        height = int(fields["height"])
        tx_count = int(fields["tx_count"])
        monotonic_ns = int(fields["monotonic_ns"])
        epoch = int(fields["epoch"])
        tree = int(fields["tree"])
        root = int(fields["root"])
    except ValueError as exc:
        raise SmokeError("commit marker contains a non-integer field") from exc
    if replica != source_replica:
        raise SmokeError(
            f"source replica {source_replica} logged marker for replica {replica}"
        )
    if min(replica, height, tx_count, monotonic_ns, epoch, tree, root) < 0:
        raise SmokeError("commit marker contains a negative field")
    block_hash = fields["hash"].lower()
    if not FULL_HASH.fullmatch(block_hash):
        raise SmokeError("commit marker hash is not a full 256-bit hex value")
    return CommitEvent(
        source_replica,
        replica,
        height,
        block_hash,
        tx_count,
        monotonic_ns,
        epoch,
        tree,
        root,
    )


def parse_commit_events(text: str, source_replica: int) -> list[CommitEvent]:
    events: list[CommitEvent] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        try:
            event = parse_commit_marker(line, source_replica)
        except SmokeError as exc:
            raise SmokeError(
                f"replica {source_replica} line {line_number}: {exc}"
            ) from exc
        if event is not None:
            events.append(event)
    return events


def validate_and_deduplicate_commits(
    events: Sequence[CommitEvent],
) -> list[CommitEvent]:
    """Deduplicate by full hash and reject every height/hash ambiguity."""
    by_hash: dict[str, CommitEvent] = {}
    by_height: dict[int, str] = {}
    for event in events:
        known_hash = by_height.get(event.height)
        if known_hash is not None and known_hash != event.block_hash:
            raise SmokeError(
                f"height {event.height} has conflicting hashes "
                f"{known_hash} and {event.block_hash}"
            )
        by_height[event.height] = event.block_hash
        known_event = by_hash.get(event.block_hash)
        if known_event is not None:
            if known_event.height != event.height:
                raise SmokeError(
                    f"hash {event.block_hash} appears at conflicting heights"
                )
            known_metadata = (
                known_event.tx_count,
                known_event.epoch,
                known_event.tree,
                known_event.root,
            )
            event_metadata = (
                event.tx_count,
                event.epoch,
                event.tree,
                event.root,
            )
            if event_metadata != known_metadata:
                raise SmokeError(
                    f"hash {event.block_hash} at height {event.height} has "
                    "conflicting authoritative metadata"
                )
            if event.monotonic_ns < known_event.monotonic_ns:
                by_hash[event.block_hash] = event
            continue
        by_hash[event.block_hash] = event
    return sorted(
        by_hash.values(),
        key=lambda event: (event.monotonic_ns, event.height),
    )


def validate_survivor_agreement(
    events_by_replica: Mapping[int, Sequence[CommitEvent]],
    survivor_ids: Sequence[int],
) -> None:
    """Reject two survivor logs assigning different hashes to one height."""
    observed: dict[int, tuple[str, int]] = {}
    for replica in survivor_ids:
        if replica not in events_by_replica:
            raise SmokeError(f"survivor replica {replica} has no commit log")
        for event in validate_and_deduplicate_commits(
            events_by_replica[replica]
        ):
            if event.replica != replica:
                raise SmokeError(
                    f"survivor replica {replica} contains another source"
                )
            previous = observed.get(event.height)
            if previous is not None and previous[0] != event.block_hash:
                raise SmokeError(
                    f"survivors do not agree at height {event.height}: "
                    f"replica {previous[1]}={previous[0]}, "
                    f"replica {replica}={event.block_hash}"
                )
            observed[event.height] = (event.block_hash, replica)


def common_commits(
    events_by_replica: Mapping[int, Sequence[CommitEvent]],
    replica_ids: Sequence[int],
    *,
    minimum_monotonic_ns: int | None = None,
) -> list[tuple[int, str]]:
    maps: list[dict[int, str]] = []
    for replica in replica_ids:
        events = events_by_replica.get(replica, ())
        maps.append(
            {
                event.height: event.block_hash
                for event in validate_and_deduplicate_commits(events)
                if minimum_monotonic_ns is None
                or event.monotonic_ns >= minimum_monotonic_ns
            }
        )
    if not maps or any(not values for values in maps):
        return []
    shared_heights = set(maps[0])
    for values in maps[1:]:
        shared_heights &= set(values)
    result: list[tuple[int, str]] = []
    for height in sorted(shared_heights):
        hashes = {values[height] for values in maps}
        if len(hashes) != 1:
            raise SmokeError(f"common height {height} has conflicting hashes")
        result.append((height, hashes.pop()))
    return result


def build_throughput_buckets(
    events: Sequence[CommitEvent],
    *,
    measurement_start_ns: int,
    bucket_width_s: float,
    baseline_bucket_count: int,
    grace_bucket_count: int,
    post_bucket_count: int,
) -> list[ThroughputBucket]:
    if bucket_width_s <= 0:
        raise SmokeError("bucket width must be positive")
    if min(
        baseline_bucket_count, grace_bucket_count, post_bucket_count
    ) < 0:
        raise SmokeError("bucket counts cannot be negative")
    total = (
        baseline_bucket_count + grace_bucket_count + post_bucket_count
    )
    if total == 0:
        raise SmokeError("at least one throughput bucket is required")
    width_ns = int(bucket_width_s * 1_000_000_000)
    end_ns = measurement_start_ns + total * width_ns
    tx_counts = [0] * total
    commit_counts = [0] * total
    for event in validate_and_deduplicate_commits(events):
        if not (measurement_start_ns <= event.monotonic_ns < end_ns):
            continue
        index = (event.monotonic_ns - measurement_start_ns) // width_ns
        tx_counts[index] += event.tx_count
        commit_counts[index] += 1
    buckets: list[ThroughputBucket] = []
    grace_end = baseline_bucket_count + grace_bucket_count
    for index in range(total):
        if index < baseline_bucket_count:
            phase = "baseline"
        elif index < grace_end:
            phase = "grace"
        else:
            phase = "post"
        buckets.append(
            ThroughputBucket(
                index,
                index * bucket_width_s,
                (index + 1) * bucket_width_s,
                bucket_width_s,
                commit_counts[index],
                tx_counts[index],
                tx_counts[index] / bucket_width_s,
                phase,
                phase == "grace",
            )
        )
    return buckets


def maximum_observer_commit_gap_s(
    events: Sequence[CommitEvent],
    *,
    window_start_ns: int,
    window_end_ns: int,
) -> float:
    """Return the longest exact commit-free interval in one closed window."""
    if window_start_ns < 0 or window_end_ns < 0:
        raise SmokeError("commit-gap window cannot be negative")
    if window_end_ns < window_start_ns:
        raise SmokeError("commit-gap window end precedes its start")
    timestamps = [
        event.monotonic_ns
        for event in validate_and_deduplicate_commits(events)
        if window_start_ns <= event.monotonic_ns <= window_end_ns
    ]
    boundaries = [window_start_ns, *timestamps, window_end_ns]
    return max(
        (right - left) / 1_000_000_000
        for left, right in zip(boundaries, boundaries[1:])
    )


def evaluate_throughput(
    buckets: Sequence[ThroughputBucket],
    *,
    observer_events: Sequence[CommitEvent],
    measurement_start_ns: int,
    minimum_recovery_ratio: float,
    max_stall_s: float,
) -> ThroughputVerdict:
    if not 0 <= minimum_recovery_ratio <= 1:
        raise SmokeError("minimum recovery ratio must be in [0, 1]")
    if max_stall_s < 0:
        raise SmokeError("maximum stall must be non-negative")
    baseline = [bucket.tps for bucket in buckets if bucket.phase == "baseline"]
    post = [bucket.tps for bucket in buckets if bucket.phase == "post"]
    if not baseline or not post:
        raise SmokeError("baseline and post-crash buckets are required")
    baseline_median = float(statistics.median(baseline))
    post_median = float(statistics.median(post))
    recovery_ratio = (
        post_median / baseline_median if baseline_median > 0 else 0.0
    )
    monitored = [
        bucket for bucket in buckets if bucket.phase in {"grace", "post"}
    ]
    if not monitored:
        raise SmokeError("grace or post-crash buckets are required")
    monitored_start_ns = measurement_start_ns + round(
        min(bucket.bucket_start_s for bucket in monitored) * 1_000_000_000
    )
    monitored_end_ns = measurement_start_ns + round(
        max(bucket.bucket_end_s for bucket in monitored) * 1_000_000_000
    )
    maximum_zero_run = maximum_observer_commit_gap_s(
        observer_events,
        window_start_ns=monitored_start_ns,
        window_end_ns=monitored_end_ns,
    )

    reasons: list[str] = []
    classification = "PASS"
    if baseline_median <= 0:
        classification = "FAIL"
        reasons.append("baseline median throughput is zero")
    if maximum_zero_run > max_stall_s:
        classification = "FAIL"
        reasons.append(
            f"zero-throughput interval {maximum_zero_run:.3f}s exceeds "
            f"{max_stall_s:.3f}s"
        )
    elif any(value <= 0 for value in post):
        classification = "DEGRADED"
        reasons.append("one or more stable post-crash buckets have no progress")
    if (
        classification != "FAIL"
        and recovery_ratio + 1e-12 < minimum_recovery_ratio
    ):
        classification = "DEGRADED"
        reasons.append(
            f"post/baseline median ratio {recovery_ratio:.3f} is below "
            f"{minimum_recovery_ratio:.3f}"
        )
    return ThroughputVerdict(
        classification,
        baseline_median,
        post_median,
        recovery_ratio,
        maximum_zero_run,
        tuple(reasons),
    )


def inject_crashes(
    records_by_replica: Mapping[int, object],
    crash_targets: Sequence[int],
    *,
    killpg: object = os.killpg,
) -> list[dict[str, object]]:
    """Send SIGKILL only to the explicitly registered crash targets."""
    if len(set(crash_targets)) != len(crash_targets):
        raise SmokeError("crash targets must be unique")
    own_group = os.getpgrp()
    selected: list[tuple[int, object]] = []
    for replica in crash_targets:
        record = records_by_replica.get(replica)
        if record is None:
            raise SmokeError(f"crash target replica {replica} is not registered")
        pid = int(getattr(record, "pid"))
        pgid = int(getattr(record, "pgid"))
        if pgid <= 1 or pgid == own_group:
            raise SmokeError(
                f"refusing unsafe process group {pgid} for replica {replica}"
            )
        try:
            live_pgid = os.getpgid(pid)
        except ProcessLookupError as exc:
            if hasattr(record, "process"):
                raise SmokeError(
                    f"crash target replica {replica} is no longer running"
                ) from exc
            # Unit-test records do not own live operating-system processes.
            live_pgid = pgid
        if live_pgid != pgid:
            raise SmokeError(
                f"stored/live PGID mismatch for replica {replica}: "
                f"stored={pgid}, live={live_pgid}"
            )
        process = getattr(record, "process", None)
        if process is not None and process.poll() is not None:
            raise SmokeError(f"crash target replica {replica} already exited")
        selected.append((replica, record))
    events: list[dict[str, object]] = []
    for replica, record in selected:
        requested_ns = time.monotonic_ns()
        killpg(int(getattr(record, "pgid")), signal.SIGKILL)  # type: ignore[operator]
        events.append(
            {
                "replica": replica,
                "pid": int(getattr(record, "pid")),
                "pgid": int(getattr(record, "pgid")),
                "signal": "SIGKILL",
                "signal_number": int(signal.SIGKILL),
                "requested_utc": dt.datetime.now(
                    dt.timezone.utc
                ).isoformat(),
                "requested_monotonic_ns": requested_ns,
                "epoch": 0,
                "tree": 0,
                "role": "leaf",
            }
        )
    return events


def validate_process_exits(
    exit_observations: Sequence[Mapping[str, object]],
    expected_crashes: Sequence[int],
) -> None:
    expected = set(expected_crashes)
    observed_expected: set[int] = set()
    for observation in exit_observations:
        if observation.get("phase") == "cleanup":
            continue
        replica_value = observation.get("replica")
        if replica_value is None:
            raise SmokeError(
                f"non-replica process {observation.get('name', 'unknown')} "
                "exited before cleanup"
            )
        replica = int(replica_value)
        if replica not in expected:
            raise SmokeError(f"survivor replica {replica} exited early")
        if int(observation.get("signal_number") or 0) != int(signal.SIGKILL):
            raise SmokeError(
                f"expected crash replica {replica} did not exit by SIGKILL"
            )
        observed_expected.add(replica)
    missing = sorted(expected - observed_expected)
    if missing:
        raise SmokeError(f"expected crash exits were not observed: {missing}")


def evaluate_reputation_log(
    text: str, replica_ids: Sequence[int]
) -> ReputationVerdict:
    membership = set(replica_ids)
    reasons: list[str] = []
    updates: list[Mapping[str, object]] = []
    scores: dict[int, int] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        offset = line.find(REPUTATION_MARKER_TOKEN)
        if offset < 0:
            continue
        payload = line[offset + len(REPUTATION_MARKER_TOKEN) :].strip()
        try:
            tokens = shlex.split(payload)
        except ValueError as exc:
            reasons.append(f"line {line_number}: invalid quoting: {exc}")
            continue
        if not tokens or tokens.pop(0) != "update":
            reasons.append(f"line {line_number}: invalid reputation event")
            continue
        fields: dict[str, str] = {}
        malformed = False
        for token in tokens:
            if "=" not in token:
                malformed = True
                break
            key, value = token.split("=", 1)
            if not key or not value or key in fields:
                malformed = True
                break
            fields[key] = value
        required = {"reporter", "target", "outcome", "delta", "score"}
        if malformed or set(fields) != required:
            reasons.append(f"line {line_number}: incomplete reputation update")
            continue
        try:
            reporter = int(fields["reporter"])
            target = int(fields["target"])
            delta = int(fields["delta"])
            score = int(fields["score"])
        except ValueError:
            reasons.append(f"line {line_number}: non-integer reputation field")
            continue
        if reporter not in membership or target not in membership:
            reasons.append(
                f"line {line_number}: reporter-target pair is outside membership"
            )
            continue
        if reporter == target:
            reasons.append(f"line {line_number}: self observation is invalid")
            continue
        expected_delta = {"response": 1, "timeout": -1}.get(
            fields["outcome"]
        )
        if expected_delta is None or delta != expected_delta:
            reasons.append(f"line {line_number}: outcome/delta mismatch")
            continue
        previous = scores.get(target, 0)
        if score != previous + delta:
            reasons.append(
                f"line {line_number}: target {target} score jumped from "
                f"{previous} to {score}"
            )
            continue
        scores[target] = score
        updates.append(
            {
                "line_number": line_number,
                "reporter": reporter,
                "target": target,
                "outcome": fields["outcome"],
                "delta": delta,
                "score": score,
            }
        )
    if not updates:
        reasons.append("missing KAURI_REPUTATION update")
    return ReputationVerdict(not reasons, tuple(reasons), tuple(updates))


def load_profile(path: Path) -> dict[str, object]:
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SmokeError(f"cannot read profile {path}: {exc}") from exc
    required = {
        "profile_id",
        "label",
        "replica_ids",
        "f",
        "tree",
        "observer",
        "crash_targets",
        "bucket_width_s",
        "baseline_bucket_count",
        "grace_bucket_count",
        "post_bucket_count",
        "minimum_recovery_ratio",
        "max_stall_s",
        "minimum_common_baseline_commits",
        "minimum_common_post_crash_commits",
        "aggregation_timeout_s",
        "leader_progress_timeout_s",
        "leader_activation_grace_s",
        "adaptive_activation_height",
        "block_size",
        "seed",
    }
    missing = sorted(required - set(profile))
    if missing:
        raise SmokeError("profile is missing fields: " + ", ".join(missing))
    replica_ids = tuple(int(value) for value in profile["replica_ids"])
    if replica_ids != tuple(range(7)) or len(set(replica_ids)) != 7:
        raise SmokeError("profile must contain replica IDs 0 through 6")
    if int(profile["f"]) != 2:
        raise SmokeError("seven-replica profile must use f=2")
    if str(profile["tree"]) != "fan:2 pipe:2 0 1 2 3 4 5 6":
        raise SmokeError("profile tree differs from the frozen smoke tree")
    if int(profile["observer"]) != 0:
        raise SmokeError("designated observer must be surviving replica 0")
    if tuple(int(value) for value in profile["crash_targets"]) != (4, 6):
        raise SmokeError("frozen crash targets must be replicas 4 and 6")
    return profile


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_revision(repository: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def create_run_directory(results_root: Path) -> Path:
    results_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_directory = (
        results_root / f"{stamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    )
    run_directory.mkdir(mode=0o700)
    return run_directory


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_private_text(path: Path, value: str) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(value)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _assert_executable(path: Path, label: str) -> None:
    if not path.is_file() or not os.access(path, os.X_OK):
        raise SmokeError(f"{label} is not executable: {path}")


def _assert_safe_command(command: Sequence[str]) -> None:
    if not command or any(not part for part in command):
        raise SmokeError("process command contains an empty argument")
    lowered = {Path(part).name.lower() for part in command}
    forbidden = lowered & FORBIDDEN_COMMAND_TOKENS
    if forbidden:
        raise SmokeError(f"unsafe command token: {sorted(forbidden)[0]}")


def _parse_generator_output(
    text: str,
    *,
    expected_count: int,
    required_fields: Sequence[str],
    label: str,
) -> list[dict[str, str]]:
    parsed: list[dict[str, str]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        fields: dict[str, str] = {}
        for token in line.split():
            if ":" not in token:
                raise SmokeError(
                    f"{label} line {line_number} has invalid token"
                )
            key, value = token.split(":", 1)
            if not key or not value or key in fields:
                raise SmokeError(
                    f"{label} line {line_number} has invalid field"
                )
            fields[key] = value
        if set(fields) != set(required_fields):
            raise SmokeError(
                f"{label} line {line_number} has unexpected fields"
            )
        parsed.append(fields)
    if len(parsed) != expected_count:
        raise SmokeError(
            f"{label} produced {len(parsed)} identities, expected "
            f"{expected_count}"
        )
    for field in required_fields:
        values = [entry[field] for entry in parsed]
        if len(set(values)) != len(values):
            raise SmokeError(f"{label} produced duplicate {field} values")
    return parsed


def generate_identities(
    keygen_binary: Path,
    tls_keygen_binary: Path,
    config_directory: Path,
    replica_count: int,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    bls_command = (
        str(keygen_binary),
        "--num",
        str(replica_count),
        "--algo",
        "bls",
    )
    tls_command = (
        str(tls_keygen_binary),
        "--num",
        str(replica_count),
    )
    for command in (bls_command, tls_command):
        _assert_safe_command(command)
    try:
        bls_result = subprocess.run(
            list(bls_command),
            cwd=config_directory,
            check=True,
            capture_output=True,
            text=True,
        )
        tls_result = subprocess.run(
            list(tls_command),
            cwd=config_directory,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.SubprocessError as exc:
        raise SmokeError(f"identity generation failed: {exc}") from exc
    _write_private_text(
        config_directory / "bls-identities.txt",
        bls_result.stdout,
    )
    _write_private_text(
        config_directory / "tls-identities.txt",
        tls_result.stdout,
    )
    return (
        _parse_generator_output(
            bls_result.stdout,
            expected_count=replica_count,
            required_fields=("pub", "sec"),
            label="BLS keygen",
        ),
        _parse_generator_output(
            tls_result.stdout,
            expected_count=replica_count,
            required_fields=("crt", "sec", "cid"),
            label="TLS keygen",
        ),
    )


def write_configs(
    run_directory: Path,
    profile: Mapping[str, object],
    bls: Sequence[Mapping[str, str]],
    tls: Sequence[Mapping[str, str]],
    *,
    peer_port: int,
    client_port: int,
) -> tuple[Path, list[Path], Path, Path]:
    config_directory = run_directory / "config"
    local_directory = Path(__file__).resolve().parent
    epoch0 = config_directory / "epoch0.tree"
    epoch1 = config_directory / "epoch1.tree"
    epoch0.write_text(
        (local_directory / "epoch0.tree").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    epoch1.write_text(
        (local_directory / "epoch1.tree").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    timeout_file = config_directory / "timeouts.empty"
    timeout_file.write_text("# no injected reports\n", encoding="utf-8")

    replica_ids = tuple(int(value) for value in profile["replica_ids"])
    lines = [
        f"block-size = {int(profile['block_size'])}",
        "nworker = 2",
        "pace-maker = dummy",
        "proposer = 0",
        "fan-out = 2",
        "piped_latency = 1",
        "async_blocks = 4",
        "base-timeout = 2.0",
        "prop-delay = 0.1",
        f"aggregation-timeout = {float(profile['aggregation_timeout_s'])}",
        f"leader-progress-timeout = {float(profile['leader_progress_timeout_s'])}",
        f"leader-activation-grace = {float(profile['leader_activation_grace_s'])}",
        "client-ip = 127.0.0.1",
        "tree-generation = file",
        f"tree-generation-fpath = {epoch0}",
        "tree-switch-period = 1000000",
        "epoch-protocol-mode = adaptive_v1",
        f"adaptive-epoch-file = {epoch1}",
        "adaptive-activation-height = "
        f"{int(profile['adaptive_activation_height'])}",
    ]
    for replica in replica_ids:
        lines.append(
            "replica = "
            f"127.0.0.1:{peer_port + replica};{client_port + replica}, "
            f"{bls[replica]['pub']}, {tls[replica]['cid']}"
        )
    main_config = config_directory / "hotstuff.gen.conf"
    main_config.write_text("\n".join(lines) + "\n", encoding="utf-8")

    replica_configs: list[Path] = []
    for replica in replica_ids:
        path = config_directory / f"hotstuff-sec{replica}.conf"
        _write_private_text(
            path,
            f"privkey = {bls[replica]['sec']}\n"
            f"tls-privkey = {tls[replica]['sec']}\n"
            f"tls-cert = {tls[replica]['crt']}\n"
            f"idx = {replica}\n",
        )
        replica_configs.append(path)
    return main_config, replica_configs, epoch0, timeout_file


def build_replica_command(
    app_binary: Path, main_config: Path, replica_config: Path
) -> tuple[str, ...]:
    command = (
        str(app_binary),
        "--conf",
        str(main_config),
        "--conf",
        str(replica_config),
    )
    _assert_safe_command(command)
    return command


def build_manager_command(
    manager_binary: Path,
    main_config: Path,
    epoch0_file: Path,
    timeout_file: Path,
) -> tuple[str, ...]:
    command = (
        str(manager_binary),
        "--conf",
        str(main_config),
        "--idx",
        "0",
        "--default_epoch",
        str(epoch0_file),
        "--timeouts",
        str(timeout_file),
    )
    _assert_safe_command(command)
    return command


def required_ports(
    replica_ids: Sequence[int], peer_port: int, client_port: int
) -> tuple[int, ...]:
    ports = tuple(peer_port + replica for replica in replica_ids)
    ports += tuple(client_port + replica for replica in replica_ids)
    ports += (MANAGER_PORT,)
    if len(set(ports)) != len(ports):
        raise SmokeError("configured smoke ports overlap")
    return ports


def check_ports_free(ports: Iterable[int]) -> list[int]:
    unavailable: list[int] = []
    for port in ports:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            unavailable.append(port)
        finally:
            probe.close()
    return unavailable


def check_ports_listening(ports: Iterable[int]) -> list[int]:
    listening: list[int] = []
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.1)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                listening.append(port)
    return listening


def wait_for_port_listening(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port in check_ports_listening((port,)):
            return True
        time.sleep(0.05)
    return False


def wait_for_listeners_stopped(
    ports: Sequence[int], timeout: float
) -> list[int]:
    deadline = time.monotonic() + timeout
    listening = list(ports)
    while listening and time.monotonic() < deadline:
        listening = check_ports_listening(ports)
        if listening:
            time.sleep(0.05)
    return listening


def spawn_process(
    name: str,
    command: Sequence[str],
    log_path: Path,
    cwd: Path,
    *,
    replica: int | None = None,
) -> ProcessRecord:
    _assert_safe_command(command)
    log_handle = log_path.open("wb")
    try:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        pgid = os.getpgid(process.pid)
        if pgid != process.pid:
            raise SmokeError(f"{name} did not receive an isolated group")
        return ProcessRecord(
            name,
            process.pid,
            pgid,
            command,
            log_path,
            process,
            log_handle,
            replica=replica,
        )
    except Exception:
        log_handle.close()
        raise


def process_exit_status(return_code: int) -> dict[str, object]:
    signaled = return_code < 0
    signal_number = -return_code if signaled else None
    signal_name: str | None = None
    if signal_number is not None:
        try:
            signal_name = signal.Signals(signal_number).name
        except ValueError:
            signal_name = f"UNKNOWN_{signal_number}"
    return {
        "popen_return_code": return_code,
        "shell_return_code": 128 + signal_number if signaled else return_code,
        "signaled": signaled,
        "signal_number": signal_number,
        "signal_name": signal_name,
    }


def observe_process_exits(
    records: Sequence[ProcessRecord],
    observations: list[dict[str, object]],
    *,
    phase: str,
) -> list[dict[str, object]]:
    known = {str(observation["name"]) for observation in observations}
    new: list[dict[str, object]] = []
    for record in records:
        return_code = record.process.poll()
        if return_code is None or record.name in known:
            continue
        observation: dict[str, object] = {
            "name": record.name,
            "replica": record.replica,
            "pid": record.pid,
            "pgid": record.pgid,
            "command": list(record.command),
            "observed_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "observed_monotonic_ns": time.monotonic_ns(),
            "phase": phase,
            **process_exit_status(return_code),
        }
        observations.append(observation)
        new.append(observation)
        known.add(record.name)
    return new


def terminate_recorded_groups(
    records: Sequence[ProcessRecord],
    *,
    int_grace: float = 2.0,
    term_grace: float = 2.0,
) -> None:
    groups = {record.pgid for record in records if record.pgid > 1}
    if os.getpgrp() in groups:
        raise SmokeError("refusing to signal the launcher's process group")

    def alive(record: ProcessRecord) -> bool:
        return record.process.poll() is None

    for sig, grace in (
        (signal.SIGINT, int_grace),
        (signal.SIGTERM, term_grace),
        (signal.SIGKILL, 0.2),
    ):
        active = {record.pgid for record in records if alive(record)}
        for pgid in sorted(groups & active):
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + grace
        while any(alive(record) for record in records) and time.monotonic() < deadline:
            time.sleep(0.05)
    for record in records:
        try:
            record.process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass
        record.log_handle.close()


def _read_replica_logs(
    run_directory: Path, replica_ids: Sequence[int]
) -> dict[int, str]:
    logs: dict[int, str] = {}
    for replica in replica_ids:
        path = run_directory / f"replica-{replica}.log"
        if path.exists():
            logs[replica] = path.read_text(
                encoding="utf-8", errors="replace"
            )
    return logs


def _read_manager_log(run_directory: Path) -> str:
    path = run_directory / "manager.log"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _runtime_exit_error(
    new_exits: Sequence[Mapping[str, object]],
    expected_crashes: set[int],
    crash_started: bool,
) -> str | None:
    for observation in new_exits:
        replica_value = observation.get("replica")
        if replica_value is None:
            return f"{observation['name']} exited before cleanup"
        replica = int(replica_value)
        if not crash_started or replica not in expected_crashes:
            return f"survivor replica {replica} exited before cleanup"
        if int(observation.get("signal_number") or 0) != int(signal.SIGKILL):
            return f"crash target replica {replica} exited unexpectedly"
    return None


def wait_for_common_baseline(
    run_directory: Path,
    records: Sequence[ProcessRecord],
    observations: list[dict[str, object]],
    replica_ids: Sequence[int],
    minimum_commits: int,
    timeout: float,
) -> list[tuple[int, str]]:
    deadline = time.monotonic() + timeout
    last_common: list[tuple[int, str]] = []
    while time.monotonic() < deadline:
        new = observe_process_exits(records, observations, phase="startup")
        error = _runtime_exit_error(new, set(), False)
        if error is not None:
            raise SmokeError(error)
        logs = _read_replica_logs(run_directory, replica_ids)
        if len(logs) == len(replica_ids):
            events = {
                replica: parse_commit_events(text, replica)
                for replica, text in logs.items()
            }
            validate_survivor_agreement(events, replica_ids)
            last_common = common_commits(events, replica_ids)
            if len(last_common) >= minimum_commits:
                return last_common
        time.sleep(0.1)
    raise SmokeError(
        f"timed out waiting for {minimum_commits} common baseline commits; "
        f"observed {len(last_common)}"
    )


def wait_until_monotonic_ns(
    deadline_ns: int,
    records: Sequence[ProcessRecord],
    observations: list[dict[str, object]],
    *,
    phase: str,
    expected_crashes: set[int],
    crash_started: bool,
) -> None:
    while time.monotonic_ns() < deadline_ns:
        new = observe_process_exits(records, observations, phase=phase)
        error = _runtime_exit_error(new, expected_crashes, crash_started)
        if error is not None:
            raise SmokeError(error)
        remaining = max(0, deadline_ns - time.monotonic_ns())
        time.sleep(min(0.1, remaining / 1_000_000_000))


def confirm_crashes(
    records_by_replica: Mapping[int, ProcessRecord],
    crash_events: list[dict[str, object]],
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    pending = {int(event["replica"]) for event in crash_events}
    while pending and time.monotonic() < deadline:
        for replica in tuple(pending):
            return_code = records_by_replica[replica].process.poll()
            if return_code is None:
                continue
            event = next(
                item for item in crash_events if item["replica"] == replica
            )
            event["confirmed_utc"] = dt.datetime.now(
                dt.timezone.utc
            ).isoformat()
            event["confirmed_monotonic_ns"] = time.monotonic_ns()
            event["exit"] = process_exit_status(return_code)
            pending.remove(replica)
        if pending:
            time.sleep(0.05)
    if pending:
        raise SmokeError(f"crash targets did not stop: {sorted(pending)}")


def _write_throughput_csv(
    path: Path, buckets: Sequence[ThroughputBucket]
) -> None:
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(
            [
                "bucket_index",
                "bucket_start_s",
                "bucket_end_s",
                "bucket_width_s",
                "committed_blocks",
                "transactions",
                "tps",
                "phase",
                "is_grace",
            ]
        )
        for bucket in buckets:
            writer.writerow(
                [
                    bucket.bucket_index,
                    f"{bucket.bucket_start_s:.6f}",
                    f"{bucket.bucket_end_s:.6f}",
                    f"{bucket.bucket_width_s:.6f}",
                    bucket.commit_count,
                    bucket.tx_count,
                    f"{bucket.tps:.9f}",
                    bucket.phase,
                    str(bucket.is_grace).lower(),
                ]
            )


def _write_reputation_csv(
    path: Path, updates: Sequence[Mapping[str, object]]
) -> None:
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(
            ["line_number", "reporter", "target", "outcome", "delta", "score"]
        )
        for update in updates:
            writer.writerow(
                [
                    update["line_number"],
                    update["reporter"],
                    update["target"],
                    update["outcome"],
                    update["delta"],
                    update["score"],
                ]
            )


def _write_figure_svg(
    path: Path,
    buckets: Sequence[ThroughputBucket],
    throughput: ThroughputVerdict,
    label: str,
    crash_offset_s: float,
) -> None:
    width, height = 1000, 560
    left, right, top, bottom = 90, 40, 90, 115
    plot_width = width - left - right
    plot_height = height - top - bottom
    maximum = max((bucket.tps for bucket in buckets), default=0.0)
    maximum = max(maximum, throughput.baseline_median_tps, 1.0) * 1.15
    bar_width = plot_width / max(len(buckets), 1)

    def x(index: float) -> float:
        return left + index * bar_width

    def y(value: float) -> float:
        return top + plot_height - (value / maximum) * plot_height

    colors = {
        "baseline": "#2563eb",
        "grace": "#f59e0b",
        "post": "#16a34a" if throughput.classification == "PASS" else "#dc2626",
    }
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{left}" y="34" font-family="system-ui,sans-serif" font-size="22" font-weight="700" fill="#111827">{escape(label)}</text>',
        f'<text x="{left}" y="61" font-family="system-ui,sans-serif" font-size="14" fill="#4b5563">Raw 5-second authoritative commit throughput · verdict {throughput.classification}</text>',
    ]
    for tick in range(6):
        value = maximum * tick / 5
        tick_y = y(value)
        elements.append(
            f'<line x1="{left}" x2="{width-right}" y1="{tick_y:.2f}" y2="{tick_y:.2f}" stroke="#e5e7eb"/>'
        )
        elements.append(
            f'<text x="{left-12}" y="{tick_y+5:.2f}" text-anchor="end" font-family="system-ui,sans-serif" font-size="12" fill="#6b7280">{value:.1f}</text>'
        )
    for bucket in buckets:
        bar_x = x(bucket.bucket_index) + bar_width * 0.16
        bar_y = y(bucket.tps)
        bar_height = top + plot_height - bar_y
        elements.append(
            f'<rect x="{bar_x:.2f}" y="{bar_y:.2f}" width="{bar_width*0.68:.2f}" height="{bar_height:.2f}" rx="4" fill="{colors[bucket.phase]}"/>'
        )
        phase_index = sum(
            1
            for previous in buckets[: bucket.bucket_index + 1]
            if previous.phase == bucket.phase
        )
        prefix = {"baseline": "B", "grace": "G", "post": "P"}[
            bucket.phase
        ]
        elements.append(
            f'<text x="{x(bucket.bucket_index+0.5):.2f}" y="{top+plot_height+24}" text-anchor="middle" font-family="system-ui,sans-serif" font-size="12" fill="#374151">{prefix}{phase_index}</text>'
        )
        elements.append(
            f'<text x="{x(bucket.bucket_index+0.5):.2f}" y="{bar_y-8:.2f}" text-anchor="middle" font-family="system-ui,sans-serif" font-size="12" font-weight="600" fill="#111827">{bucket.tps:.1f}</text>'
        )
    baseline_count = sum(bucket.phase == "baseline" for bucket in buckets)
    post_start = next(
        (bucket.bucket_index for bucket in buckets if bucket.phase == "post"),
        len(buckets),
    )
    bucket_width_s = buckets[0].bucket_width_s if buckets else 1.0
    crash_x = x(crash_offset_s / bucket_width_s)
    elements.extend(
        [
            f'<line x1="{crash_x:.2f}" x2="{crash_x:.2f}" y1="{top}" y2="{top+plot_height}" stroke="#991b1b" stroke-width="2" stroke-dasharray="7 5"/>',
            f'<text x="{crash_x+8:.2f}" y="{top+16}" font-family="system-ui,sans-serif" font-size="12" font-weight="700" fill="#991b1b">crash replicas 4 &amp; 6 at {crash_offset_s:.3f}s</text>',
            f'<line x1="{left}" x2="{x(baseline_count):.2f}" y1="{y(throughput.baseline_median_tps):.2f}" y2="{y(throughput.baseline_median_tps):.2f}" stroke="#1d4ed8" stroke-width="3"/>',
            f'<line x1="{x(post_start):.2f}" x2="{width-right}" y1="{y(throughput.post_median_tps):.2f}" y2="{y(throughput.post_median_tps):.2f}" stroke="#15803d" stroke-width="3"/>',
            f'<text x="{left}" y="{height-60}" font-family="system-ui,sans-serif" font-size="13" fill="#111827">Baseline median {throughput.baseline_median_tps:.2f} TPS · Post median {throughput.post_median_tps:.2f} TPS · Ratio {throughput.recovery_ratio:.1%}</text>',
            f'<text x="{left}" y="{height-32}" font-family="system-ui,sans-serif" font-size="12" fill="#6b7280">Development smoke only: fixed tree with failures pre-positioned at leaves; not adaptive crash recovery evidence.</text>',
            f'<text transform="translate(24 {top+plot_height/2:.2f}) rotate(-90)" text-anchor="middle" font-family="system-ui,sans-serif" font-size="13" fill="#374151">Transactions per second</text>',
            "</svg>",
        ]
    )
    path.write_text("\n".join(elements) + "\n", encoding="utf-8")


def analyze_and_write_artifacts(
    run_directory: Path,
    profile: Mapping[str, object],
    *,
    measurement_start_event_ns: int | None,
    event_clock_offset_ns: int | None,
    crash_events: Sequence[Mapping[str, object]],
    exit_observations: Sequence[Mapping[str, object]],
    runtime_error: str | None,
    postflight_listeners: Sequence[int],
) -> dict[str, object]:
    replica_ids = tuple(int(value) for value in profile["replica_ids"])
    crash_targets = tuple(int(value) for value in profile["crash_targets"])
    survivors = tuple(
        replica for replica in replica_ids if replica not in crash_targets
    )
    observer = int(profile["observer"])
    start_ns = measurement_start_event_ns or time.monotonic_ns()
    hard_reasons: list[str] = []
    if runtime_error:
        hard_reasons.append(runtime_error)
    if postflight_listeners:
        hard_reasons.append(
            f"postflight listeners remain active: {list(postflight_listeners)}"
        )

    logs = _read_replica_logs(run_directory, replica_ids)
    events_by_replica: dict[int, list[CommitEvent]] = {}
    for replica in replica_ids:
        text = logs.get(replica, "")
        if not text:
            hard_reasons.append(f"replica {replica} log is missing or empty")
            events_by_replica[replica] = []
            continue
        if FATAL_TEXT.search(text):
            hard_reasons.append(f"replica {replica} contains fatal text")
        if PROTOCOL_REJECTION_TEXT.search(text):
            hard_reasons.append(
                f"replica {replica} contains protocol rejection text"
            )
        try:
            events_by_replica[replica] = validate_and_deduplicate_commits(
                parse_commit_events(text, replica)
            )
        except SmokeError as exc:
            hard_reasons.append(str(exc))
            events_by_replica[replica] = []

    crash_ns = (
        min(
            runner_to_event_clock(
                int(event["requested_monotonic_ns"]),
                event_clock_offset_ns or 0,
            )
            for event in crash_events
        )
        if crash_events
        else start_ns
        + int(profile["baseline_bucket_count"])
        * int(float(profile["bucket_width_s"]) * 1_000_000_000)
    )
    pre_crash_events = {
        replica: [
            event
            for event in events_by_replica.get(replica, ())
            if event.monotonic_ns < crash_ns
        ]
        for replica in replica_ids
    }
    try:
        validate_survivor_agreement(pre_crash_events, replica_ids)
    except SmokeError as exc:
        hard_reasons.append(str(exc))
    try:
        validate_survivor_agreement(events_by_replica, survivors)
    except SmokeError as exc:
        hard_reasons.append(str(exc))

    observer_events = validate_and_deduplicate_commits(
        events_by_replica.get(observer, ())
    )
    buckets = build_throughput_buckets(
        observer_events,
        measurement_start_ns=start_ns,
        bucket_width_s=float(profile["bucket_width_s"]),
        baseline_bucket_count=int(profile["baseline_bucket_count"]),
        grace_bucket_count=int(profile["grace_bucket_count"]),
        post_bucket_count=int(profile["post_bucket_count"]),
    )
    throughput = evaluate_throughput(
        buckets,
        observer_events=observer_events,
        measurement_start_ns=start_ns,
        minimum_recovery_ratio=float(profile["minimum_recovery_ratio"]),
        max_stall_s=float(profile["max_stall_s"]),
    )

    try:
        post_common = common_commits(
            events_by_replica,
            survivors,
            minimum_monotonic_ns=crash_ns,
        )
    except SmokeError as exc:
        hard_reasons.append(str(exc))
        post_common = []
    minimum_post = int(profile["minimum_common_post_crash_commits"])
    if len(post_common) < minimum_post:
        hard_reasons.append(
            f"only {len(post_common)} common survivor commits after crash; "
            f"required {minimum_post}"
        )

    runtime_exits = [
        observation
        for observation in exit_observations
        if observation.get("phase") != "cleanup"
    ]
    try:
        validate_process_exits(runtime_exits, crash_targets)
    except SmokeError as exc:
        hard_reasons.append(str(exc))

    reputation = evaluate_reputation_log(
        _read_manager_log(run_directory), replica_ids
    )
    reputation_diagnostics = list(reputation.reasons)
    if not reputation.passed:
        reputation_diagnostics = [
            f"reputation: {reason}" for reason in reputation.reasons
        ]
    required_timeout_pairs = {(1, 4), (2, 6)}
    observed_timeout_pairs = {
        (int(update["reporter"]), int(update["target"]))
        for update in reputation.updates
        if update["outcome"] == "timeout"
    }
    missing_timeout_pairs = sorted(
        required_timeout_pairs - observed_timeout_pairs
    )
    if missing_timeout_pairs:
        reputation_diagnostics.append(
            f"missing reputation timeout observations: {missing_timeout_pairs}"
        )

    classification = (
        "FAIL" if hard_reasons else throughput.classification
    )
    reasons = list(hard_reasons)
    if not hard_reasons:
        reasons.extend(throughput.reasons)

    _write_throughput_csv(run_directory / "throughput.csv", buckets)
    _write_reputation_csv(
        run_directory / "reputation.csv", reputation.updates
    )
    _write_json(run_directory / "crash-events.json", list(crash_events))
    _write_json(
        run_directory / "commit-events.json",
        {
            str(replica): [event._asdict() for event in events]
            for replica, events in events_by_replica.items()
        },
    )
    _write_figure_svg(
        run_directory / "throughput.svg",
        buckets,
        throughput._replace(classification=classification),
        str(profile["label"]),
        (crash_ns - start_ns) / 1_000_000_000,
    )
    result: dict[str, object] = {
        "classification": classification,
        "passed": classification == "PASS",
        "reasons": reasons,
        "profile_id": profile["profile_id"],
        "scope": "fixed-tree leaf-crash development smoke",
        "not_smoke20": True,
        "measurement_start_event_monotonic_ns": start_ns,
        "event_clock_offset_ns": event_clock_offset_ns,
        "crash_offset_s": (crash_ns - start_ns) / 1_000_000_000,
        "throughput": throughput._asdict(),
        "common_post_crash_commits": [
            {"height": height, "hash": block_hash}
            for height, block_hash in post_common
        ],
        "required_reputation_timeout_pairs": [
            list(pair) for pair in sorted(required_timeout_pairs)
        ],
        "observed_reputation_timeout_pairs": [
            list(pair) for pair in sorted(observed_timeout_pairs)
        ],
        "reputation_diagnostics": reputation_diagnostics,
        "reputation_diagnostic_passed": (
            reputation.passed and not missing_timeout_pairs
        ),
        "listeners_stopped": not postflight_listeners,
    }
    _write_json(run_directory / "verdict.json", result)
    return result


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    local_directory = Path(__file__).resolve().parent
    repository = local_directory.parents[2]
    parser = argparse.ArgumentParser(
        description="Run the seven-replica fixed-tree leaf-crash smoke"
    )
    parser.add_argument("--repository", type=Path, default=repository)
    parser.add_argument(
        "--profile", type=Path, default=local_directory / "profile.json"
    )
    parser.add_argument(
        "--app-binary",
        type=Path,
        default=repository / "build-adaptive/examples/hotstuff-app",
    )
    parser.add_argument(
        "--manager-binary",
        type=Path,
        default=repository / "build-adaptive/examples/hotstuff-client",
    )
    parser.add_argument(
        "--keygen-binary",
        type=Path,
        default=repository / "build-adaptive/hotstuff-keygen",
    )
    parser.add_argument(
        "--tls-keygen-binary",
        type=Path,
        default=repository / "build-adaptive/hotstuff-tls-keygen",
    )
    parser.add_argument(
        "--results-root",
        type=Path,
        default=repository / "results/seven-replica-leaf-crash-smoke",
    )
    parser.add_argument("--peer-port", type=int, default=25100)
    parser.add_argument("--client-port", type=int, default=26100)
    parser.add_argument("--startup-timeout", type=float, default=60.0)
    parser.add_argument("--crash-confirm-timeout", type=float, default=5.0)
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    repository = args.repository.resolve()
    profile_path = args.profile.resolve()
    profile = load_profile(profile_path)
    app_binary = args.app_binary.resolve()
    manager_binary = args.manager_binary.resolve()
    keygen_binary = args.keygen_binary.resolve()
    tls_keygen_binary = args.tls_keygen_binary.resolve()
    for path, label in (
        (app_binary, "app binary"),
        (manager_binary, "manager binary"),
        (keygen_binary, "BLS keygen"),
        (tls_keygen_binary, "TLS keygen"),
    ):
        _assert_executable(path, label)
    if args.startup_timeout <= 0 or args.crash_confirm_timeout <= 0:
        raise SmokeError("runner timeouts must be positive")

    replica_ids = tuple(int(value) for value in profile["replica_ids"])
    crash_targets = tuple(int(value) for value in profile["crash_targets"])
    ports = required_ports(replica_ids, args.peer_port, args.client_port)
    occupied = check_ports_free(ports)
    if occupied:
        raise SmokeError(f"preflight ports are in use: {occupied}")

    run_directory = create_run_directory(args.results_root.resolve())
    config_directory = run_directory / "config"
    config_directory.mkdir(mode=0o700)
    records: list[ProcessRecord] = []
    records_by_replica: dict[int, ProcessRecord] = {}
    exit_observations: list[dict[str, object]] = []
    crash_events: list[dict[str, object]] = []
    runner_measurement_start_ns: int | None = None
    measurement_start_event_ns: int | None = None
    event_clock_offset_ns: int | None = None
    runtime_error: str | None = None
    postflight_listeners: list[int] = list(ports)
    interrupted = False
    interruption_signal: int | None = None

    revision = git_revision(repository)
    manifest: dict[str, object] = {
        "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "started_monotonic_ns": time.monotonic_ns(),
        "repository": str(repository),
        "revision": revision,
        "profile": str(profile_path),
        "profile_sha256": sha256_file(profile_path),
        "profile_data": profile,
        "runner_pid": os.getpid(),
        "runner_pgid": os.getpgrp(),
        "ports": list(ports),
        "binaries": {
            "app": {"path": str(app_binary), "sha256": sha256_file(app_binary)},
            "manager": {
                "path": str(manager_binary),
                "sha256": sha256_file(manager_binary),
            },
            "keygen": {
                "path": str(keygen_binary),
                "sha256": sha256_file(keygen_binary),
            },
            "tls_keygen": {
                "path": str(tls_keygen_binary),
                "sha256": sha256_file(tls_keygen_binary),
            },
        },
        "processes": [],
        "exit_observations": [],
    }
    _write_json(run_directory / "manifest.json", manifest)

    def request_shutdown(signum: int, _frame: object) -> None:
        nonlocal interrupted, interruption_signal
        interruption_signal = signum
        interrupted = True
        raise KeyboardInterrupt

    previous_handlers = {
        signum: signal.signal(signum, request_shutdown)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        bls, tls = generate_identities(
            keygen_binary,
            tls_keygen_binary,
            config_directory,
            len(replica_ids),
        )
        main_config, replica_configs, epoch0, timeout_file = write_configs(
            run_directory,
            profile,
            bls,
            tls,
            peer_port=args.peer_port,
            client_port=args.client_port,
        )
        process_cwd = run_directory.resolve()
        manager = spawn_process(
            "manager",
            build_manager_command(
                manager_binary, main_config, epoch0, timeout_file
            ),
            run_directory / "manager.log",
            process_cwd,
        )
        records.append(manager)
        if not wait_for_port_listening(MANAGER_PORT, args.startup_timeout):
            raise SmokeError("reputation manager did not become ready")

        for replica in replica_ids:
            record = spawn_process(
                f"replica-{replica}",
                build_replica_command(
                    app_binary, main_config, replica_configs[replica]
                ),
                run_directory / f"replica-{replica}.log",
                process_cwd,
                replica=replica,
            )
            records.append(record)
            records_by_replica[replica] = record
        manifest["processes"] = [
            record.manifest_entry() for record in records
        ]
        _write_json(run_directory / "manifest.json", manifest)

        all_replica_ports = tuple(
            args.peer_port + replica for replica in replica_ids
        ) + tuple(args.client_port + replica for replica in replica_ids)
        readiness_deadline = time.monotonic() + args.startup_timeout
        while time.monotonic() < readiness_deadline:
            new = observe_process_exits(
                records, exit_observations, phase="startup"
            )
            error = _runtime_exit_error(new, set(), False)
            if error is not None:
                raise SmokeError(error)
            if set(check_ports_listening(all_replica_ports)) == set(
                all_replica_ports
            ):
                break
            time.sleep(0.1)
        else:
            missing = sorted(
                set(all_replica_ports)
                - set(check_ports_listening(all_replica_ports))
            )
            raise SmokeError(f"replica ports did not become ready: {missing}")

        baseline_common = wait_for_common_baseline(
            run_directory,
            records,
            exit_observations,
            replica_ids,
            int(profile["minimum_common_baseline_commits"]),
            args.startup_timeout,
        )
        observer = int(profile["observer"])
        observer_log = _read_replica_logs(run_directory, (observer,)).get(
            observer, ""
        )
        observer_events = validate_and_deduplicate_commits(
            parse_commit_events(observer_log, observer)
        )
        if not observer_events:
            raise SmokeError("observer has no commit event for clock anchor")
        anchor_event = observer_events[-1]
        runner_measurement_start_ns = time.monotonic_ns()
        event_clock_offset_ns = calibrate_event_clock(
            anchor_event.monotonic_ns, runner_measurement_start_ns
        )
        measurement_start_event_ns = runner_to_event_clock(
            runner_measurement_start_ns, event_clock_offset_ns
        )
        manifest["baseline_common_commits"] = [
            {"height": height, "hash": block_hash}
            for height, block_hash in baseline_common
        ]
        manifest["clock_calibration"] = {
            "anchor_replica": observer,
            "anchor_height": anchor_event.height,
            "anchor_hash": anchor_event.block_hash,
            "anchor_event_monotonic_ns": anchor_event.monotonic_ns,
            "observed_runner_monotonic_ns": runner_measurement_start_ns,
            "event_minus_runner_offset_ns": event_clock_offset_ns,
        }
        manifest["runner_measurement_start_monotonic_ns"] = (
            runner_measurement_start_ns
        )
        manifest["measurement_start_event_monotonic_ns"] = (
            measurement_start_event_ns
        )
        _write_json(run_directory / "manifest.json", manifest)

        bucket_ns = int(
            float(profile["bucket_width_s"]) * 1_000_000_000
        )
        baseline_end_ns = runner_measurement_start_ns + int(
            profile["baseline_bucket_count"]
        ) * bucket_ns
        wait_until_monotonic_ns(
            baseline_end_ns,
            records,
            exit_observations,
            phase="baseline",
            expected_crashes=set(crash_targets),
            crash_started=False,
        )

        crash_events = inject_crashes(records_by_replica, crash_targets)
        confirm_crashes(
            records_by_replica, crash_events, args.crash_confirm_timeout
        )
        observe_process_exits(
            records, exit_observations, phase="post_crash"
        )
        validate_process_exits(
            [
                observation
                for observation in exit_observations
                if observation.get("phase") != "cleanup"
            ],
            crash_targets,
        )
        _write_json(run_directory / "crash-events.json", crash_events)

        total_buckets = (
            int(profile["baseline_bucket_count"])
            + int(profile["grace_bucket_count"])
            + int(profile["post_bucket_count"])
        )
        measurement_end_ns = (
            runner_measurement_start_ns + total_buckets * bucket_ns
        )
        wait_until_monotonic_ns(
            measurement_end_ns,
            records,
            exit_observations,
            phase="post_crash",
            expected_crashes=set(crash_targets),
            crash_started=True,
        )
    except KeyboardInterrupt:
        signal_name = (
            signal.Signals(interruption_signal).name
            if interruption_signal is not None
            else "interrupt"
        )
        runtime_error = f"smoke interrupted by {signal_name}"
    except (SmokeError, OSError, subprocess.SubprocessError) as exc:
        runtime_error = str(exc)
    finally:
        try:
            observe_process_exits(
                records, exit_observations, phase="pre_cleanup"
            )
            terminate_recorded_groups(records)
            observe_process_exits(
                records, exit_observations, phase="cleanup"
            )
            postflight_listeners = wait_for_listeners_stopped(ports, 5.0)
            manifest["finished_utc"] = dt.datetime.now(
                dt.timezone.utc
            ).isoformat()
            manifest["interrupted"] = interrupted
            manifest["runtime_error"] = runtime_error
            manifest["exit_observations"] = exit_observations
            manifest["processes"] = [
                record.manifest_entry() for record in records
            ]
            _write_json(run_directory / "manifest.json", manifest)
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)

    result = analyze_and_write_artifacts(
        run_directory,
        profile,
        measurement_start_event_ns=measurement_start_event_ns,
        event_clock_offset_ns=event_clock_offset_ns,
        crash_events=crash_events,
        exit_observations=exit_observations,
        runtime_error=runtime_error,
        postflight_listeners=postflight_listeners,
    )
    print(f"results: {run_directory}")
    print(result["classification"])
    for reason in result["reasons"]:
        print(f"- {reason}")
    return 0 if result["classification"] == "PASS" else 1


def main() -> None:
    try:
        raise SystemExit(run())
    except SmokeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
