"""Typed fault plans and orchestrator-owned evidence for adaptive experiments.

Scenario runners remain responsible for deciding *when* a fault is safe to
execute.  The adaptation manager receives only its two existing one-shot loss
controls; diagnostic ground truth remains in the orchestrator-owned plan and
journal.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import time
from types import TracebackType
from typing import IO, TypeAlias, TypeVar, cast


SCHEMA_VERSION = 1
FAULT_JOURNAL_SOURCE_ID = "fault-orchestrator"


def _require_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def _validate_fault_id(fault_id: object) -> str:
    if not isinstance(fault_id, str) or not fault_id.strip():
        raise ValueError("fault id must be a non-empty string")
    return fault_id


def _validate_non_empty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class ReplicaGroupSigkill:
    """Crash one registered replica's isolated process group."""

    fault_id: str
    replica_id: int

    def __post_init__(self) -> None:
        _validate_fault_id(self.fault_id)
        _require_integer(self.replica_id, "replica id")


@dataclass(frozen=True, slots=True)
class SuccessorBundleAttemptDrop:
    """Drop one existing successor-bundle delivery attempt."""

    fault_id: str
    replica_id: int
    attempt: int

    def __post_init__(self) -> None:
        _validate_fault_id(self.fault_id)
        _require_integer(self.replica_id, "replica id")
        _require_integer(self.attempt, "attempt")


@dataclass(frozen=True, slots=True)
class ActivationAckDrop:
    """Drop the existing quorum-completing activation acknowledgement."""

    fault_id: str
    accepted_activation_ordinal: int

    def __post_init__(self) -> None:
        _validate_fault_id(self.fault_id)
        _require_integer(
            self.accepted_activation_ordinal,
            "accepted activation ordinal",
        )


@dataclass(frozen=True, slots=True)
class StaticAuthenticatedFalseReport:
    """Falsify one local reporter-target observation in a frozen window."""

    fault_id: str
    reporter_id: int
    target_id: int
    reported_outcome: str
    diagnostic_window: str

    def __post_init__(self) -> None:
        _validate_fault_id(self.fault_id)
        _require_integer(self.reporter_id, "reporter id")
        _require_integer(self.target_id, "target id")
        if self.reporter_id == self.target_id:
            raise ValueError("false reporter and target must be distinct")
        if self.reported_outcome != "timeout":
            raise ValueError(
                "the bounded false-report mode supports only timeout"
            )
        _validate_non_empty_string(
            self.diagnostic_window,
            "diagnostic window",
        )


@dataclass(frozen=True, slots=True)
class StaticPersistentOmission:
    """Omit a replica's expected contribution in a frozen window."""

    fault_id: str
    replica_id: int
    diagnostic_window: str

    def __post_init__(self) -> None:
        _validate_fault_id(self.fault_id)
        _require_integer(self.replica_id, "replica id")
        _validate_non_empty_string(
            self.diagnostic_window,
            "diagnostic window",
        )


FaultAction: TypeAlias = (
    ReplicaGroupSigkill
    | SuccessorBundleAttemptDrop
    | ActivationAckDrop
    | StaticAuthenticatedFalseReport
    | StaticPersistentOmission
)
_ACTION_TYPES = (
    ReplicaGroupSigkill,
    SuccessorBundleAttemptDrop,
    ActivationAckDrop,
    StaticAuthenticatedFalseReport,
    StaticPersistentOmission,
)
_ActionT = TypeVar("_ActionT", bound=FaultAction)


@dataclass(frozen=True, slots=True)
class ScenarioContext:
    """The immutable limits against which a fault plan is preflighted."""

    replica_ids: tuple[int, ...]
    quorum: int
    crash_budget: int
    successor_bundle_retry_limit: int
    diagnostic_fault_bound: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.replica_ids, tuple) or not self.replica_ids:
            raise ValueError("replica ids must be a non-empty tuple")
        if any(
            isinstance(replica_id, bool) or not isinstance(replica_id, int)
            for replica_id in self.replica_ids
        ):
            raise ValueError("replica ids must contain only integers")
        if any(replica_id < 0 for replica_id in self.replica_ids):
            raise ValueError("replica ids must be non-negative")
        if len(set(self.replica_ids)) != len(self.replica_ids):
            raise ValueError("replica ids must be unique")

        quorum = _require_integer(self.quorum, "quorum")
        if quorum < 1 or quorum > len(self.replica_ids):
            raise ValueError("quorum must be within the replica membership")

        crash_budget = _require_integer(self.crash_budget, "crash budget")
        maximum_crash_budget = len(self.replica_ids) - quorum
        if crash_budget < 0 or crash_budget > maximum_crash_budget:
            raise ValueError(
                "crash budget must preserve the configured quorum"
            )

        retry_limit = _require_integer(
            self.successor_bundle_retry_limit,
            "successor bundle retry limit",
        )
        if retry_limit < 1:
            raise ValueError(
                "successor bundle retry limit must be at least one"
            )

        diagnostic_fault_bound = _require_integer(
            self.diagnostic_fault_bound,
            "diagnostic fault bound",
        )
        if (
            diagnostic_fault_bound < 0
            or diagnostic_fault_bound > crash_budget
        ):
            raise ValueError(
                "diagnostic fault bound must be within the consensus "
                "fault budget"
            )


@dataclass(frozen=True, slots=True)
class FaultPlan:
    """A canonical, preflighted v1 plan for approved experiment effects."""

    context: ScenarioContext
    seed: int
    actions: tuple[FaultAction, ...]

    def __post_init__(self) -> None:
        if type(self.context) is not ScenarioContext:
            raise TypeError("context must be an exact ScenarioContext")
        _require_integer(self.seed, "seed")
        if not isinstance(self.actions, tuple):
            raise TypeError("actions must be a tuple")

        fault_ids: set[str] = set()
        crash_replicas: set[int] = set()
        bundle_controls: set[tuple[int, int]] = set()
        activation_ack_count = 0
        false_reporters: set[int] = set()
        persistent_omitters: set[int] = set()
        diagnostic_windows: set[str] = set()

        for action in self.actions:
            if type(action) not in _ACTION_TYPES:
                raise TypeError(
                    "fault plan contains an unsupported action type"
                )

            fault_id = _validate_fault_id(action.fault_id)
            if fault_id in fault_ids:
                raise ValueError(f"duplicate fault id: {fault_id}")
            fault_ids.add(fault_id)

            if type(action) is ReplicaGroupSigkill:
                crash = cast(ReplicaGroupSigkill, action)
                self._validate_replica_membership(crash.replica_id)
                if crash.replica_id in crash_replicas:
                    raise ValueError(
                        "replica group SIGKILL may target each replica "
                        "at most once"
                    )
                crash_replicas.add(crash.replica_id)
            elif type(action) is SuccessorBundleAttemptDrop:
                bundle = cast(SuccessorBundleAttemptDrop, action)
                self._validate_replica_membership(bundle.replica_id)
                if not (
                    1
                    <= bundle.attempt
                    <= self.context.successor_bundle_retry_limit
                ):
                    raise ValueError(
                        "successor bundle attempt is outside the "
                        "scenario retry limit"
                    )
                control = (bundle.replica_id, bundle.attempt)
                if control in bundle_controls:
                    raise ValueError(
                        "successor bundle attempt may be dropped at most once"
                    )
                bundle_controls.add(control)
            elif type(action) is ActivationAckDrop:
                acknowledgement = cast(ActivationAckDrop, action)
                if (
                    acknowledgement.accepted_activation_ordinal
                    != self.context.quorum
                ):
                    raise ValueError(
                        "activation ACK drop must target the "
                        "quorum-completing ordinal"
                    )
                activation_ack_count += 1
            elif type(action) is StaticAuthenticatedFalseReport:
                false_report = cast(
                    StaticAuthenticatedFalseReport,
                    action,
                )
                self._validate_replica_membership(
                    false_report.reporter_id
                )
                self._validate_replica_membership(
                    false_report.target_id
                )
                false_reporters.add(false_report.reporter_id)
                diagnostic_windows.add(
                    false_report.diagnostic_window
                )
            else:
                omission = cast(StaticPersistentOmission, action)
                self._validate_replica_membership(omission.replica_id)
                persistent_omitters.add(omission.replica_id)
                diagnostic_windows.add(omission.diagnostic_window)

        if len(crash_replicas) > self.context.crash_budget:
            raise ValueError("fault plan exceeds the scenario crash budget")
        if len(bundle_controls) > 1:
            raise ValueError(
                "the manager supports one successor bundle drop control"
            )
        if activation_ack_count > 1:
            raise ValueError(
                "the manager supports one activation ACK drop control"
            )
        if false_reporters & persistent_omitters:
            raise ValueError(
                "false-reporter and persistent-omission sets must "
                "be disjoint"
            )
        diagnostic_identities = false_reporters | persistent_omitters
        if (
            len(diagnostic_identities)
            > self.context.diagnostic_fault_bound
        ):
            raise ValueError(
                "fault plan exceeds the diagnostic fault bound"
            )
        if len(diagnostic_windows) > 1:
            raise ValueError(
                "diagnostic fault modes must share one frozen window"
            )

    def _validate_replica_membership(self, replica_id: int) -> None:
        if replica_id not in self.context.replica_ids:
            raise ValueError(
                f"replica {replica_id} is outside scenario membership"
            )

    def actions_of_type(
        self,
        action_type: type[_ActionT],
    ) -> tuple[_ActionT, ...]:
        """Return matching actions without allowing subclass dispatch."""
        if action_type not in _ACTION_TYPES:
            raise TypeError("unsupported fault action type")
        return tuple(
            cast(_ActionT, action)
            for action in self.actions
            if type(action) is action_type
        )

    def manager_cli_args(self) -> tuple[str, ...]:
        """Translate only manager-visible loss controls in fixed CLI order."""
        arguments: list[str] = []
        bundle_actions = self.actions_of_type(SuccessorBundleAttemptDrop)
        acknowledgement_actions = self.actions_of_type(ActivationAckDrop)

        if bundle_actions:
            bundle = bundle_actions[0]
            arguments.extend(
                (
                    "--experiment-drop-bundle-attempt",
                    f"{bundle.replica_id}:{bundle.attempt}",
                )
            )
        if acknowledgement_actions:
            acknowledgement = acknowledgement_actions[0]
            arguments.extend(
                (
                    "--experiment-drop-activation-ack",
                    str(acknowledgement.accepted_activation_ordinal),
                )
            )
        return tuple(arguments)

    def replica_cli_args(self, replica_id: int) -> tuple[str, ...]:
        """Translate diagnostic controls only for their faulty replica.

        Exact runtime configuration and application bounds belong to the
        frozen scenario profile and are appended by its runner.  This method
        intentionally has no manager equivalent.
        """
        _require_integer(replica_id, "replica id")
        self._validate_replica_membership(replica_id)
        arguments: list[str] = []
        for action in self.actions:
            if (
                type(action) is StaticAuthenticatedFalseReport
                and cast(
                    StaticAuthenticatedFalseReport,
                    action,
                ).reporter_id
                == replica_id
            ):
                false_report = cast(
                    StaticAuthenticatedFalseReport,
                    action,
                )
                arguments.extend(
                    (
                        "--experiment-byzantine-window",
                        false_report.diagnostic_window,
                        "--experiment-false-report-target",
                        str(false_report.target_id),
                    )
                )
            elif (
                type(action) is StaticPersistentOmission
                and cast(
                    StaticPersistentOmission,
                    action,
                ).replica_id
                == replica_id
            ):
                omission = cast(StaticPersistentOmission, action)
                arguments.extend(
                    (
                        "--experiment-byzantine-window",
                        omission.diagnostic_window,
                        "--experiment-omit-outbound-aggregate",
                    )
                )
        return tuple(arguments)

    def canonical_json(self) -> str:
        """Return the compact, key-sorted canonical schema-v1 document."""
        scenario: dict[str, object] = {
            "crash_budget": self.context.crash_budget,
            "quorum": self.context.quorum,
            "replica_ids": list(self.context.replica_ids),
            "successor_bundle_retry_limit": (
                self.context.successor_bundle_retry_limit
            ),
        }
        if self.context.diagnostic_fault_bound:
            scenario["diagnostic_fault_bound"] = (
                self.context.diagnostic_fault_bound
            )
        value = {
            "actions": [
                self._canonical_action(action) for action in self.actions
            ],
            "scenario": scenario,
            "schema_version": SCHEMA_VERSION,
            "seed": self.seed,
        }
        return json.dumps(
            value,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @property
    def sha256(self) -> str:
        """Return the stable digest of the canonical plan bytes."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @staticmethod
    def _canonical_action(action: FaultAction) -> dict[str, object]:
        if type(action) is ReplicaGroupSigkill:
            crash = cast(ReplicaGroupSigkill, action)
            return {
                "fault_id": crash.fault_id,
                "kind": "replica_group_sigkill",
                "replica_id": crash.replica_id,
            }
        if type(action) is SuccessorBundleAttemptDrop:
            bundle = cast(SuccessorBundleAttemptDrop, action)
            return {
                "attempt": bundle.attempt,
                "fault_id": bundle.fault_id,
                "kind": "successor_bundle_attempt_drop",
                "replica_id": bundle.replica_id,
            }
        if type(action) is ActivationAckDrop:
            acknowledgement = cast(ActivationAckDrop, action)
            return {
                "accepted_activation_ordinal": (
                    acknowledgement.accepted_activation_ordinal
                ),
                "fault_id": acknowledgement.fault_id,
                "kind": "activation_ack_drop",
            }
        if type(action) is StaticAuthenticatedFalseReport:
            false_report = cast(
                StaticAuthenticatedFalseReport,
                action,
            )
            return {
                "diagnostic_window": false_report.diagnostic_window,
                "fault_id": false_report.fault_id,
                "kind": "static_authenticated_false_report",
                "reported_outcome": false_report.reported_outcome,
                "reporter_id": false_report.reporter_id,
                "target_id": false_report.target_id,
            }
        if type(action) is StaticPersistentOmission:
            omission = cast(StaticPersistentOmission, action)
            return {
                "diagnostic_window": omission.diagnostic_window,
                "fault_id": omission.fault_id,
                "kind": "static_persistent_omission",
                "replica_id": omission.replica_id,
            }
        raise TypeError("unsupported fault action type")


class FaultJournal:
    """A flushed, append-only JSONL record owned by the orchestrator."""

    def __init__(
        self,
        path: Path,
        plan_sha256: str,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self._path = Path(path)
        self._plan_sha256 = self._validate_plan_sha256(plan_sha256)
        if not callable(monotonic_ns):
            raise TypeError("monotonic_ns must be callable")
        self._monotonic_ns = monotonic_ns
        self._stream: IO[str] | None = None
        self._source_sequence = 0
        self._last_monotonic_ns: int | None = None

    @staticmethod
    def _validate_plan_sha256(value: object) -> str:
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("plan sha256 must be a lowercase SHA-256 digest")
        return value

    def __enter__(self) -> FaultJournal:
        if self._stream is not None:
            raise RuntimeError("fault journal is already open")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self._path.open(
            "x",
            encoding="utf-8",
            newline="\n",
        )
        return self

    def __exit__(
        self,
        _exc_type: object,
        _exc_value: object,
        _traceback: object,
    ) -> None:
        if self._stream is not None:
            self._stream.flush()
            self._stream.close()
            self._stream = None

    def append(
        self,
        *,
        fault_id: str,
        lifecycle: str,
        outcome: Mapping[str, object] | None = None,
    ) -> None:
        """Append and flush one self-contained lifecycle event."""
        stream = self._stream
        if stream is None:
            raise RuntimeError("fault journal is not open")
        _validate_fault_id(fault_id)
        if not isinstance(lifecycle, str) or not lifecycle.strip():
            raise ValueError("fault lifecycle must be a non-empty string")
        if outcome is not None and not isinstance(outcome, Mapping):
            raise TypeError("fault outcome must be a mapping")

        timestamp = _require_integer(
            self._monotonic_ns(),
            "source monotonic timestamp",
        )
        if timestamp < 0:
            raise ValueError("source monotonic timestamp must be non-negative")
        if (
            self._last_monotonic_ns is not None
            and timestamp < self._last_monotonic_ns
        ):
            raise ValueError("source monotonic timestamp regressed")

        event: dict[str, object] = {
            "fault_id": fault_id,
            "lifecycle": lifecycle,
            "plan_sha256": self._plan_sha256,
            "schema_version": SCHEMA_VERSION,
            "source_id": FAULT_JOURNAL_SOURCE_ID,
            "source_monotonic_ns": timestamp,
            "source_sequence": self._source_sequence,
        }
        if outcome is not None:
            event["outcome"] = dict(outcome)

        encoded = json.dumps(
            event,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        stream.write(encoded)
        stream.write("\n")
        stream.flush()

        self._last_monotonic_ns = timestamp
        self._source_sequence += 1


class FaultLifecycle:
    """Write exactly one terminal journal event for every planned action."""

    def __init__(self, plan: FaultPlan, journal: FaultJournal) -> None:
        if type(plan) is not FaultPlan:
            raise TypeError("plan must be an exact FaultPlan")
        if not hasattr(journal, "append") or not callable(journal.append):
            raise TypeError("journal must provide an append method")

        self._journal = journal
        self._fault_ids = tuple(action.fault_id for action in plan.actions)
        self._known_fault_ids = frozenset(self._fault_ids)
        self._started_fault_ids: set[str] = set()
        self._terminal_fault_ids: set[str] = set()

    def start(self, fault_id: str) -> None:
        """Record that one exact planned action has started."""

        self._require_known(fault_id)
        if fault_id in self._terminal_fault_ids:
            raise RuntimeError(f"fault {fault_id} is already terminal")
        if fault_id in self._started_fault_ids:
            raise RuntimeError(f"fault {fault_id} was already started")

        self._journal.append(
            fault_id=fault_id,
            lifecycle="started",
        )
        self._started_fault_ids.add(fault_id)

    def terminal(
        self,
        fault_id: str,
        status: str,
        outcome: Mapping[str, object] | None = None,
    ) -> None:
        """Record one terminal status, with optional non-status details."""

        self._require_known(fault_id)
        if fault_id in self._terminal_fault_ids:
            raise RuntimeError(f"fault {fault_id} is already terminal")
        if not isinstance(status, str) or not status.strip():
            raise ValueError("fault terminal status must be a non-empty string")
        if outcome is not None and not isinstance(outcome, Mapping):
            raise TypeError("fault terminal outcome must be a mapping")

        terminal_outcome = dict(outcome or {})
        supplied_status = terminal_outcome.get("status")
        if supplied_status is not None and supplied_status != status:
            raise ValueError(
                "fault terminal outcome status conflicts with terminal status"
            )
        terminal_outcome["status"] = status

        self._journal.append(
            fault_id=fault_id,
            lifecycle="terminal",
            outcome=terminal_outcome,
        )
        self._terminal_fault_ids.add(fault_id)

    def finalize(
        self,
        unstarted_status: str = "not_reached",
        started_status: str = "failed",
    ) -> None:
        """Terminalize every unfinished action in plan order, idempotently."""

        self._validate_status(unstarted_status, "unstarted_status")
        self._validate_status(started_status, "started_status")
        for fault_id in self._fault_ids:
            if fault_id in self._terminal_fault_ids:
                continue
            status = (
                started_status
                if fault_id in self._started_fault_ids
                else unstarted_status
            )
            self.terminal(fault_id, status)

    def _require_known(self, fault_id: object) -> str:
        validated = _validate_fault_id(fault_id)
        if validated not in self._known_fault_ids:
            raise KeyError(f"fault {validated} is not present in the plan")
        return validated

    @staticmethod
    def _validate_status(status: object, field: str) -> str:
        if not isinstance(status, str) or not status.strip():
            raise ValueError(f"{field} must be a non-empty string")
        return status


class FaultEvidence:
    """Bind one immutable plan to its matching lifecycle journal."""

    def __init__(
        self,
        run_directory: Path,
        plan: FaultPlan,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if type(plan) is not FaultPlan:
            raise TypeError("plan must be an exact FaultPlan")
        if not callable(monotonic_ns):
            raise TypeError("monotonic_ns must be callable")

        self._run_directory = Path(run_directory)
        self._plan = plan
        self._monotonic_ns = monotonic_ns
        self._plan_path = self._run_directory / "fault-plan.json"
        self._journal_path = (
            self._run_directory / "raw" / "fault-orchestrator.jsonl"
        )
        self._journal: FaultJournal | None = None
        self._lifecycle: FaultLifecycle | None = None
        self._used = False

    def __enter__(self) -> FaultLifecycle:
        if self._used:
            raise RuntimeError("fault evidence context may only be entered once")
        self._used = True

        self._persist_plan()
        journal = FaultJournal(
            self._journal_path,
            self._plan.sha256,
            monotonic_ns=self._monotonic_ns,
        )
        try:
            journal.__enter__()
        except BaseException as exc:
            try:
                self._plan_path.unlink()
            except OSError as rollback_error:
                exc.add_note(
                    "could not roll back unmatched fault plan: "
                    f"{rollback_error}"
                )
            raise

        self._journal = journal
        self._lifecycle = FaultLifecycle(self._plan, journal)
        return self._lifecycle

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        lifecycle = self._lifecycle
        journal = self._journal
        if lifecycle is None or journal is None:
            return
        try:
            lifecycle.finalize()
        finally:
            journal.__exit__(exc_type, exc_value, traceback)
            self._lifecycle = None
            self._journal = None

    def _persist_plan(self) -> None:
        payload = self._plan.canonical_json().encode("utf-8")
        descriptor = os.open(
            self._plan_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            self._plan_path.unlink(missing_ok=True)
            raise
