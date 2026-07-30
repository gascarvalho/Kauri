#!/usr/bin/env python3
"""Run one bounded N=7 crash/Byzantine comparison arm.

This is deliberately a development evaluator, not a campaign framework.  Each
invocation runs exactly one frozen arm, preserves canonical FI-Core evidence,
and emits ``arm-verdict.json``.  PASS means only that the configured action was
observed, the fixed N=7/Q=5 context remained visible, a common commit existed
before and after the action, and no conflicting committed hashes were observed.
It makes no diagnosis, rematching, performance, or statistical claim.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import time
from types import ModuleType
from typing import Any, Mapping, Sequence
import uuid


KAURI_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(KAURI_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(KAURI_REPOSITORY_ROOT))

from experiments.adaptive.kauri_experiment import (  # noqa: E402
    FaultEvidence,
    ProcessRegistry,
    ReplicaGroupSigkill,
)
from experiments.adaptive.kauri_experiment.comparison import (  # noqa: E402
    FaultComparisonArm,
    build_n7_comparison,
)


ARM_NAMES = (
    "sigkill_crash",
    "static_authenticated_false_report",
    "static_persistent_omission",
)
REPLICA_IDS = tuple(range(7))
FAULTY_REPLICA_ID = 1
FALSE_REPORT_TARGET_ID = 4
FAULT_THRESHOLD = 2
QUORUM = 5
TREE_ID = 6
TREE_MEMBERS_BREADTH_FIRST = (6, 0, 1, 2, 3, 4, 5)
SNAPSHOT_SEED = 0xA2F7
DIAGNOSTIC_WINDOW = "n7-epoch0-tree6-static-v1"
DEFAULT_CONTEXT_LIMIT = 8
AUTHORITATIVE_SOURCE_ID = "replica-2"
MANAGER_SOURCE_ID = "adaptive-manager"

# This is the exact adaptive-v2 epoch-zero digest produced by the frozen N=7
# breadth-first rotations in n7-crash-recovery/profile.json.  A live run checks
# it against all seven configuration-active records before accepting a fault.
EPOCH0_DIGEST = (
    "f550407e56cc54a8fd4e93d1997ebe658"
    "b75699f4f2a9e955f4cc829b52bec81"
)
EXACT_CONFIGURATION = f"0:{TREE_ID}:{EPOCH0_DIGEST}"

FALSE_REPORT_MARKER = "KAURI_FAULT false_timeout_emitted"
OMISSION_MARKER = "KAURI_FAULT aggregate_omitted"
LIMITATIONS = (
    "single development run; no statistical inference",
    "no false-reporter-versus-omitter diagnosis",
    "no topology rematching or structural-exposure conclusion",
    "no throughput-improvement conclusion",
    "absence of an observed conflicting commit is not a safety proof",
)


class ComparisonRunError(RuntimeError):
    """A preflight, orchestration, or evidence failure."""


def load_n7_runner(repository: Path = KAURI_REPOSITORY_ROOT) -> ModuleType:
    """Dynamically load the established N=7 runner from its hyphenated path."""

    path = (
        repository
        / "experiments"
        / "adaptive"
        / "n7-crash-recovery"
        / "run.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_kauri_n7_crash_runner",
        path,
    )
    if spec is None or spec.loader is None:
        raise ComparisonRunError(f"cannot load N=7 runner: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def build_arm(arm_name: str, kauri_revision: str) -> FaultComparisonArm:
    """Return one exact arm from the frozen three-arm comparison."""

    if arm_name not in ARM_NAMES:
        raise ComparisonRunError(f"unknown comparison arm: {arm_name}")
    comparison = build_n7_comparison(
        kauri_revision=kauri_revision,
        seed=SNAPSHOT_SEED,
        faulty_replica_id=FAULTY_REPLICA_ID,
        false_report_target_id=FALSE_REPORT_TARGET_ID,
        diagnostic_window=DIAGNOSTIC_WINDOW,
    )
    return next(arm for arm in comparison.arms if arm.name == arm_name)


def replica_launch_overlays(
    arm: FaultComparisonArm,
    *,
    context_limit: int = DEFAULT_CONTEXT_LIMIT,
) -> tuple[tuple[str, ...], ...]:
    """Return replica-only CLI overlays for one arm.

    The FI-Core plan supplies the mode and frozen window.  The runner supplies
    the exact active configuration and a finite proposal-context bound.
    """

    if isinstance(context_limit, bool) or not isinstance(context_limit, int):
        raise ComparisonRunError("Byzantine context limit must be an integer")
    if context_limit <= 0 or context_limit > 1024:
        raise ComparisonRunError(
            "Byzantine context limit must be within 1..1024"
        )
    if arm.plan.manager_cli_args():
        raise ComparisonRunError(
            "comparison ground truth must not enter manager arguments"
        )

    result: list[tuple[str, ...]] = []
    for replica_id in REPLICA_IDS:
        plan_arguments = arm.plan.replica_cli_args(replica_id)
        if plan_arguments:
            result.append(
                (
                    *plan_arguments,
                    "--experiment-byzantine-configuration",
                    EXACT_CONFIGURATION,
                    "--experiment-byzantine-context-limit",
                    str(context_limit),
                )
            )
        else:
            result.append(())
    return tuple(result)


def augment_replica_commands(
    commands: Sequence[Sequence[str]],
    arm: FaultComparisonArm,
    *,
    context_limit: int,
) -> tuple[tuple[str, ...], ...]:
    """Append exact experiment controls to the generated replica commands."""

    if len(commands) != len(REPLICA_IDS):
        raise ComparisonRunError("launch requires exactly seven replicas")
    overlays = replica_launch_overlays(
        arm,
        context_limit=context_limit,
    )
    return tuple(
        (*tuple(command), *overlays[replica_id])
        for replica_id, command in enumerate(commands)
    )


def launch_bundle(
    arm: FaultComparisonArm,
    *,
    kauri_revision: str,
    profile_path: Path,
    profile_sha256: str,
    context_limit: int,
) -> dict[str, object]:
    """Return the immutable arm/overlay contract used by live and dry runs."""

    overlays = replica_launch_overlays(
        arm,
        context_limit=context_limit,
    )
    return {
        "schema_version": 1,
        "scenario": "n7-static-fault-comparison",
        "arm": arm.name,
        "kauri_revision": kauri_revision,
        "fault_plan_sha256": arm.plan.sha256,
        "profile": {
            "path": str(profile_path),
            "sha256": profile_sha256,
        },
        "fixed_context": {
            "replica_ids": list(REPLICA_IDS),
            "fault_threshold": FAULT_THRESHOLD,
            "quorum": QUORUM,
            "seed": SNAPSHOT_SEED,
            "faulty_replica_id": FAULTY_REPLICA_ID,
            "false_report_target_id": FALSE_REPORT_TARGET_ID,
            "tree_id": TREE_ID,
            "tree_members_breadth_first": list(
                TREE_MEMBERS_BREADTH_FIRST
            ),
            "epoch0_digest": EPOCH0_DIGEST,
            "diagnostic_window": DIAGNOSTIC_WINDOW,
            "byzantine_context_limit": context_limit,
        },
        "replica_overlays": [
            {
                "replica_id": replica_id,
                "argv": list(arguments),
            }
            for replica_id, arguments in enumerate(overlays)
        ],
        "manager_overlay": [],
        "claims_not_made": list(LIMITATIONS),
    }


def persist_augmented_launch_arguments(
    runner: ModuleType,
    run_directory: Path,
    replica_commands: Sequence[Sequence[str]],
    arm: FaultComparisonArm,
    *,
    context_limit: int,
) -> None:
    """Bind persisted launch metadata to the commands actually executed."""

    path = run_directory / "runtime" / "launch-arguments.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ComparisonRunError(
            f"cannot read generated launch arguments: {exc}"
        ) from exc
    processes = document.get("processes")
    if not isinstance(processes, list) or len(processes) != 8:
        raise ComparisonRunError("generated launch arguments have schema drift")

    overlays = replica_launch_overlays(
        arm,
        context_limit=context_limit,
    )
    by_source = {
        process.get("source_id"): process
        for process in processes
        if isinstance(process, dict)
    }
    for replica_id in REPLICA_IDS:
        source_id = f"replica-{replica_id}"
        process = by_source.get(source_id)
        if not isinstance(process, dict):
            raise ComparisonRunError(
                f"generated launch arguments omit {source_id}"
            )
        process["argv"] = list(replica_commands[replica_id])
        effective = process.get("effective_options")
        if not isinstance(effective, dict):
            raise ComparisonRunError(
                f"generated launch options omit {source_id}"
            )
        if overlays[replica_id]:
            effective["experiment_byzantine_configuration"] = (
                EXACT_CONFIGURATION
            )
            effective["experiment_byzantine_window"] = DIAGNOSTIC_WINDOW
            effective["experiment_byzantine_context_limit"] = context_limit
            effective["experiment_fault_arm"] = arm.name

    manager = by_source.get(MANAGER_SOURCE_ID)
    if not isinstance(manager, dict):
        raise ComparisonRunError(
            "generated launch arguments omit adaptation manager"
        )
    manager_argv = manager.get("argv")
    if not isinstance(manager_argv, list):
        raise ComparisonRunError("manager launch argv is invalid")
    if any(
        str(argument).startswith("--experiment-byzantine")
        or str(argument).startswith("--experiment-false-report")
        or str(argument).startswith("--experiment-omit-outbound")
        for argument in manager_argv
    ):
        raise ComparisonRunError(
            "diagnostic ground truth leaked into manager argv"
        )
    runner._replace_json(path, document)


def _event_timestamp(runner: ModuleType, event: Mapping[str, Any]) -> int:
    try:
        return int(runner._event_timestamp(event))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ComparisonRunError("structured event has an invalid timestamp") from exc


def _commit_key(
    runner: ModuleType,
    event: Mapping[str, Any],
) -> tuple[int, str]:
    try:
        return runner._commit_key(event)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ComparisonRunError("commit evidence has invalid identity") from exc


def common_commit_after(
    runner: ModuleType,
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    participants: Sequence[int],
    after_ns: int,
) -> dict[str, object] | None:
    """Return the first observer commit witnessed by every participant."""

    if isinstance(after_ns, bool) or not isinstance(after_ns, int):
        raise ComparisonRunError("common-commit boundary must be an integer")
    if after_ns < 0:
        raise ComparisonRunError(
            "common-commit boundary must be non-negative"
        )
    participant_tuple = tuple(participants)
    witnesses = runner.commit_witness_timestamps(
        streams,
        participant_tuple,
    )
    found = runner.find_first_common_epoch_commit(
        streams.get(AUTHORITATIVE_SOURCE_ID, ()),
        witnesses,
        participants=participant_tuple,
        epoch_number=0,
        strictly_after_ns=after_ns if after_ns else None,
    )
    if found is None:
        return None
    key = _commit_key(runner, found.observer_event)
    return {
        "block_height": key[0],
        "block_hash": key[1],
        "common_monotonic_raw_ns": int(found.common_ns),
        "participants": list(participant_tuple),
    }


def conflicting_commits(
    runner: ModuleType,
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[dict[str, object]]:
    """Return every observed committed height with more than one hash."""

    hashes_by_height: dict[int, set[str]] = {}
    sources_by_identity: dict[tuple[int, str], set[str]] = {}
    for source_id, events in streams.items():
        if not source_id.startswith("replica-"):
            continue
        for event in events:
            if event.get("event_type") != "block.committed":
                continue
            height, block_hash = _commit_key(runner, event)
            hashes_by_height.setdefault(height, set()).add(block_hash)
            sources_by_identity.setdefault((height, block_hash), set()).add(
                source_id
            )

    conflicts: list[dict[str, object]] = []
    for height, hashes in sorted(hashes_by_height.items()):
        if len(hashes) <= 1:
            continue
        conflicts.append(
            {
                "block_height": height,
                "hashes": [
                    {
                        "block_hash": block_hash,
                        "sources": sorted(
                            sources_by_identity[(height, block_hash)]
                        ),
                    }
                    for block_hash in sorted(hashes)
                ],
            }
        )
    return conflicts


def observed_fixed_quorum(
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, object]:
    """Require every observed active-configuration record to retain Q=5."""

    observed = 0
    invalid: list[dict[str, object]] = []
    for source_id, events in streams.items():
        if not source_id.startswith("replica-"):
            continue
        for event in events:
            if event.get("event_type") != "adaptive.configuration_active":
                continue
            observed += 1
            payload = event.get("payload")
            quorum = (
                payload.get("global_quorum")
                if isinstance(payload, dict)
                else None
            )
            if quorum != QUORUM:
                invalid.append(
                    {
                        "source_id": source_id,
                        "global_quorum": quorum,
                    }
                )
    if observed == 0:
        raise ComparisonRunError(
            "no active-configuration record proves the fixed quorum"
        )
    return {
        "configured_replica_count": len(REPLICA_IDS),
        "configured_fault_threshold": FAULT_THRESHOLD,
        "configured_quorum": QUORUM,
        "active_configuration_records": observed,
        "invalid_records": invalid,
    }


def expected_marker(arm_name: str) -> str | None:
    if arm_name == "static_authenticated_false_report":
        return FALSE_REPORT_MARKER
    if arm_name == "static_persistent_omission":
        return OMISSION_MARKER
    if arm_name == "sigkill_crash":
        return None
    raise ComparisonRunError(f"unknown comparison arm: {arm_name}")


def find_fault_marker(
    run_directory: Path,
    arm_name: str,
    *,
    start_offset: int = 0,
    end_offset: int | None = None,
) -> dict[str, object] | None:
    """Find the exact log marker proving one Byzantine action occurred."""

    marker = expected_marker(arm_name)
    if marker is None:
        return None
    if (
        isinstance(start_offset, bool)
        or not isinstance(start_offset, int)
        or start_offset < 0
    ):
        raise ComparisonRunError(
            "fault log start offset must be a non-negative integer"
        )
    if (
        end_offset is not None
        and (
            isinstance(end_offset, bool)
            or not isinstance(end_offset, int)
            or end_offset < start_offset
        )
    ):
        raise ComparisonRunError(
            "fault log end offset must follow the start offset"
        )
    path = run_directory / "logs" / f"replica-{FAULTY_REPLICA_ID}.log"
    try:
        with path.open("rb") as stream:
            stream.seek(start_offset)
            maximum = (
                None
                if end_offset is None
                else end_offset - start_offset
            )
            payload = stream.read(maximum)
        lines = payload.decode("utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ComparisonRunError(f"cannot read Byzantine fault log: {exc}") from exc

    exact_tokens = (
        f"reporter={FAULTY_REPLICA_ID}",
        f"target={FALSE_REPORT_TARGET_ID}",
        "epoch=0",
        f"tree={TREE_ID}",
        f"window={DIAGNOSTIC_WINDOW}",
    )
    if arm_name == "static_persistent_omission":
        exact_tokens = (
            f"replica={FAULTY_REPLICA_ID}",
            "parent=6",
            "epoch=0",
            f"tree={TREE_ID}",
            f"window={DIAGNOSTIC_WINDOW}",
        )
    matches = [
        line
        for line in lines
        if marker in line and all(token in line for token in exact_tokens)
    ]
    if not matches:
        return None
    return {
        "kind": marker.removeprefix("KAURI_FAULT "),
        "source_id": f"replica-{FAULTY_REPLICA_ID}",
        "log_path": str(path.relative_to(run_directory)),
        "matching_line_count": len(matches),
        "line": matches[0][-1024:],
    }


def fault_log_cursor(run_directory: Path) -> int:
    """Return the current faulty-replica log size for ordered observation."""

    path = run_directory / "logs" / f"replica-{FAULTY_REPLICA_ID}.log"
    try:
        return path.stat().st_size
    except OSError as exc:
        raise ComparisonRunError(
            f"cannot snapshot Byzantine fault log: {exc}"
        ) from exc


def validate_fault_evidence(
    run_directory: Path,
    arm: FaultComparisonArm,
    *,
    expected_status: str,
) -> dict[str, object]:
    """Validate the canonical plan/journal binding for one completed action."""

    plan_path = run_directory / "fault-plan.json"
    journal_path = run_directory / "raw" / "fault-orchestrator.jsonl"
    try:
        plan_payload = plan_path.read_text(encoding="utf-8")
        journal_lines = journal_path.read_text(
            encoding="utf-8"
        ).splitlines()
        events = [json.loads(line) for line in journal_lines if line]
    except (OSError, json.JSONDecodeError) as exc:
        raise ComparisonRunError(
            f"cannot validate canonical fault evidence: {exc}"
        ) from exc
    if plan_payload != arm.plan.canonical_json():
        raise ComparisonRunError(
            "persisted fault plan differs from canonical arm plan"
        )
    if not events or any(not isinstance(event, dict) for event in events):
        raise ComparisonRunError("fault journal is empty or malformed")
    if any(
        event.get("fault_id") != arm.plan.actions[0].fault_id
        or event.get("plan_sha256") != arm.plan.sha256
        for event in events
    ):
        raise ComparisonRunError(
            "fault journal identity differs from the canonical plan"
        )
    terminal = [
        event for event in events if event.get("lifecycle") == "terminal"
    ]
    if len(terminal) != 1:
        raise ComparisonRunError(
            "fault journal requires exactly one terminal event"
        )
    outcome = terminal[0].get("outcome")
    if (
        not isinstance(outcome, dict)
        or outcome.get("status") != expected_status
    ):
        raise ComparisonRunError(
            f"fault journal terminal status is not {expected_status}"
        )
    return {
        "plan_path": str(plan_path.relative_to(run_directory)),
        "journal_path": str(journal_path.relative_to(run_directory)),
        "plan_sha256": arm.plan.sha256,
        "terminal_status": expected_status,
        "journal_event_count": len(events),
    }


def build_arm_verdict(
    *,
    arm: FaultComparisonArm,
    run_id: str,
    kauri_revision: str,
    action_observation: Mapping[str, object] | None,
    before_commit: Mapping[str, object] | None,
    after_commit: Mapping[str, object] | None,
    fixed_quorum: Mapping[str, object] | None,
    conflicts: Sequence[Mapping[str, object]],
    runtime_error: str | None,
    dry_run: bool = False,
) -> dict[str, object]:
    """Build a conservative immutable verdict for one arm."""

    if dry_run:
        verdict = "DRY_RUN"
    else:
        passed = (
            runtime_error is None
            and action_observation is not None
            and before_commit is not None
            and after_commit is not None
            and fixed_quorum is not None
            and not fixed_quorum.get("invalid_records")
            and not conflicts
        )
        verdict = "PASS" if passed else "INCOMPLETE"
    return {
        "schema_version": 1,
        "scenario": "n7-static-fault-comparison",
        "arm": arm.name,
        "run_id": run_id,
        "kauri_revision": kauri_revision,
        "fault_plan_sha256": arm.plan.sha256,
        "verdict": verdict,
        "action_observation": (
            dict(action_observation)
            if action_observation is not None
            else None
        ),
        "common_commit_before": (
            dict(before_commit) if before_commit is not None else None
        ),
        "common_commit_after": (
            dict(after_commit) if after_commit is not None else None
        ),
        "fixed_context_observation": (
            dict(fixed_quorum) if fixed_quorum is not None else None
        ),
        "conflicting_commits": [dict(conflict) for conflict in conflicts],
        "runtime_error": runtime_error,
        "claims_not_made": list(LIMITATIONS),
    }


def _shutdown_records(
    records: Sequence[Any],
) -> list[dict[str, object]]:
    """Stop only process groups created by this invocation."""

    groups = {
        int(record.pgid)
        for record in records
        if record.process.poll() is None
    }
    if any(group <= 1 for group in groups) or os.getpgrp() in groups:
        raise ComparisonRunError("refusing unsafe comparison cleanup groups")
    for signum, grace_s in (
        (signal.SIGINT, 8.0),
        (signal.SIGTERM, 2.0),
        (signal.SIGKILL, 1.0),
    ):
        active = [
            record for record in records if record.process.poll() is None
        ]
        for record in active:
            try:
                os.killpg(record.pgid, signum)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + grace_s
        while (
            any(record.process.poll() is None for record in records)
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        if not any(record.process.poll() is None for record in records):
            break

    outcomes: list[dict[str, object]] = []
    for record in records:
        try:
            returncode = record.process.wait(timeout=0.2)
        except Exception:
            returncode = record.process.poll()
        record.log_handle.close()
        outcomes.append(
            {
                "source_id": record.name,
                "pid": record.pid,
                "pgid": record.pgid,
                "returncode": returncode,
            }
        )
    return outcomes


def _write_or_replace_json(
    runner: ModuleType,
    path: Path,
    value: Mapping[str, object],
) -> None:
    if path.exists():
        runner._replace_json(path, value)
    else:
        runner._write_json_exclusive(path, value)


def _profile_checks(profile: Mapping[str, Any]) -> None:
    expected = {
        "replica_ids": list(REPLICA_IDS),
        "fault_threshold": FAULT_THRESHOLD,
        "quorum": QUORUM,
        "snapshot_seed": SNAPSHOT_SEED,
        "epoch0_roots": list(REPLICA_IDS),
        "tree_switch_period_blocks": 1,
    }
    for field, value in expected.items():
        if profile.get(field) != value:
            raise ComparisonRunError(
                f"frozen profile {field} differs from comparison: "
                f"{profile.get(field)!r}"
            )


def _dry_run(
    *,
    runner: ModuleType,
    arm: FaultComparisonArm,
    run_directory: Path,
    revision: str,
    profile_path: Path,
    profile_sha256: str,
    context_limit: int,
) -> int:
    bundle = launch_bundle(
        arm,
        kauri_revision=revision,
        profile_path=profile_path,
        profile_sha256=profile_sha256,
        context_limit=context_limit,
    )
    runner._write_json_exclusive(
        run_directory / "launch-bundle.json",
        bundle,
    )
    with FaultEvidence(
        run_directory,
        arm.plan,
        monotonic_ns=runner.monotonic_raw_ns,
    ):
        pass
    verdict = build_arm_verdict(
        arm=arm,
        run_id=run_directory.name,
        kauri_revision=revision,
        action_observation=None,
        before_commit=None,
        after_commit=None,
        fixed_quorum=None,
        conflicts=(),
        runtime_error=None,
        dry_run=True,
    )
    runner._write_json_exclusive(
        run_directory / "arm-verdict.json",
        verdict,
    )
    print(f"DRY_RUN: {run_directory}")
    return 0


def _run_live(
    *,
    args: argparse.Namespace,
    runner: ModuleType,
    arm: FaultComparisonArm,
    run_directory: Path,
    revision: str,
    profile: Mapping[str, Any],
    profile_path: Path,
    profile_bytes: bytes,
    binaries: Mapping[str, Path],
) -> int:
    run_id = run_directory.name
    source_instances = {
        f"replica-{replica_id}": (
            f"{run_id}-replica-{replica_id}-{uuid.uuid4().hex}"
        )
        for replica_id in REPLICA_IDS
    }
    source_instances[MANAGER_SOURCE_ID] = (
        f"{run_id}-manager-{uuid.uuid4().hex}"
    )
    bundle = launch_bundle(
        arm,
        kauri_revision=revision,
        profile_path=profile_path,
        profile_sha256=runner.sha256_bytes(profile_bytes),
        context_limit=args.context_limit,
    )
    runner._write_json_exclusive(
        run_directory / "launch-bundle.json",
        bundle,
    )
    runner._write_private(run_directory / "profile.json", profile_bytes)

    state_path = run_directory / "runner-state.json"
    state: dict[str, object] = {
        "schema_version": 1,
        "scenario": "n7-static-fault-comparison",
        "arm": arm.name,
        "run_id": run_id,
        "revision": revision,
        "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "phase": "identity_generation",
        "runtime_error": None,
    }
    runner._write_json_exclusive(state_path, state)

    records: list[Any] = []
    registry = ProcessRegistry(monotonic_ns=runner.monotonic_raw_ns)
    resources = ExitStack()
    lifecycle: Any | None = None
    before_commit: dict[str, object] | None = None
    after_commit: dict[str, object] | None = None
    action_observation: dict[str, object] | None = None
    fixed_quorum: dict[str, object] | None = None
    conflicts: list[dict[str, object]] = []
    runtime_error: str | None = None
    cleanup: list[dict[str, object]] = []
    expected_crashed: set[int] = set()
    fault_marker_offset: int | None = None
    interrupted = False

    try:
        lifecycle = resources.enter_context(
            FaultEvidence(
                run_directory,
                arm.plan,
                monotonic_ns=runner.monotonic_raw_ns,
            )
        )
        bls, tls, issuer = runner.generate_identities(
            binaries["keygen"],
            binaries["tls_keygen"],
            run_directory / "config",
        )
        (
            _main_config,
            _replica_configs,
            manager_command,
            base_replica_commands,
            _runtime_artifacts,
        ) = runner.write_runtime_inputs(
            run_directory,
            profile,
            bls,
            tls,
            issuer,
            peer_port=args.peer_port,
            client_port=args.client_port,
            manager_port=args.manager_port,
            run_id=run_id,
            source_instances=source_instances,
            app_binary=binaries["app"],
            manager_binary=binaries["manager"],
            fault_plan=arm.plan,
        )
        replica_commands = augment_replica_commands(
            base_replica_commands,
            arm,
            context_limit=args.context_limit,
        )
        persist_augmented_launch_arguments(
            runner,
            run_directory,
            replica_commands,
            arm,
            context_limit=args.context_limit,
        )

        state["phase"] = "launch"
        runner._replace_json(state_path, state)
        manager_record = runner.spawn_process(
            MANAGER_SOURCE_ID,
            manager_command,
            run_directory / "logs" / "adaptive-manager.log",
            run_directory,
            replica_id=None,
        )
        records.append(manager_record)
        for replica_id in REPLICA_IDS:
            record = runner.spawn_process(
                f"replica-{replica_id}",
                replica_commands[replica_id],
                run_directory / "logs" / f"replica-{replica_id}.log",
                run_directory,
                replica_id=replica_id,
            )
            records.append(record)
            runner.register_fault_replica(registry, record)

        def allow_manager_exit(record: Any) -> bool:
            return (
                record.name == MANAGER_SOURCE_ID
                and record.process.poll() == 0
            )

        def all_ready() -> bool:
            streams = runner._event_streams(run_directory)
            sources = (
                *(f"replica-{replica_id}" for replica_id in REPLICA_IDS),
                MANAGER_SOURCE_ID,
            )
            return all(
                sum(
                    event.get("event_type") == "process.ready"
                    for event in streams[source_id]
                )
                == 1
                for source_id in sources
            )

        runner._wait(
            "all N=7 comparison processes ready",
            args.startup_timeout,
            records,
            all_ready,
            allow_clean_exit=allow_manager_exit,
        )
        if arm.name != "sigkill_crash":
            lifecycle.start(arm.plan.actions[0].fault_id)

        def first_common_commit() -> dict[str, object] | None:
            streams = runner._event_streams(run_directory)
            return common_commit_after(
                runner,
                streams,
                participants=REPLICA_IDS,
                after_ns=0,
            )

        before_commit = runner._wait(
            "first common N=7 commit",
            args.startup_timeout,
            records,
            first_common_commit,
            allow_clean_exit=allow_manager_exit,
        )
        if arm.name != "sigkill_crash":
            fault_marker_offset = fault_log_cursor(run_directory)
            if find_fault_marker(
                run_directory,
                arm.name,
                end_offset=fault_marker_offset,
            ) is not None:
                raise ComparisonRunError(
                    "Byzantine action occurred before the baseline commit"
                )
        state["phase"] = "awaiting_epoch0_tree6"
        state["common_commit_before"] = before_commit
        state["fault_log_baseline_cursor"] = fault_marker_offset
        runner._replace_json(state_path, state)

        watermarks, offsets = runner.replica_event_tail_snapshot(
            run_directory
        )
        runtime = runner.runtime_parameters(
            profile,
            app_binary=binaries["app"],
            manager_binary=binaries["manager"],
        )
        poller = runner.FreshConfigurationPoller(
            run_directory,
            watermarks,
            start_offsets=offsets,
            maximum_skew_ns=(
                int(runtime["aggregation_timeout_ms"]) * 1_000_000
            ),
        )
        boundary = runner._wait(
            "fresh common epoch-0 tree-6 configuration",
            args.fault_timeout,
            records,
            poller.poll,
            allow_clean_exit=allow_manager_exit,
        )
        if boundary.get("epoch_digest") != EPOCH0_DIGEST:
            raise ComparisonRunError(
                "live epoch-0 digest differs from frozen launch binding"
            )

        state["phase"] = "inject_or_observe_fault"
        state["tree6_boundary"] = boundary
        runner._replace_json(state_path, state)
        if arm.name == "sigkill_crash":
            action = arm.plan.actions_of_type(ReplicaGroupSigkill)[0]
            lifecycle.start(action.fault_id)
            outcome = registry.sigkill_replica_group(
                fault_id=action.fault_id,
                replica_id=action.replica_id,
                timeout_s=args.crash_confirm_timeout,
            )
            lifecycle.terminal(
                action.fault_id,
                "succeeded",
                {
                    "confirmed_monotonic_raw_ns": (
                        outcome.confirmed_monotonic_ns
                    ),
                    "pgid": outcome.pgid,
                    "pid": outcome.pid,
                    "replica_id": outcome.replica_id,
                    "requested_monotonic_raw_ns": (
                        outcome.requested_monotonic_ns
                    ),
                    "returncode": outcome.returncode,
                    "signal": "SIGKILL",
                    "signal_number": outcome.signal_number,
                },
            )
            expected_crashed.add(FAULTY_REPLICA_ID)
            fault_observed_ns = outcome.confirmed_monotonic_ns
            action_observation = {
                "kind": "replica_group_sigkill",
                "fault_id": action.fault_id,
                "replica_id": outcome.replica_id,
                "requested_monotonic_raw_ns": (
                    outcome.requested_monotonic_ns
                ),
                "confirmed_monotonic_raw_ns": (
                    outcome.confirmed_monotonic_ns
                ),
                "signal": "SIGKILL",
                "returncode": outcome.returncode,
            }
        else:
            assert fault_marker_offset is not None
            marker = runner._wait(
                f"{arm.name} runtime marker",
                args.fault_timeout,
                records,
                lambda: find_fault_marker(
                    run_directory,
                    arm.name,
                    start_offset=fault_marker_offset,
                ),
                allow_clean_exit=allow_manager_exit,
            )
            fault_observed_ns = runner.monotonic_raw_ns()
            action_observation = {
                **marker,
                "fault_id": arm.plan.actions[0].fault_id,
                "observed_monotonic_raw_ns": fault_observed_ns,
                "configuration": EXACT_CONFIGURATION,
                "context_limit": args.context_limit,
                "log_start_offset": fault_marker_offset,
            }
            lifecycle.terminal(
                arm.plan.actions[0].fault_id,
                "succeeded",
                action_observation,
            )

        participants = tuple(
            replica_id
            for replica_id in REPLICA_IDS
            if replica_id not in expected_crashed
        )

        def post_fault_commit() -> dict[str, object] | None:
            streams = runner._event_streams(run_directory)
            return common_commit_after(
                runner,
                streams,
                participants=participants,
                after_ns=fault_observed_ns,
            )

        after_commit = runner._wait(
            "first common post-fault commit",
            args.post_fault_timeout,
            records,
            post_fault_commit,
            expected_crashed=expected_crashed,
            allow_clean_exit=allow_manager_exit,
        )
        runner._check_processes(
            records,
            expected_crashed,
            allow_clean_exit=allow_manager_exit,
        )
        streams = runner._event_streams(run_directory)
        fixed_quorum = observed_fixed_quorum(streams)
        conflicts = conflicting_commits(runner, streams)
        if arm.name != "sigkill_crash":
            assert fault_marker_offset is not None
            final_marker = find_fault_marker(
                run_directory,
                arm.name,
                start_offset=fault_marker_offset,
            )
            if final_marker is None:
                raise ComparisonRunError(
                    "Byzantine action marker disappeared before validation"
                )
            count = final_marker["matching_line_count"]
            if not isinstance(count, int) or not (
                1 <= count <= args.context_limit
            ):
                raise ComparisonRunError(
                    "Byzantine action exceeded its proposal-context bound"
                )
            assert action_observation is not None
            action_observation.update(final_marker)
        if fixed_quorum["invalid_records"]:
            raise ComparisonRunError(
                "an active configuration changed the fixed quorum"
            )
        if conflicts:
            raise ComparisonRunError(
                "conflicting committed hashes were observed"
            )
        state["phase"] = "validated"
    except KeyboardInterrupt:
        interrupted = True
        runtime_error = "comparison interrupted"
    except (
        ComparisonRunError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        runtime_error = str(exc)
    finally:
        state["phase"] = "cleanup"
        state["runtime_error"] = runtime_error
        runner._replace_json(state_path, state)
        try:
            if records:
                cleanup = _shutdown_records(records)
            remaining = runner._wait_listeners_stopped(
                runner.required_ports(
                    args.peer_port,
                    args.client_port,
                    args.manager_port,
                ),
                5.0,
            )
            if remaining:
                cleanup.append(
                    {
                        "error": "listeners remained after cleanup",
                        "ports": remaining,
                    }
                )
                runtime_error = (
                    f"{runtime_error}; " if runtime_error else ""
                ) + f"listeners remained after cleanup: {remaining}"
        except (ComparisonRunError, OSError, RuntimeError) as exc:
            runtime_error = (
                f"{runtime_error}; " if runtime_error else ""
            ) + f"cleanup failed: {exc}"
        try:
            resources.close()
        except (OSError, RuntimeError, ValueError) as exc:
            runtime_error = (
                f"{runtime_error}; " if runtime_error else ""
            ) + f"fault evidence close failed: {exc}"
        if action_observation is not None:
            try:
                action_observation["fault_evidence"] = (
                    validate_fault_evidence(
                        run_directory,
                        arm,
                        expected_status="succeeded",
                    )
                )
            except ComparisonRunError as exc:
                runtime_error = (
                    f"{runtime_error}; " if runtime_error else ""
                ) + str(exc)

    verdict = build_arm_verdict(
        arm=arm,
        run_id=run_id,
        kauri_revision=revision,
        action_observation=action_observation,
        before_commit=before_commit,
        after_commit=after_commit,
        fixed_quorum=fixed_quorum,
        conflicts=conflicts,
        runtime_error=runtime_error,
    )
    verdict["cleanup"] = cleanup
    verdict["interrupted"] = interrupted
    _write_or_replace_json(
        runner,
        run_directory / "arm-verdict.json",
        verdict,
    )
    state["phase"] = "finished"
    state["runtime_error"] = runtime_error
    state["verdict"] = verdict["verdict"]
    state["finished_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    runner._replace_json(state_path, state)

    print(f"results: {run_directory}")
    print(f"{verdict['verdict']}: {run_directory / 'arm-verdict.json'}")
    return 0 if verdict["verdict"] == "PASS" else 1


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    repository = KAURI_REPOSITORY_ROOT
    scenario_directory = (
        repository / "experiments" / "adaptive" / "n7-crash-recovery"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=ARM_NAMES, required=True)
    parser.add_argument("--repository", type=Path, default=repository)
    parser.add_argument(
        "--profile",
        type=Path,
        default=scenario_directory / "profile.json",
    )
    parser.add_argument(
        "--app-binary",
        type=Path,
        default=repository / "build-adaptive/examples/hotstuff-app",
    )
    parser.add_argument(
        "--manager-binary",
        type=Path,
        default=(
            repository / "build-adaptive/examples/adaptation-manager"
        ),
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
        default=repository / "results/n7-fault-comparison",
    )
    parser.add_argument("--peer-port", type=int, default=28100)
    parser.add_argument("--client-port", type=int, default=29100)
    parser.add_argument("--manager-port", type=int, default=30100)
    parser.add_argument("--startup-timeout", type=float, default=90.0)
    parser.add_argument("--fault-timeout", type=float, default=120.0)
    parser.add_argument("--post-fault-timeout", type=float, default=90.0)
    parser.add_argument("--crash-confirm-timeout", type=float, default=5.0)
    parser.add_argument(
        "--context-limit",
        type=int,
        default=DEFAULT_CONTEXT_LIMIT,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "persist the canonical plan and exact launch overlays without "
            "claiming a live verdict"
        ),
    )
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    repository = args.repository.resolve()
    runner = load_n7_runner(repository)
    snapshot = runner.verify_repository_state(repository)
    profile_path = args.profile.resolve()
    profile, profile_bytes = runner.load_frozen_profile(profile_path)
    _profile_checks(profile)
    replica_launch_overlays(
        build_arm(args.arm, snapshot.revision),
        context_limit=args.context_limit,
    )
    if min(
        args.startup_timeout,
        args.fault_timeout,
        args.post_fault_timeout,
        args.crash_confirm_timeout,
    ) <= 0:
        raise ComparisonRunError("comparison timeouts must be positive")

    results_root = args.results_root.resolve() / args.arm
    run_directory = runner.create_run_directory(results_root)
    arm = build_arm(args.arm, snapshot.revision)
    if args.dry_run:
        return _dry_run(
            runner=runner,
            arm=arm,
            run_directory=run_directory,
            revision=snapshot.revision,
            profile_path=profile_path,
            profile_sha256=runner.sha256_bytes(profile_bytes),
            context_limit=args.context_limit,
        )

    binaries = {
        "app": args.app_binary.resolve(),
        "manager": args.manager_binary.resolve(),
        "keygen": args.keygen_binary.resolve(),
        "tls_keygen": args.tls_keygen_binary.resolve(),
    }
    for label, path in binaries.items():
        runner._assert_executable(path, label)
    ports = runner.required_ports(
        args.peer_port,
        args.client_port,
        args.manager_port,
    )
    occupied = runner.ports_in_use(ports)
    if occupied:
        raise ComparisonRunError(
            f"comparison ports are already in use: {occupied}"
        )
    return _run_live(
        args=args,
        runner=runner,
        arm=arm,
        run_directory=run_directory,
        revision=snapshot.revision,
        profile=profile,
        profile_path=profile_path,
        profile_bytes=profile_bytes,
        binaries=binaries,
    )


def main() -> None:
    try:
        raise SystemExit(run())
    except ComparisonRunError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
