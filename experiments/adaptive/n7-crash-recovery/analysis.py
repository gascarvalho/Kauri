#!/usr/bin/env python3
"""Strict structured-event parsing and throughput analysis for the N=7 run.

This module deliberately does not validate a complete experiment run and does
not plot figures.  It turns the designated observer's canonical structured
commit events into raw, zero-filled throughput buckets.  A later validator is
responsible for accepting or rejecting the complete run before plotting.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import statistics
from typing import Any, Iterable, Mapping, Sequence


LEGACY_EVENT_PREFIX = "KAURI_EVENT "
EVENT_SCHEMA_VERSION = 1
AUTHORITATIVE_OBSERVER = 2
AUTHORITATIVE_SOURCE_ID = f"replica-{AUTHORITATIVE_OBSERVER}"
REPLICA_COUNT = 7
BUCKET_WIDTH_NS = 5_000_000_000
UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1

_FULL_HASH = re.compile(r"^[0-9a-f]{64}$")
_ENVELOPE_FIELDS = frozenset(
    {
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
)
_COMMIT_FIELDS = frozenset(
    {
        "block_height",
        "block_hash",
        "parent_hash",
        "transaction_count",
        "designated_observer",
        "decision_proof",
        "view_generation",
        "commit_batch_index",
    }
)
_DECISION_PROOF_FIELDS = frozenset(
    {"epoch_number", "tree_id", "epoch_digest", "block_hash"}
)
_LEGACY_PHASE_NAMES = ("baseline", "degraded", "post")
_RECURRING_PHASE_NAMES = (
    "baseline",
    "degraded",
    "containment",
    "optimized",
)

ConfigurationKey = tuple[int, str, int]


class AnalysisError(ValueError):
    """A deterministic canonical-input or throughput-analysis failure."""


@dataclass(frozen=True, slots=True)
class StructuredEvent:
    """One validated structured-event envelope from a bound source file."""

    run_id: str
    source_kind: str
    source_id: str
    source_instance: str
    source_sequence: int
    timestamp_ns: int
    event_type: str
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CommitEvent:
    """One authoritative, non-genesis commit from replica 2."""

    run_id: str
    source_kind: str
    source_id: str
    source_instance: str
    source_sequence: int
    timestamp_ns: int
    observer_replica: int
    height: int
    block_hash: str
    parent_hash: str | None
    epoch_number: int
    tree_id: int
    epoch_digest: str
    leader_replica: int
    transaction_count: int
    view_generation: int | None
    commit_batch_index: int

    @property
    def configuration_key(self) -> ConfigurationKey:
        return (self.epoch_number, self.epoch_digest, self.tree_id)

    @property
    def substantive_identity(self) -> tuple[object, ...]:
        """Metadata that must agree before a hash replay is deduplicated."""
        return (
            self.height,
            self.block_hash,
            self.parent_hash,
            self.transaction_count,
            self.epoch_number,
            self.tree_id,
            self.epoch_digest,
            self.leader_replica,
            self.view_generation,
            self.commit_batch_index,
        )


@dataclass(frozen=True, slots=True)
class PhaseBoundaries:
    """Half-open measurement boundaries in the event monotonic clock."""

    baseline_start_ns: int
    crash_ns: int
    activation_ns: int
    end_ns: int


@dataclass(frozen=True, slots=True)
class PhaseWindow:
    """One explicit half-open, epoch-qualified measurement window."""

    phase: str
    epoch_number: int
    start_ns: int
    end_ns: int


@dataclass(frozen=True, slots=True)
class ThroughputBucket:
    """One raw bucket; per-leader throughput conserves the aggregate exactly."""

    bucket_index: int
    phase: str
    start_ns: int
    end_ns: int
    elapsed_seconds: float
    commit_count: int
    transaction_count: int
    aggregate_tps: float
    leader_transactions: tuple[int, int, int, int, int, int, int]
    leader_tps: tuple[float, float, float, float, float, float, float]

    def as_row(self) -> dict[str, int | float | str]:
        """Return a flat row suitable for a later canonical CSV writer."""
        row: dict[str, int | float | str] = {
            "bucket_index": self.bucket_index,
            "phase": self.phase,
            "bucket_start_ns": self.start_ns,
            "bucket_end_ns": self.end_ns,
            "elapsed_seconds": self.elapsed_seconds,
            "commit_count": self.commit_count,
            "transaction_count": self.transaction_count,
            "throughput_tps": self.aggregate_tps,
        }
        for replica in range(REPLICA_COUNT):
            row[f"leader_{replica}_transactions"] = (
                self.leader_transactions[replica]
            )
            row[f"leader_{replica}_tps"] = self.leader_tps[replica]
        return row


@dataclass(frozen=True, slots=True)
class PhaseMedians:
    baseline_tps: float
    degraded_tps: float
    post_tps: float | None = None
    containment_tps: float | None = None
    optimized_tps: float | None = None


@dataclass(frozen=True, slots=True)
class ThroughputAnalysis:
    buckets: tuple[ThroughputBucket, ...]
    medians: PhaseMedians


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AnalysisError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise AnalysisError(f"non-finite JSON number is not canonical: {value}")


def _load_canonical_json(payload: str, line_number: int) -> dict[str, Any]:
    try:
        decoded = json.loads(
            payload,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except AnalysisError as exc:
        raise AnalysisError(f"line {line_number}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AnalysisError(
            f"line {line_number}: malformed KAURI_EVENT JSON: {exc.msg}"
        ) from exc
    if not isinstance(decoded, dict):
        raise AnalysisError(
            f"line {line_number}: KAURI_EVENT JSON must be an object"
        )
    return decoded


def _require_exact_fields(
    value: Mapping[str, Any],
    expected: frozenset[str],
    context: str,
) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise AnalysisError(f"{context} fields are invalid: {'; '.join(details)}")


def _require_integer(
    value: Any,
    name: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if (
        type(value) is not int
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        upper = "" if maximum is None else f" and at most {maximum}"
        raise AnalysisError(
            f"{name} must be an integer greater than or equal to {minimum}"
            f"{upper}"
        )
    return value


def _require_hash(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _FULL_HASH.fullmatch(value):
        raise AnalysisError(
            f"{name} must be a canonical lowercase 256-bit hexadecimal hash"
        )
    return value


def _parse_commit(
    envelope: StructuredEvent,
    *,
    expected_run_id: str,
    source_instance: str,
    leader_by_configuration: Mapping[ConfigurationKey, int],
) -> CommitEvent:
    payload = envelope.payload
    _require_exact_fields(payload, _COMMIT_FIELDS, "commit payload")

    if payload["designated_observer"] is not True:
        raise AnalysisError(
            "commit was not emitted by the designated authoritative observer"
        )

    height = _require_integer(
        payload["block_height"],
        "block_height",
        minimum=1,
        maximum=UINT64_MAX,
    )
    block_hash = _require_hash(payload["block_hash"], "block_hash")
    parent_hash = payload["parent_hash"]
    if parent_hash is not None:
        _require_hash(parent_hash, "parent_hash")
    transaction_count = _require_integer(
        payload["transaction_count"],
        "transaction_count",
        maximum=UINT64_MAX,
    )
    view_generation = payload["view_generation"]
    if view_generation is not None:
        view_generation = _require_integer(
            view_generation,
            "view_generation",
            maximum=UINT64_MAX,
        )
    commit_batch_index = _require_integer(
        payload["commit_batch_index"],
        "commit_batch_index",
        maximum=UINT64_MAX,
    )

    proof = payload["decision_proof"]
    if not isinstance(proof, dict):
        raise AnalysisError("decision_proof must be an object")
    _require_exact_fields(proof, _DECISION_PROOF_FIELDS, "decision_proof")
    epoch_number = _require_integer(
        proof["epoch_number"], "epoch_number", maximum=UINT32_MAX
    )
    tree_id = _require_integer(
        proof["tree_id"], "tree_id", maximum=UINT32_MAX
    )
    epoch_digest = _require_hash(proof["epoch_digest"], "epoch_digest")
    proof_hash = _require_hash(proof["block_hash"], "decision_proof.block_hash")
    if proof_hash != block_hash:
        raise AnalysisError(
            "decision_proof.block_hash does not match the committed block hash"
        )

    configuration_key = (epoch_number, epoch_digest, tree_id)
    try:
        leader_replica = leader_by_configuration[configuration_key]
    except KeyError as exc:
        raise AnalysisError(
            "missing validated leader mapping for configuration "
            f"{configuration_key!r}"
        ) from exc
    leader_replica = _require_integer(
        leader_replica, "leader mapping value"
    )
    if leader_replica >= REPLICA_COUNT:
        raise AnalysisError(
            f"leader mapping value must be in [0, {REPLICA_COUNT - 1}]"
        )

    return CommitEvent(
        run_id=expected_run_id,
        source_kind="replica",
        source_id=AUTHORITATIVE_SOURCE_ID,
        source_instance=source_instance,
        source_sequence=_require_integer(
            envelope.source_sequence,
            "source_sequence",
            minimum=1,
            maximum=UINT64_MAX,
        ),
        timestamp_ns=_require_integer(
            envelope.timestamp_ns,
            "source_monotonic_ns",
            maximum=UINT64_MAX,
        ),
        observer_replica=AUTHORITATIVE_OBSERVER,
        height=height,
        block_hash=block_hash,
        parent_hash=parent_hash,
        epoch_number=epoch_number,
        tree_id=tree_id,
        epoch_digest=epoch_digest,
        leader_replica=leader_replica,
        transaction_count=transaction_count,
        view_generation=view_generation,
        commit_batch_index=commit_batch_index,
    )


def parse_structured_events(
    text: str,
    *,
    expected_run_id: str,
    expected_source_kind: str,
    expected_source_id: str,
    expected_source_instance: str,
    allow_legacy_prefix: bool = False,
) -> tuple[StructuredEvent, ...]:
    """Parse one source-bound canonical raw JSONL stream.

    Production mode accepts one JSON object per non-empty line and rejects
    diagnostic text. ``allow_legacy_prefix`` is intentionally explicit and is
    provided only to inspect historical mixed logs whose event records begin
    with ``KAURI_EVENT ``. A full evidence validator must never enable it.
    """
    if not isinstance(expected_run_id, str) or not expected_run_id:
        raise AnalysisError("expected_run_id must be a non-empty string")
    if not isinstance(expected_source_kind, str) or not expected_source_kind:
        raise AnalysisError("expected_source_kind must be a non-empty string")
    if not isinstance(expected_source_id, str) or not expected_source_id:
        raise AnalysisError("expected_source_id must be a non-empty string")
    if (
        not isinstance(expected_source_instance, str)
        or not expected_source_instance
    ):
        raise AnalysisError(
            "expected_source_instance must be a non-empty string"
        )

    events: list[StructuredEvent] = []
    previous_sequence: int | None = None
    previous_timestamp_ns: int | None = None

    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        payload_text = line
        if allow_legacy_prefix:
            if not line.startswith(LEGACY_EVENT_PREFIX):
                if "KAURI_EVENT" in line:
                    raise AnalysisError(
                        f"line {line_number}: KAURI_EVENT must start the log line"
                    )
                continue
            payload_text = line[len(LEGACY_EVENT_PREFIX) :]
        elif line.startswith(LEGACY_EVENT_PREFIX):
            raise AnalysisError(
                f"line {line_number}: legacy KAURI_EVENT prefix is not "
                "canonical raw JSONL"
            )
        elif "KAURI_EVENT" in line:
            raise AnalysisError(
                f"line {line_number}: diagnostic KAURI_EVENT text is not "
                "canonical raw JSONL"
            )

        envelope = _load_canonical_json(payload_text, line_number)
        try:
            _require_exact_fields(envelope, _ENVELOPE_FIELDS, "event envelope")
            schema = _require_integer(
                envelope["event_schema_version"],
                "event_schema_version",
                maximum=UINT32_MAX,
            )
            if schema != EVENT_SCHEMA_VERSION:
                raise AnalysisError(
                    f"unsupported event_schema_version: {schema}"
                )
            if envelope["run_id"] != expected_run_id:
                raise AnalysisError("structured event run_id mismatch")
            if envelope["source_kind"] != expected_source_kind:
                raise AnalysisError(
                    "structured event source_kind mismatch: expected "
                    f"{expected_source_kind}"
                )
            if envelope["source_id"] != expected_source_id:
                raise AnalysisError(
                    "structured event source_id mismatch: expected "
                    f"{expected_source_id}"
                )
            source_instance = envelope["source_instance"]
            if not isinstance(source_instance, str) or not source_instance:
                raise AnalysisError("source_instance must be a non-empty string")
            if source_instance != expected_source_instance:
                raise AnalysisError("structured event source_instance mismatch")

            sequence = _require_integer(
                envelope["source_sequence"],
                "source_sequence",
                minimum=1,
                maximum=UINT64_MAX,
            )
            timestamp_ns = _require_integer(
                envelope["source_monotonic_ns"],
                "source_monotonic_ns",
                maximum=UINT64_MAX,
            )
            if previous_sequence is not None and sequence <= previous_sequence:
                raise AnalysisError("source_sequence is not strictly increasing")
            if (
                previous_timestamp_ns is not None
                and timestamp_ns < previous_timestamp_ns
            ):
                raise AnalysisError("source_monotonic_ns regressed")
            previous_sequence = sequence
            previous_timestamp_ns = timestamp_ns

            if not isinstance(envelope["event_type"], str):
                raise AnalysisError("event_type must be a string")
            if not isinstance(envelope["payload"], dict):
                raise AnalysisError("event payload must be an object")
            events.append(
                StructuredEvent(
                    run_id=expected_run_id,
                    source_kind=expected_source_kind,
                    source_id=expected_source_id,
                    source_instance=source_instance,
                    source_sequence=sequence,
                    timestamp_ns=timestamp_ns,
                    event_type=envelope["event_type"],
                    payload=envelope["payload"],
                )
            )
        except AnalysisError as exc:
            raise AnalysisError(f"line {line_number}: {exc}") from exc

    return tuple(events)


def parse_commit_events(
    text: str,
    *,
    expected_run_id: str,
    leader_by_configuration: Mapping[ConfigurationKey, int],
    expected_source_instance: str,
    allow_legacy_prefix: bool = False,
) -> tuple[CommitEvent, ...]:
    """Parse authoritative commits from replica-2 raw structured JSONL.

    All events are source-bound and ordered before non-commit records are
    ignored. Exact same-hash commit replays retain the first envelope after
    substantive metadata agrees.
    """
    envelopes = parse_structured_events(
        text,
        expected_run_id=expected_run_id,
        expected_source_kind="replica",
        expected_source_id=AUTHORITATIVE_SOURCE_ID,
        expected_source_instance=expected_source_instance,
        allow_legacy_prefix=allow_legacy_prefix,
    )

    events: list[CommitEvent] = []
    commits_by_hash: dict[str, CommitEvent] = {}
    heights: dict[int, str] = {}
    for envelope in envelopes:
        if envelope.event_type != "block.committed":
            continue
        try:
            event = _parse_commit(
                envelope,
                expected_run_id=expected_run_id,
                source_instance=envelope.source_instance,
                leader_by_configuration=leader_by_configuration,
            )
            existing_commit = commits_by_hash.get(event.block_hash)
            if existing_commit is not None:
                if (
                    existing_commit.substantive_identity
                    == event.substantive_identity
                ):
                    continue
                raise AnalysisError(
                    "authoritative commit hash has conflicting metadata: "
                    f"{event.block_hash}"
                )
            existing_hash = heights.get(event.height)
            if existing_hash is not None:
                raise AnalysisError(
                    "conflicting authoritative commits at height "
                    f"{event.height}: {existing_hash} and {event.block_hash}"
                )
            commits_by_hash[event.block_hash] = event
            heights[event.height] = event.block_hash
            events.append(event)
        except AnalysisError as exc:
            raise AnalysisError(
                f"source_sequence {envelope.source_sequence}: {exc}"
            ) from exc

    return tuple(events)


def _validate_legacy_boundaries(boundaries: PhaseBoundaries) -> None:
    values = (
        boundaries.baseline_start_ns,
        boundaries.crash_ns,
        boundaries.activation_ns,
        boundaries.end_ns,
    )
    if any(
        type(value) is not int or not 0 <= value <= UINT64_MAX
        for value in values
    ):
        raise AnalysisError(
            "phase boundaries must be unsigned 64-bit integers"
        )
    if not (
        boundaries.baseline_start_ns
        < boundaries.crash_ns
        < boundaries.activation_ns
        < boundaries.end_ns
    ):
        raise AnalysisError(
            "phase boundaries must satisfy baseline < crash < activation < end"
        )


def _validate_phase_windows(windows: Sequence[PhaseWindow]) -> None:
    if tuple(window.phase for window in windows) != _RECURRING_PHASE_NAMES:
        raise AnalysisError(
            "recurring phase windows must be exactly baseline, degraded, "
            "containment, optimized"
        )
    previous_end_ns: int | None = None
    for window in windows:
        if (
            type(window.epoch_number) is not int
            or not 0 <= window.epoch_number <= UINT32_MAX
            or type(window.start_ns) is not int
            or type(window.end_ns) is not int
            or not 0 <= window.start_ns <= UINT64_MAX
            or not 0 <= window.end_ns <= UINT64_MAX
        ):
            raise AnalysisError(
                "phase windows require unsigned epoch and timestamp integers"
            )
        if window.start_ns >= window.end_ns:
            raise AnalysisError("phase window start must precede its end")
        if previous_end_ns is not None and window.start_ns < previous_end_ns:
            raise AnalysisError("phase windows overlap or regress")
        previous_end_ns = window.end_ns


def _phase_intervals(
    boundaries: PhaseBoundaries | Sequence[PhaseWindow],
) -> tuple[tuple[str, int, int, int | None], ...]:
    if isinstance(boundaries, PhaseBoundaries):
        _validate_legacy_boundaries(boundaries)
        return (
            (
                "baseline",
                boundaries.baseline_start_ns,
                boundaries.crash_ns,
                None,
            ),
            (
                "degraded",
                boundaries.crash_ns,
                boundaries.activation_ns,
                None,
            ),
            ("post", boundaries.activation_ns, boundaries.end_ns, None),
        )
    windows = tuple(boundaries)
    _validate_phase_windows(windows)
    return (
        *(
            (window.phase, window.start_ns, window.end_ns, window.epoch_number)
            for window in windows
        ),
    )


def _validated_unique_commits(
    events: Sequence[CommitEvent],
) -> list[CommitEvent]:
    previous_timestamp_ns: int | None = None
    commits_by_hash: dict[str, CommitEvent] = {}
    heights: dict[int, str] = {}
    unique_events: list[CommitEvent] = []
    for event in events:
        if (
            event.source_kind != "replica"
            or event.source_id != AUTHORITATIVE_SOURCE_ID
            or event.observer_replica != AUTHORITATIVE_OBSERVER
        ):
            raise AnalysisError("throughput input contains a non-authoritative event")
        _require_integer(
            event.source_sequence,
            "throughput source_sequence",
            minimum=1,
            maximum=UINT64_MAX,
        )
        _require_integer(
            event.timestamp_ns,
            "throughput timestamp_ns",
            maximum=UINT64_MAX,
        )
        _require_integer(
            event.height,
            "throughput height",
            minimum=1,
            maximum=UINT64_MAX,
        )
        _require_integer(
            event.transaction_count,
            "throughput transaction_count",
            maximum=UINT64_MAX,
        )
        _require_integer(
            event.epoch_number,
            "throughput epoch_number",
            maximum=UINT32_MAX,
        )
        _require_integer(
            event.tree_id,
            "throughput tree_id",
            maximum=UINT32_MAX,
        )
        if event.view_generation is not None:
            _require_integer(
                event.view_generation,
                "throughput view_generation",
                maximum=UINT64_MAX,
            )
        _require_integer(
            event.commit_batch_index,
            "throughput commit_batch_index",
            maximum=UINT64_MAX,
        )
        _require_hash(event.block_hash, "throughput block_hash")
        _require_hash(event.epoch_digest, "throughput epoch_digest")
        if event.parent_hash is not None:
            _require_hash(event.parent_hash, "throughput parent_hash")
        if (
            type(event.leader_replica) is not int
            or not 0 <= event.leader_replica < REPLICA_COUNT
        ):
            raise AnalysisError("throughput input contains an invalid leader")
        if previous_timestamp_ns is not None and (
            event.timestamp_ns < previous_timestamp_ns
        ):
            raise AnalysisError("throughput input timestamps regressed")
        previous_timestamp_ns = event.timestamp_ns

        existing_commit = commits_by_hash.get(event.block_hash)
        if existing_commit is not None:
            if (
                existing_commit.substantive_identity
                == event.substantive_identity
            ):
                continue
            raise AnalysisError(
                "throughput commit hash has conflicting metadata: "
                f"{event.block_hash}"
            )
        existing_hash = heights.get(event.height)
        if existing_hash is not None:
            raise AnalysisError(
                "throughput commits conflict at height "
                f"{event.height}: {existing_hash} and {event.block_hash}"
            )
        commits_by_hash[event.block_hash] = event
        heights[event.height] = event.block_hash
        unique_events.append(event)
    return unique_events


def _build_phase_buckets(
    unique_events: Sequence[CommitEvent],
    boundaries: PhaseBoundaries | Sequence[PhaseWindow],
) -> tuple[ThroughputBucket, ...]:
    intervals = _phase_intervals(boundaries)
    measurement_start_ns = intervals[0][1]
    measurement_end_ns = intervals[-1][2]
    in_window = [
        event
        for event in unique_events
        if measurement_start_ns <= event.timestamp_ns < measurement_end_ns
    ]
    event_index = 0
    buckets: list[ThroughputBucket] = []

    for phase, phase_start_ns, phase_end_ns, expected_epoch in intervals:
        bucket_start_ns = phase_start_ns
        while bucket_start_ns < phase_end_ns:
            bucket_end_ns = min(
                bucket_start_ns + BUCKET_WIDTH_NS, phase_end_ns
            )
            leader_transactions = [0] * REPLICA_COUNT
            commit_count = 0
            while (
                event_index < len(in_window)
                and in_window[event_index].timestamp_ns < bucket_end_ns
            ):
                event = in_window[event_index]
                if event.timestamp_ns >= bucket_start_ns:
                    if (
                        expected_epoch is not None
                        and event.epoch_number != expected_epoch
                    ):
                        raise AnalysisError(
                            f"{phase} phase contains epoch {event.epoch_number}; "
                            f"expected epoch {expected_epoch}"
                        )
                    leader_transactions[event.leader_replica] += (
                        event.transaction_count
                    )
                    commit_count += 1
                event_index += 1

            elapsed_seconds = (
                bucket_end_ns - bucket_start_ns
            ) / 1_000_000_000
            leader_tps = tuple(
                transactions / elapsed_seconds
                for transactions in leader_transactions
            )
            aggregate_tps = sum(leader_tps)
            bucket = ThroughputBucket(
                bucket_index=len(buckets),
                phase=phase,
                start_ns=bucket_start_ns,
                end_ns=bucket_end_ns,
                elapsed_seconds=elapsed_seconds,
                commit_count=commit_count,
                transaction_count=sum(leader_transactions),
                aggregate_tps=aggregate_tps,
                leader_transactions=tuple(  # type: ignore[arg-type]
                    leader_transactions
                ),
                leader_tps=leader_tps,  # type: ignore[arg-type]
            )
            if sum(bucket.leader_tps) != bucket.aggregate_tps:
                raise AnalysisError(
                    "per-leader throughput does not conserve aggregate"
                )
            buckets.append(bucket)
            bucket_start_ns = bucket_end_ns

    return tuple(buckets)


def build_throughput_buckets(
    events: Sequence[CommitEvent],
    boundaries: PhaseBoundaries | Sequence[PhaseWindow],
) -> tuple[ThroughputBucket, ...]:
    """Build max-five-second phase-local buckets with explicit zero rows.

    Buckets are half-open.  A crash- or activation-boundary commit therefore
    belongs to the new phase.  Each phase starts a fresh five-second grid so a
    bucket never mixes two phases; a phase's final bucket may be shorter than
    five seconds and uses its actual elapsed duration. Exact hash replays are
    defensively deduplicated again before attribution.
    """
    _phase_intervals(boundaries)
    unique_events = _validated_unique_commits(events)
    return _build_phase_buckets(unique_events, boundaries)


def compute_phase_medians(
    buckets: Iterable[ThroughputBucket],
) -> PhaseMedians:
    """Compute medians from the raw buckets, including explicit zeroes."""
    bucket_values = tuple(buckets)
    phases = tuple(dict.fromkeys(bucket.phase for bucket in bucket_values))
    if phases == _LEGACY_PHASE_NAMES:
        expected_phases = _LEGACY_PHASE_NAMES
    elif phases == _RECURRING_PHASE_NAMES:
        expected_phases = _RECURRING_PHASE_NAMES
    else:
        raise AnalysisError(
            "throughput buckets must contain one complete legacy or recurring "
            "phase sequence"
        )
    values: dict[str, list[float]] = {
        phase: [] for phase in expected_phases
    }
    for bucket in bucket_values:
        if bucket.phase not in values:
            raise AnalysisError(f"unknown throughput phase: {bucket.phase}")
        values[bucket.phase].append(bucket.aggregate_tps)
    missing = [phase for phase in expected_phases if not values[phase]]
    if missing:
        raise AnalysisError(
            "cannot compute medians without buckets for: " + ", ".join(missing)
        )
    baseline_tps = float(statistics.median(values["baseline"]))
    degraded_tps = float(statistics.median(values["degraded"]))
    if expected_phases == _LEGACY_PHASE_NAMES:
        return PhaseMedians(
            baseline_tps=baseline_tps,
            degraded_tps=degraded_tps,
            post_tps=float(statistics.median(values["post"])),
        )
    return PhaseMedians(
        baseline_tps=baseline_tps,
        degraded_tps=degraded_tps,
        containment_tps=float(statistics.median(values["containment"])),
        optimized_tps=float(statistics.median(values["optimized"])),
    )


def analyze_throughput(
    events: Sequence[CommitEvent],
    boundaries: PhaseBoundaries | Sequence[PhaseWindow],
) -> ThroughputAnalysis:
    buckets = build_throughput_buckets(events, boundaries)
    return ThroughputAnalysis(
        buckets=buckets,
        medians=compute_phase_medians(buckets),
    )
