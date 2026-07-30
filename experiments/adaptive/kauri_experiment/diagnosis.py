"""Exact bounded diagnosis for the adaptive experiment harness.

The oracle keeps every static two-mode hypothesis compatible with accepted
reporter-target observations.  A hypothesis contains disjoint sets of false
reporters ``L`` and persistent omitters ``C`` with ``|L| + |C| <= td``.
For a reporter outside ``L``, a timeout is expected exactly when the target is
in ``C``.  Reports made by a member of ``L`` are deliberately unconstrained.

This module is diagnostic only.  It has no quorum, voting, membership, epoch,
or tree-policy authority.  Experiment fault labels belong to the validator
and are not inputs to the oracle.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from fractions import Fraction
from itertools import combinations, islice
import json
from math import comb
from typing import Literal, TypeAlias


MAXIMUM_MEMBERS = 31
MAXIMUM_DIAGNOSTIC_FAULT_BOUND = 3
DEFAULT_MAXIMUM_HYPOTHESES = 37_883
DEFAULT_MAXIMUM_OBSERVATIONS = 4_096
DEFAULT_MAXIMUM_JSONL_RECORDS = 4_096
DEFAULT_MAXIMUM_JSONL_LINE_BYTES = 64 * 1_024

MANAGER_EVIDENCE_PROJECTION_SCOPE = (
    "development-only projection: reputation.evidence_applied omits exact "
    "tree/block context and therefore cannot support campaign evidence"
)

DiagnosticOutcome: TypeAlias = Literal["response", "timeout"]
DefinitiveMode: TypeAlias = Literal[
    "false_reporter",
    "persistent_omitter",
    "non_faulty",
]


class DiagnosisError(ValueError):
    """The requested diagnosis operation is invalid or unsafe."""


class DiagnosisCapacityError(DiagnosisError):
    """A frozen diagnosis capacity would be exceeded."""


class DiagnosisContradictionError(DiagnosisError):
    """An observation would eliminate every bounded hypothesis."""


class ManagerEvidenceProjectionError(DiagnosisError):
    """Manager JSONL cannot be projected without ambiguity."""


class ManagerEvidenceProjectionCapacityError(
    ManagerEvidenceProjectionError,
    DiagnosisCapacityError,
):
    """A manager JSONL projection capacity would be exceeded."""


def _require_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DiagnosisError(f"{field} must be an integer")
    return value


def _require_positive_capacity(value: object, field: str) -> int:
    capacity = _require_integer(value, field)
    if capacity < 1:
        raise DiagnosisCapacityError(f"{field} must be at least one")
    return capacity


def _canonical_membership(membership: Iterable[int]) -> tuple[int, ...]:
    if isinstance(membership, (str, bytes)):
        raise DiagnosisError("membership must be an iterable of replica ids")
    try:
        replica_ids = tuple(
            islice(iter(membership), MAXIMUM_MEMBERS + 1)
        )
    except TypeError as error:
        raise DiagnosisError(
            "membership must be an iterable of replica ids"
        ) from error

    if not replica_ids:
        raise DiagnosisError("membership must be non-empty")
    if len(replica_ids) > MAXIMUM_MEMBERS:
        raise DiagnosisCapacityError(
            f"membership cannot exceed {MAXIMUM_MEMBERS} replicas"
        )
    if any(
        isinstance(replica_id, bool) or not isinstance(replica_id, int)
        for replica_id in replica_ids
    ):
        raise DiagnosisError("membership replica ids must be integers")
    if any(replica_id < 0 for replica_id in replica_ids):
        raise DiagnosisError("membership replica ids must be non-negative")
    if len(set(replica_ids)) != len(replica_ids):
        raise DiagnosisError("membership replica ids must be unique")
    return tuple(sorted(replica_ids))


@dataclass(frozen=True, order=True, slots=True)
class DiagnosticObservation:
    """One accepted attempt projected to the two-mode diagnostic relation."""

    attempt_id: str
    reporter_id: int
    target_id: int
    outcome: DiagnosticOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, str) or not self.attempt_id.strip():
            raise DiagnosisError("attempt id must be a non-empty string")
        _require_integer(self.reporter_id, "reporter id")
        _require_integer(self.target_id, "target id")
        if self.reporter_id < 0 or self.target_id < 0:
            raise DiagnosisError(
                "observation endpoints must be non-negative"
            )
        if self.reporter_id == self.target_id:
            raise DiagnosisError(
                "observation reporter and target must be distinct"
            )
        if self.outcome not in ("response", "timeout"):
            raise DiagnosisError(
                "observation outcome must be response or timeout"
            )


@dataclass(frozen=True, order=True, slots=True)
class FaultHypothesis:
    """One canonical assignment of bounded diagnostic modes."""

    false_reporters: tuple[int, ...]
    persistent_omitters: tuple[int, ...]

    def compatible_with(self, observation: DiagnosticObservation) -> bool:
        if observation.reporter_id in self.false_reporters:
            return True
        expected_outcome: DiagnosticOutcome = (
            "timeout"
            if observation.target_id in self.persistent_omitters
            else "response"
        )
        return observation.outcome == expected_outcome


@dataclass(frozen=True, slots=True)
class ReplicaModeCounts:
    """Exact compatible-hypothesis counts for one replica.

    The fractions are hypothesis mass, not calibrated probabilities.
    """

    replica_id: int
    compatible_hypotheses: int
    false_reporter_hypotheses: int
    persistent_omitter_hypotheses: int

    @property
    def false_reporter_mass(self) -> Fraction:
        return Fraction(
            self.false_reporter_hypotheses,
            self.compatible_hypotheses,
        )

    @property
    def persistent_omitter_mass(self) -> Fraction:
        return Fraction(
            self.persistent_omitter_hypotheses,
            self.compatible_hypotheses,
        )

    @property
    def definitive_mode(self) -> DefinitiveMode | None:
        if self.false_reporter_hypotheses == self.compatible_hypotheses:
            return "false_reporter"
        if self.persistent_omitter_hypotheses == (
            self.compatible_hypotheses
        ):
            return "persistent_omitter"
        if (
            self.false_reporter_hypotheses == 0
            and self.persistent_omitter_hypotheses == 0
        ):
            return "non_faulty"
        return None


@dataclass(frozen=True, slots=True)
class DiagnosisSnapshot:
    """Immutable deterministic view of one diagnosis state."""

    hypotheses: tuple[FaultHypothesis, ...]
    observations: tuple[DiagnosticObservation, ...]
    mode_counts: tuple[ReplicaModeCounts, ...]

    @property
    def compatible_hypothesis_count(self) -> int:
        return len(self.hypotheses)

    @property
    def observation_count(self) -> int:
        return len(self.observations)

    def mode_count_for(self, replica_id: int) -> ReplicaModeCounts:
        replica_id = _require_integer(replica_id, "replica id")
        for counts in self.mode_counts:
            if counts.replica_id == replica_id:
                return counts
        raise DiagnosisError("replica id is outside diagnosis membership")


def _bounded_hypothesis_count(
    membership_size: int,
    diagnostic_fault_bound: int,
) -> int:
    return sum(
        comb(membership_size, faulty_count) * (2**faulty_count)
        for faulty_count in range(diagnostic_fault_bound + 1)
    )


def _enumerate_hypotheses(
    membership: tuple[int, ...],
    diagnostic_fault_bound: int,
) -> tuple[FaultHypothesis, ...]:
    hypotheses: list[FaultHypothesis] = []
    for false_reporter_count in range(diagnostic_fault_bound + 1):
        for false_reporters in combinations(
            membership,
            false_reporter_count,
        ):
            remaining = tuple(
                replica_id
                for replica_id in membership
                if replica_id not in false_reporters
            )
            maximum_omitters = (
                diagnostic_fault_bound - false_reporter_count
            )
            for omitter_count in range(maximum_omitters + 1):
                for persistent_omitters in combinations(
                    remaining,
                    omitter_count,
                ):
                    hypotheses.append(
                        FaultHypothesis(
                            false_reporters=false_reporters,
                            persistent_omitters=persistent_omitters,
                        )
                    )
    return tuple(sorted(hypotheses))


class BoundedTwoModeDiagnosis:
    """Incremental exact oracle with frozen memory capacities."""

    def __init__(
        self,
        membership: Iterable[int],
        diagnostic_fault_bound: int,
        *,
        maximum_hypotheses: int = DEFAULT_MAXIMUM_HYPOTHESES,
        maximum_observations: int = DEFAULT_MAXIMUM_OBSERVATIONS,
    ) -> None:
        self._membership = _canonical_membership(membership)
        self._membership_set = frozenset(self._membership)

        fault_bound = _require_integer(
            diagnostic_fault_bound,
            "diagnostic fault bound",
        )
        if fault_bound < 0:
            raise DiagnosisError(
                "diagnostic fault bound must be non-negative"
            )
        if fault_bound > MAXIMUM_DIAGNOSTIC_FAULT_BOUND:
            raise DiagnosisCapacityError(
                "diagnostic fault bound cannot exceed "
                f"{MAXIMUM_DIAGNOSTIC_FAULT_BOUND}"
            )
        if fault_bound > len(self._membership):
            raise DiagnosisError(
                "diagnostic fault bound cannot exceed membership"
            )
        self._diagnostic_fault_bound = fault_bound

        hypothesis_capacity = _require_positive_capacity(
            maximum_hypotheses,
            "hypothesis capacity",
        )
        self._maximum_observations = _require_positive_capacity(
            maximum_observations,
            "observation capacity",
        )
        required_hypotheses = _bounded_hypothesis_count(
            len(self._membership),
            fault_bound,
        )
        if required_hypotheses > hypothesis_capacity:
            raise DiagnosisCapacityError(
                "hypothesis capacity is smaller than the exact initial "
                f"set ({hypothesis_capacity} < {required_hypotheses})"
            )

        hypotheses = _enumerate_hypotheses(
            self._membership,
            fault_bound,
        )
        if len(hypotheses) != required_hypotheses:
            raise DiagnosisError(
                "internal bounded-hypothesis enumeration mismatch"
            )
        self._compatible_hypotheses = hypotheses
        self._observations_by_attempt: dict[
            str,
            DiagnosticObservation,
        ] = {}

    @property
    def membership(self) -> tuple[int, ...]:
        return self._membership

    @property
    def diagnostic_fault_bound(self) -> int:
        return self._diagnostic_fault_bound

    @property
    def hypothesis_count(self) -> int:
        return len(self._compatible_hypotheses)

    def observe(
        self,
        observation: DiagnosticObservation,
    ) -> DiagnosisSnapshot:
        if type(observation) is not DiagnosticObservation:
            raise DiagnosisError(
                "observation must be an exact DiagnosticObservation"
            )
        if (
            observation.reporter_id not in self._membership_set
            or observation.target_id not in self._membership_set
        ):
            raise DiagnosisError(
                "observation endpoint is outside diagnosis membership"
            )

        existing = self._observations_by_attempt.get(
            observation.attempt_id
        )
        if existing is not None:
            if existing != observation:
                raise DiagnosisError(
                    "conflicting duplicate attempt observation"
                )
            return self.snapshot()

        if (
            len(self._observations_by_attempt)
            >= self._maximum_observations
        ):
            raise DiagnosisCapacityError(
                "observation capacity would be exceeded"
            )

        compatible = tuple(
            hypothesis
            for hypothesis in self._compatible_hypotheses
            if hypothesis.compatible_with(observation)
        )
        if not compatible:
            raise DiagnosisContradictionError(
                "observation leaves no compatible bounded hypothesis"
            )

        self._compatible_hypotheses = compatible
        self._observations_by_attempt[observation.attempt_id] = observation
        return self.snapshot()

    def observe_many(
        self,
        observations: Iterable[DiagnosticObservation],
    ) -> DiagnosisSnapshot:
        for observation in observations:
            self.observe(observation)
        return self.snapshot()

    def snapshot(self) -> DiagnosisSnapshot:
        denominator = len(self._compatible_hypotheses)
        counts = tuple(
            ReplicaModeCounts(
                replica_id=replica_id,
                compatible_hypotheses=denominator,
                false_reporter_hypotheses=sum(
                    replica_id in hypothesis.false_reporters
                    for hypothesis in self._compatible_hypotheses
                ),
                persistent_omitter_hypotheses=sum(
                    replica_id in hypothesis.persistent_omitters
                    for hypothesis in self._compatible_hypotheses
                ),
            )
            for replica_id in self._membership
        )
        return DiagnosisSnapshot(
            hypotheses=self._compatible_hypotheses,
            observations=tuple(
                sorted(self._observations_by_attempt.values())
            ),
            mode_counts=counts,
        )


def _projection_error(line_number: int, message: str) -> (
    ManagerEvidenceProjectionError
):
    return ManagerEvidenceProjectionError(
        f"manager evidence JSONL record {line_number}: {message}"
    )


def project_manager_evidence_jsonl(
    records: Iterable[str],
    *,
    membership: Iterable[int],
    maximum_records: int = DEFAULT_MAXIMUM_JSONL_RECORDS,
    maximum_line_bytes: int = DEFAULT_MAXIMUM_JSONL_LINE_BYTES,
) -> tuple[DiagnosticObservation, ...]:
    """Project accepted manager audit events to diagnostic observations.

    This helper intentionally ignores scalar score fields.  It is
    development-only because the audit event omits the exact configuration,
    tree, block, and expected-message identity needed for campaign evidence.
    Late outcomes fail closed because this projection cannot return exclusion
    metadata without making an incomplete history look authoritative.
    """

    try:
        canonical_membership = _canonical_membership(membership)
    except DiagnosisError as error:
        raise ManagerEvidenceProjectionError(str(error)) from error
    membership_set = frozenset(canonical_membership)
    record_capacity = _require_positive_capacity(
        maximum_records,
        "record capacity",
    )
    line_capacity = _require_positive_capacity(
        maximum_line_bytes,
        "JSONL line capacity",
    )

    projected_by_attempt: dict[str, DiagnosticObservation] = {}
    nonblank_record_count = 0
    for line_number, line in enumerate(records, start=1):
        if not isinstance(line, str):
            raise _projection_error(
                line_number,
                "record must be a string",
            )
        if not line.strip():
            continue

        nonblank_record_count += 1
        if nonblank_record_count > record_capacity:
            raise ManagerEvidenceProjectionCapacityError(
                "manager evidence JSONL record capacity would be exceeded"
            )
        if len(line.encode("utf-8")) > line_capacity:
            raise ManagerEvidenceProjectionCapacityError(
                "manager evidence JSONL line capacity would be exceeded"
            )

        try:
            decoded = json.loads(line)
        except (json.JSONDecodeError, UnicodeError) as error:
            raise _projection_error(
                line_number,
                "invalid JSON",
            ) from error
        if not isinstance(decoded, Mapping):
            raise _projection_error(
                line_number,
                "top-level value must be an object",
            )

        event_type = decoded.get("event_type")
        if not isinstance(event_type, str):
            raise _projection_error(
                line_number,
                "event_type must be a string",
            )
        if event_type != "reputation.evidence_applied":
            continue

        if decoded.get("event_schema_version") != 1:
            raise _projection_error(
                line_number,
                "unsupported event schema version",
            )
        if decoded.get("source_kind") != "adaptation_manager":
            raise _projection_error(
                line_number,
                "event source is not the adaptation manager",
            )
        payload = decoded.get("payload")
        if not isinstance(payload, Mapping):
            raise _projection_error(
                line_number,
                "payload must be an object",
            )

        observation_id = payload.get("observation_id")
        reporter_id = payload.get("reporter_id")
        target_id = payload.get("target_id")
        evidence_outcome = payload.get("evidence_outcome")
        if (
            not isinstance(observation_id, str)
            or not observation_id.strip()
        ):
            raise _projection_error(
                line_number,
                "observation_id must be a non-empty string",
            )
        if (
            isinstance(reporter_id, bool)
            or not isinstance(reporter_id, int)
            or isinstance(target_id, bool)
            or not isinstance(target_id, int)
        ):
            raise _projection_error(
                line_number,
                "reporter and target ids must be integers",
            )
        if (
            reporter_id not in membership_set
            or target_id not in membership_set
        ):
            raise _projection_error(
                line_number,
                "observation endpoint is outside membership",
            )
        if reporter_id == target_id:
            raise _projection_error(
                line_number,
                "observation reporter and target must be distinct",
            )
        if evidence_outcome == "timeout":
            diagnostic_outcome: DiagnosticOutcome = "timeout"
        elif evidence_outcome == "on_time":
            diagnostic_outcome = "response"
        elif evidence_outcome == "late":
            raise _projection_error(
                line_number,
                "late outcome makes the development projection incomplete",
            )
        else:
            raise _projection_error(
                line_number,
                "unsupported evidence outcome",
            )

        observation = DiagnosticObservation(
            attempt_id=observation_id,
            reporter_id=reporter_id,
            target_id=target_id,
            outcome=diagnostic_outcome,
        )
        existing = projected_by_attempt.get(observation_id)
        if existing is not None:
            if existing != observation:
                raise _projection_error(
                    line_number,
                    "conflicting duplicate attempt observation",
                )
            continue
        projected_by_attempt[observation_id] = observation

    return tuple(projected_by_attempt.values())
