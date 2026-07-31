"""Proof-gated passive cross-checks for aggregate-relay observations.

The certificate builder consumes manager-accepted observations in receipt
order.  It deliberately accepts no injected-fault label: conclusions come
only from the exact bounded two-mode diagnosis model.

This module is experiment evidence plumbing.  A certificate has no authority
over consensus membership, quorum, voting, commits, or active topology.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal, TypeAlias

from .diagnosis import (
    BoundedTwoModeDiagnosis,
    DiagnosisError,
    DiagnosticObservation,
    FaultHypothesis,
)


AGGREGATE_RELAY = "aggregate_relay"
ORDERING_BASIS = "manager_receipt_order"
_HEX_256 = re.compile(r"^[0-9a-f]{64}$")

PassiveCrosscheckOutcome: TypeAlias = Literal["response", "timeout"]


class PassiveCrosscheckError(ValueError):
    """The requested passive cross-check is invalid or out of scope."""


def _require_nonnegative_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PassiveCrosscheckError(f"{field} must be an integer")
    if value < 0:
        raise PassiveCrosscheckError(f"{field} must be non-negative")
    return value


def _require_hex_256(value: object, field: str) -> str:
    if not isinstance(value, str) or _HEX_256.fullmatch(value) is None:
        raise PassiveCrosscheckError(
            f"{field} must be lowercase 256-bit hex"
        )
    return value


@dataclass(frozen=True, order=True, slots=True)
class PassiveCrosscheckPhase:
    """One expected aggregate-relay phase in protocol rotation order."""

    tree_id: int
    reporter_id: int

    def __post_init__(self) -> None:
        _require_nonnegative_integer(self.tree_id, "phase tree id")
        _require_nonnegative_integer(
            self.reporter_id,
            "phase reporter id",
        )


@dataclass(frozen=True, slots=True)
class PassiveCrosscheckScope:
    """Exact configuration and phase scope of one passive cross-check."""

    epoch_number: int
    epoch_digest: str
    target_id: int
    expected_message_type: str
    phases: tuple[PassiveCrosscheckPhase, ...]
    diagnostic_fault_bound: int = 1

    def __post_init__(self) -> None:
        _require_nonnegative_integer(
            self.epoch_number,
            "scope epoch number",
        )
        _require_hex_256(self.epoch_digest, "scope epoch digest")
        _require_nonnegative_integer(self.target_id, "scope target id")
        if (
            not isinstance(self.expected_message_type, str)
            or not self.expected_message_type
        ):
            raise PassiveCrosscheckError(
                "scope expected message type must be non-empty"
            )
        if isinstance(self.phases, (str, bytes)):
            raise PassiveCrosscheckError(
                "scope phases must be an iterable of phases"
            )
        try:
            canonical_phases = tuple(self.phases)
        except TypeError as error:
            raise PassiveCrosscheckError(
                "scope phases must be an iterable of phases"
            ) from error
        if len(canonical_phases) != 2:
            raise PassiveCrosscheckError(
                "scope must contain exactly two cross-check phases"
            )
        if any(
            type(phase) is not PassiveCrosscheckPhase
            for phase in canonical_phases
        ):
            raise PassiveCrosscheckError(
                "scope phases must be exact PassiveCrosscheckPhase values"
            )
        if len({phase.tree_id for phase in canonical_phases}) != 2:
            raise PassiveCrosscheckError(
                "cross-check phases must use distinct trees"
            )
        if len({phase.reporter_id for phase in canonical_phases}) != 2:
            raise PassiveCrosscheckError(
                "cross-check phases must use distinct reporters"
            )
        if any(
            phase.reporter_id == self.target_id
            for phase in canonical_phases
        ):
            raise PassiveCrosscheckError(
                "phase reporter and target must be distinct"
            )
        _require_nonnegative_integer(
            self.diagnostic_fault_bound,
            "diagnostic fault bound",
        )
        object.__setattr__(self, "phases", canonical_phases)


@dataclass(frozen=True, order=True, slots=True)
class PassiveCrosscheckObservation:
    """One finalized manager-accepted exact observation."""

    observation_id: str
    reporter_id: int
    target_id: int
    epoch_number: int
    tree_id: int
    epoch_digest: str
    expected_message_type: str
    outcome: PassiveCrosscheckOutcome

    def __post_init__(self) -> None:
        _require_hex_256(self.observation_id, "observation id")
        _require_nonnegative_integer(
            self.reporter_id,
            "observation reporter id",
        )
        _require_nonnegative_integer(
            self.target_id,
            "observation target id",
        )
        if self.reporter_id == self.target_id:
            raise PassiveCrosscheckError(
                "observation reporter and target must be distinct"
            )
        _require_nonnegative_integer(
            self.epoch_number,
            "observation epoch number",
        )
        _require_nonnegative_integer(
            self.tree_id,
            "observation tree id",
        )
        _require_hex_256(
            self.epoch_digest,
            "observation epoch digest",
        )
        if (
            not isinstance(self.expected_message_type, str)
            or not self.expected_message_type
        ):
            raise PassiveCrosscheckError(
                "observation expected message type must be non-empty"
            )
        if self.outcome not in ("response", "timeout"):
            raise PassiveCrosscheckError(
                "observation outcome must be response or timeout"
            )


def _scope_value(scope: PassiveCrosscheckScope) -> dict[str, object]:
    return {
        "epoch_number": scope.epoch_number,
        "epoch_digest": scope.epoch_digest,
        "target_id": scope.target_id,
        "expected_message_type": scope.expected_message_type,
        "phases": [
            {
                "tree_id": phase.tree_id,
                "reporter_id": phase.reporter_id,
            }
            for phase in scope.phases
        ],
        "diagnostic_fault_bound": scope.diagnostic_fault_bound,
    }


def _observation_value(
    observation: PassiveCrosscheckObservation,
) -> dict[str, object]:
    return {
        "observation_id": observation.observation_id,
        "reporter_id": observation.reporter_id,
        "target_id": observation.target_id,
        "epoch_number": observation.epoch_number,
        "tree_id": observation.tree_id,
        "epoch_digest": observation.epoch_digest,
        "expected_message_type": (
            observation.expected_message_type
        ),
        "outcome": observation.outcome,
    }


def _hypothesis_value(
    hypothesis: FaultHypothesis,
) -> dict[str, list[int]]:
    return {
        "false_reporters": list(hypothesis.false_reporters),
        "persistent_omitters": list(
            hypothesis.persistent_omitters
        ),
    }


def _canonical_membership(membership: Iterable[int]) -> tuple[int, ...]:
    if isinstance(membership, (str, bytes)):
        raise PassiveCrosscheckError(
            "membership must be an iterable of replica ids"
        )
    try:
        members = tuple(membership)
    except TypeError as error:
        raise PassiveCrosscheckError(
            "membership must be an iterable of replica ids"
        ) from error
    if not members:
        raise PassiveCrosscheckError("membership must be non-empty")
    return members


def _validate_exact_scope(
    scope: PassiveCrosscheckScope,
    observations: tuple[PassiveCrosscheckObservation, ...],
) -> None:
    if scope.expected_message_type != AGGREGATE_RELAY:
        raise PassiveCrosscheckError(
            "passive cross-check message type must be aggregate_relay"
        )
    if scope.diagnostic_fault_bound != 1:
        raise PassiveCrosscheckError(
            "passive two-phase cross-check requires diagnostic fault "
            "bound one"
        )

    for phase_index, observation in enumerate(observations):
        phase = scope.phases[phase_index]
        if observation.expected_message_type != AGGREGATE_RELAY:
            raise PassiveCrosscheckError(
                "observation message type must be aggregate_relay"
            )
        if (
            observation.epoch_number != scope.epoch_number
            or observation.epoch_digest != scope.epoch_digest
            or observation.target_id != scope.target_id
            or observation.expected_message_type
            != scope.expected_message_type
            or observation.tree_id != phase.tree_id
            or observation.reporter_id != phase.reporter_id
        ):
            raise PassiveCrosscheckError(
                f"observation {phase_index} is outside exact phase scope"
            )

    if observations[0].outcome != "timeout":
        raise PassiveCrosscheckError(
            "the first cross-check observation must be a timeout"
        )
    if (
        len(observations) == 2
        and observations[0].observation_id
        == observations[1].observation_id
    ):
        raise PassiveCrosscheckError(
            "cross-check observations must have distinct ids"
        )


def _certificate_digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_passive_crosscheck_certificate(
    scope: PassiveCrosscheckScope,
    observations: Iterable[PassiveCrosscheckObservation],
    *,
    membership: Iterable[int] = range(7),
) -> dict[str, object]:
    """Build a deterministic proof-gated certificate.

    One first-phase timeout remains pending.  A finalized observation from
    the distinct second-phase reporter settles the two-mode diagnosis.
    """

    if type(scope) is not PassiveCrosscheckScope:
        raise PassiveCrosscheckError(
            "scope must be an exact PassiveCrosscheckScope"
        )
    if isinstance(observations, (str, bytes)):
        raise PassiveCrosscheckError(
            "observations must be an iterable of observations"
        )
    try:
        accepted = tuple(observations)
    except TypeError as error:
        raise PassiveCrosscheckError(
            "observations must be an iterable of observations"
        ) from error
    if len(accepted) not in (1, 2):
        raise PassiveCrosscheckError(
            "certificate requires one or two receipt-ordered observations"
        )
    if any(
        type(observation) is not PassiveCrosscheckObservation
        for observation in accepted
    ):
        raise PassiveCrosscheckError(
            "observations must be exact PassiveCrosscheckObservation "
            "values"
        )

    _validate_exact_scope(scope, accepted)
    members = _canonical_membership(membership)
    try:
        diagnosis = BoundedTwoModeDiagnosis(
            members,
            scope.diagnostic_fault_bound,
        )
        snapshot = diagnosis.observe_many(
            DiagnosticObservation(
                attempt_id=observation.observation_id,
                reporter_id=observation.reporter_id,
                target_id=observation.target_id,
                outcome=observation.outcome,
            )
            for observation in accepted
        )
    except DiagnosisError as error:
        raise PassiveCrosscheckError(str(error)) from error

    expected_hypothesis_count = 2 if len(accepted) == 1 else 1
    if (
        snapshot.compatible_hypothesis_count
        != expected_hypothesis_count
    ):
        raise PassiveCrosscheckError(
            "cross-check did not produce the exact bounded two-mode "
            "hypothesis count"
        )

    settled = (
        snapshot.hypotheses[0] if len(snapshot.hypotheses) == 1 else None
    )
    implicated = sorted(
        {
            replica_id
            for hypothesis in snapshot.hypotheses
            for replica_id in (
                *hypothesis.false_reporters,
                *hypothesis.persistent_omitters,
            )
        }
    )
    payload: dict[str, object] = {
        "schema_version": 1,
        "certificate_kind": (
            "passive_aggregate_relay_crosscheck"
        ),
        "ordering_basis": ORDERING_BASIS,
        "scope": _scope_value(scope),
        "observations": [
            _observation_value(observation)
            for observation in accepted
        ],
        "status": "settled" if settled is not None else "pending",
        "compatible_hypothesis_count": (
            snapshot.compatible_hypothesis_count
        ),
        "settled_hypothesis": (
            _hypothesis_value(settled)
            if settled is not None
            else None
        ),
        "durable_role_exclusions": (
            implicated if settled is not None else []
        ),
        "pending_endpoints": (
            [] if settled is not None else implicated
        ),
    }
    return {
        **payload,
        "certificate_sha256": _certificate_digest(payload),
    }


__all__ = (
    "PassiveCrosscheckError",
    "PassiveCrosscheckObservation",
    "PassiveCrosscheckOutcome",
    "PassiveCrosscheckPhase",
    "PassiveCrosscheckScope",
    "build_passive_crosscheck_certificate",
)
