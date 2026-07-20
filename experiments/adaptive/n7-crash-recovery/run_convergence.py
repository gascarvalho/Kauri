#!/usr/bin/env python3
"""Run the frozen N=7 epoch-1 convergence-only experiment."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence
import uuid

import run as base


PROFILE_ID = "n7-f2-q5-epoch1-convergence-v2"
PROFILE_SHA256 = "4146e736501e5b6f07409ccf83cd3b2b39fbefee3a89590f47b03bdcc1db16ae"
SCENARIO = "n7-epoch1-convergence"
RESULT_ROOT_NAME = "n7-epoch1-convergence"
MANAGER_SOURCE_ID = "adaptive-manager"


class RunnerError(base.RunnerError):
    """A convergence-only precondition or orchestration failure."""


def _expected_profile() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "profile_id": PROFILE_ID,
        "frozen": True,
        "base_profile": {
            "identity": base.PROFILE_ID,
            "sha256": base.PROFILE_SHA256,
        },
        "claim_scope": "epoch1_convergence_only",
        "replica_ids": list(base.REPLICA_IDS),
        "fault_threshold": base.FAULT_THRESHOLD,
        "quorum": base.QUORUM,
        "crash_targets": list(base.CRASH_TARGETS),
        "survivors": list(base.SURVIVORS),
        "successor_epoch": 1,
        "activation_delay_blocks": base.ACTIVATION_DELAY_BLOCKS,
        "fault_injection": {
            "bundle_delivery": {"recipient": 2, "attempt": 1},
            "activation_ack": {"positive_ack_ordinal": 1},
        },
        "requirements": {
            "matching_activation_sources": base.QUORUM,
            "exactly_one_converged": True,
            "exactly_one_ready": True,
            "require_ack_retransmission": True,
            "require_common_successor_commit": True,
            "forbid_epoch_above": 1,
            "throughput_claim": False,
        },
        "timeouts": {
            "startup_s": 90,
            "phase_s": 240,
            "manager_convergence_deadline_s": 120,
            "crash_confirm_s": 5,
            "ack_drain_s": 2,
        },
    }


def load_frozen_profile(path: Path) -> tuple[dict[str, Any], bytes]:
    """Load the byte-frozen convergence profile without touching the base."""
    try:
        payload = path.read_bytes()
        profile = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerError(f"cannot load frozen convergence profile: {exc}") from exc
    if hashlib.sha256(payload).hexdigest() != PROFILE_SHA256:
        raise RunnerError("frozen convergence profile SHA-256 differs")
    if profile != _expected_profile():
        raise RunnerError("convergence profile differs from the exact contract")
    return profile, payload


def loss_control_arguments(profile: Mapping[str, Any]) -> tuple[str, ...]:
    """Translate only the two frozen, explicit one-shot loss controls."""
    if dict(profile) != _expected_profile():
        raise RunnerError("loss controls require the exact convergence profile")
    bundle = profile["fault_injection"]["bundle_delivery"]
    acknowledgement = profile["fault_injection"]["activation_ack"]
    bundle_attempt = f"{bundle['recipient']}:{bundle['attempt']}"
    acknowledgement_ordinal = str(
        acknowledgement["positive_ack_ordinal"]
    )
    if bundle_attempt != "2:1" or acknowledgement_ordinal != "1":
        raise RunnerError("frozen loss-control arguments are not exact")
    return (
        "--experiment-drop-bundle-attempt",
        bundle_attempt,
        "--experiment-drop-activation-ack",
        acknowledgement_ordinal,
    )


def manager_convergence_arguments(
    profile: Mapping[str, Any],
) -> tuple[str, ...]:
    """Translate the exact v2 manager deadline and one-shot loss controls."""
    if dict(profile) != _expected_profile():
        raise RunnerError("manager controls require the exact convergence profile")
    deadline_seconds = profile["timeouts"][
        "manager_convergence_deadline_s"
    ]
    return (
        "--convergence-deadline-seconds",
        str(deadline_seconds),
        *loss_control_arguments(profile),
    )


def validate_manager_exit(exit_code: int, ready_count: int) -> None:
    if exit_code != 0 or ready_count != 1:
        raise RunnerError(
            "manager exit zero requires exactly one adaptive_v2_ready event"
        )


def _event_payloads(
    events: Sequence[Mapping[str, Any]],
    event_type: str,
) -> list[dict[str, Any]]:
    return [
        dict(event["payload"])
        for event in events
        if event.get("event_type") == event_type
        and isinstance(event.get("payload"), dict)
    ]


def _loss_control_state(
    manager_events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    deliveries = _event_payloads(
        manager_events, "adaptive_v2_delivery_attempt"
    )
    activation_events = [
        (event, dict(event["payload"]))
        for event in manager_events
        if event.get("event_type") == "adaptive_v2_activation_observed"
        and isinstance(event.get("payload"), dict)
    ]
    dropped_bundle = [
        payload
        for payload in deliveries
        if payload.get("replica_id") == 2
        and payload.get("delivery_attempt") == 1
        and payload.get("disposition") == "injected_drop"
    ]
    dropped_acknowledgement = [
        (event, payload)
        for event, payload in activation_events
        if payload.get("disposition") == "ack_injected_drop"
    ]
    if len(dropped_bundle) != 1 or len(dropped_acknowledgement) != 1:
        raise RunnerError("manager did not audit both exact one-shot losses")
    bundle_digest = dropped_bundle[0].get("canonical_payload_digest")
    _, acknowledgement_payload = dropped_acknowledgement[0]
    acknowledgement_digest = acknowledgement_payload.get(
        "canonical_payload_digest"
    )
    for label, digest in (
        ("bundle", bundle_digest),
        ("activation acknowledgement", acknowledgement_digest),
    ):
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or digest == "0" * 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise RunnerError(f"{label} loss lacks a canonical payload digest")
    ready_events = [
        event
        for event in manager_events
        if event.get("event_type") == "adaptive_v2_ready"
    ]
    if len(ready_events) != 1:
        raise RunnerError("ACK recovery boundary requires exactly one ready event")
    ready_ns = base._event_timestamp(ready_events[0])
    acknowledgement_target = acknowledgement_payload.get("replica_id")
    recovered_acknowledgements = [
        event
        for event, payload in activation_events
        if payload.get("disposition") == "ack_sent"
        and payload.get("replica_id") == acknowledgement_target
        and payload.get("canonical_payload_digest") == acknowledgement_digest
        and base._event_timestamp(event) > ready_ns
    ]
    if len(recovered_acknowledgements) != 1:
        raise RunnerError("manager did not audit one exact post-ready ACK recovery")
    ack_source_monotonic_ns = base._event_timestamp(
        recovered_acknowledgements[0]
    )
    return {
        "bundle_delivery": {
            "recipient": 2,
            "attempt": 1,
            "observed": True,
            "canonical_payload_digest": bundle_digest,
        },
        "activation_ack": {
            "positive_ack_ordinal": 1,
            "observed": True,
            "replica_id": acknowledgement_target,
            "canonical_payload_digest": acknowledgement_digest,
            "ack_source_monotonic_ns": ack_source_monotonic_ns,
        },
    }


def _wait_manager_exit(
    manager: base.ProcessRecord,
    records: Sequence[base.ProcessRecord],
    *,
    timeout_s: float,
) -> int:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for record in records:
            if record is manager:
                continue
            return_code = record.process.poll()
            if record.replica_id in base.CRASH_TARGETS:
                if return_code not in (None, -signal.SIGKILL):
                    raise RunnerError(
                        f"{record.name} exited with unexpected status {return_code}"
                    )
            elif return_code is not None:
                raise RunnerError(
                    f"{record.name} exited before manager drain: {return_code}"
                )
        return_code = manager.process.poll()
        if return_code is not None:
            return return_code
        time.sleep(0.02)
    raise RunnerError("timed out waiting for manager ACK drain exit")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(
    *,
    run_directory: Path,
    run_id: str,
    revision: str,
    profile_bytes: bytes,
    source_instances: Mapping[str, str],
    process_records: Sequence[base.ProcessRecord],
    manager_command: Sequence[str],
    manager_exit_code: int,
    crash_markers: Sequence[Mapping[str, Any]],
    crash_configuration_boundary: Mapping[str, Any],
) -> dict[str, Any]:
    records_by_source = {record.name: record for record in process_records}
    sources: list[dict[str, Any]] = []
    for replica in base.REPLICA_IDS:
        source_id = f"replica-{replica}"
        record = records_by_source[source_id]
        path = run_directory / "raw" / f"replica-{replica}.jsonl"
        sources.append(
            {
                "source_kind": "replica",
                "source_id": source_id,
                "source_instance": source_instances[source_id],
                "pid": record.pid,
                "pgid": record.pgid,
                "path": path.relative_to(run_directory).as_posix(),
                "sha256": _sha256(path),
            }
        )
    manager_path = run_directory / "raw" / "adaptive-manager.jsonl"
    manager_record = records_by_source[MANAGER_SOURCE_ID]
    sources.append(
        {
            "source_kind": "adaptation_manager",
            "source_id": MANAGER_SOURCE_ID,
            "source_instance": source_instances[MANAGER_SOURCE_ID],
            "pid": manager_record.pid,
            "pgid": manager_record.pgid,
            "path": manager_path.relative_to(run_directory).as_posix(),
            "sha256": _sha256(manager_path),
        }
    )
    artifacts = []
    for kind, name in (
        ("profile", "convergence-profile.json"),
        ("successor_bundle", "successor.bundle"),
        ("epochs", "epochs.json"),
        ("runner_state", "runner-state.json"),
    ):
        path = run_directory / name
        artifacts.append(
            {"kind": kind, "path": name, "sha256": _sha256(path)}
        )
    return {
        "schema_version": 1,
        "scenario": SCENARIO,
        "run_id": run_id,
        "kauri_revision": revision,
        "kauri_worktree_clean": True,
        "profile": {
            "identity": PROFILE_ID,
            "path": "convergence-profile.json",
            "sha256": hashlib.sha256(profile_bytes).hexdigest(),
        },
        "run_completion": {
            "complete": True,
            "interrupted": False,
            "runtime_error": None,
            "manager_exit_code": manager_exit_code,
            "unexpected_survivor_exits": [],
        },
        "replica_count": len(base.REPLICA_IDS),
        "fault_threshold": base.FAULT_THRESHOLD,
        "quorum": base.QUORUM,
        "membership": list(base.REPLICA_IDS),
        "crash_targets": list(base.CRASH_TARGETS),
        "survivors": list(base.SURVIVORS),
        "successor_epoch": 1,
        "crash_markers": list(crash_markers),
        "crash_configuration_boundary": dict(crash_configuration_boundary),
        "sources": sources,
        "artifacts": artifacts,
        "manager_argv": base.normalized_manager_argv(manager_command),
    }


def _arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    scenario_directory = Path(__file__).resolve().parent
    repository = scenario_directory.parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository", type=Path, default=repository
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=scenario_directory / "convergence-profile.json",
    )
    parser.add_argument(
        "--base-profile",
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
        default=repository / "build-adaptive/examples/adaptation-manager",
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
        default=repository / "results" / RESULT_ROOT_NAME,
    )
    parser.add_argument("--peer-port", type=int, default=28100)
    parser.add_argument("--client-port", type=int, default=29100)
    parser.add_argument("--manager-port", type=int, default=30100)
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    repository = args.repository.resolve()
    profile, profile_bytes = load_frozen_profile(args.profile.resolve())
    base_profile, _ = base.load_frozen_profile(args.base_profile.resolve())
    snapshot = base.verify_repository_state(repository)
    binaries = {
        "app": args.app_binary.resolve(),
        "manager": args.manager_binary.resolve(),
        "keygen": args.keygen_binary.resolve(),
        "tls_keygen": args.tls_keygen_binary.resolve(),
    }
    for label, path in binaries.items():
        base._assert_executable(path, label)
    ports = base.required_ports(
        args.peer_port, args.client_port, args.manager_port
    )
    occupied = base.ports_in_use(ports)
    if occupied:
        raise RunnerError(f"convergence ports are already in use: {occupied}")

    run_directory = base.create_run_directory(args.results_root.resolve())
    run_id = run_directory.name
    base._write_private(
        run_directory / "convergence-profile.json", profile_bytes
    )
    state_path = run_directory / "runner-state.json"
    state: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "phase": "identity_generation",
        "runtime_error": None,
    }
    base._write_json_exclusive(state_path, state)

    records: list[base.ProcessRecord] = []
    records_by_replica: dict[int, base.ProcessRecord] = {}
    runtime_error: str | None = None
    manager_exit_code = -1
    manager_command: tuple[str, ...] = ()
    crash_markers: list[dict[str, Any]] = []
    boundary: dict[str, Any] = {}
    source_instances = {
        f"replica-{replica}": f"{run_id}-replica-{replica}-{uuid.uuid4().hex}"
        for replica in base.REPLICA_IDS
    }
    source_instances[MANAGER_SOURCE_ID] = (
        f"{run_id}-manager-{uuid.uuid4().hex}"
    )
    try:
        bls, tls, issuer = base.generate_identities(
            binaries["keygen"],
            binaries["tls_keygen"],
            run_directory / "config",
        )
        _, _, manager_command, replica_commands, _ = base.write_runtime_inputs(
            run_directory,
            base_profile,
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
            manager_extra_args=manager_convergence_arguments(profile),
        )
        state["phase"] = "launch"
        base._replace_json(state_path, state)
        manager = base.spawn_process(
            MANAGER_SOURCE_ID,
            manager_command,
            run_directory / "logs" / "adaptive-manager.log",
            run_directory,
            replica_id=None,
        )
        records.append(manager)
        for replica in base.REPLICA_IDS:
            record = base.spawn_process(
                f"replica-{replica}",
                replica_commands[replica],
                run_directory / "logs" / f"replica-{replica}.log",
                run_directory,
                replica_id=replica,
            )
            records.append(record)
            records_by_replica[replica] = record

        def all_ready() -> bool:
            streams = base._event_streams(run_directory)
            return all(
                sum(
                    event.get("event_type") == "process.ready"
                    for event in streams[source]
                )
                == 1
                for source in (
                    *[f"replica-{replica}" for replica in base.REPLICA_IDS],
                    MANAGER_SOURCE_ID,
                )
            )

        base._wait(
            "all process.ready events",
            float(profile["timeouts"]["startup_s"]),
            records,
            all_ready,
        )

        def one_baseline_cycle() -> dict[str, Any] | None:
            streams = base._event_streams(run_directory)
            first = base.find_first_common_epoch_commit(
                streams[base.AUTHORITATIVE_SOURCE_ID],
                base.commit_witness_timestamps(streams, base.REPLICA_IDS),
                participants=base.REPLICA_IDS,
                epoch_number=0,
            )
            if first is None:
                return None
            return base.find_common_root_cycle(
                streams[base.AUTHORITATIVE_SOURCE_ID],
                base.commit_witness_timestamps(streams, base.REPLICA_IDS),
                participants=base.REPLICA_IDS,
                epoch_number=0,
                tree_roots={replica: replica for replica in base.REPLICA_IDS},
                expected_roots=base.REPLICA_IDS,
                require_terminal=False,
            )

        base._wait(
            "one common epoch-0 root cycle",
            float(profile["timeouts"]["phase_s"]),
            records,
            one_baseline_cycle,
        )
        source_watermarks, source_offsets = base.replica_event_tail_snapshot(
            run_directory
        )
        runtime = base.runtime_parameters(base_profile)
        boundary_poller = base.FreshConfigurationPoller(
            run_directory,
            source_watermarks,
            start_offsets=source_offsets,
            maximum_skew_ns=runtime["aggregation_timeout_ms"] * 1_000_000,
        )
        boundary = base._wait(
            "fresh common epoch-0 tree-6 configuration",
            float(profile["timeouts"]["phase_s"]),
            records,
            boundary_poller.poll,
        )
        state["phase"] = "crash"
        base._replace_json(state_path, state)
        crash_markers = base.inject_sigkill_crashes(
            records_by_replica,
            base.CRASH_TARGETS,
            timeout_s=float(profile["timeouts"]["crash_confirm_s"]),
        )
        base.assert_crash_boundary_held(
            boundary,
            base._event_streams(run_directory),
            crash_request_ns=max(
                marker["requested_monotonic_raw_ns"]
                for marker in crash_markers
            ),
        )
        state["crash_markers"] = crash_markers
        state["crash_configuration_boundary"] = boundary
        base._replace_json(state_path, state)

        def convergence_ready() -> tuple[dict[str, Any], dict[str, Any]] | None:
            streams = base._event_streams(run_directory)
            manager_ready = _event_payloads(
                streams[MANAGER_SOURCE_ID], "adaptive_v2_ready"
            )
            command = base._common_single_payload(
                streams, "epoch.command_committed"
            )
            activation = base._common_single_payload(streams, "epoch.activated")
            if len(manager_ready) != 1 or command is None or activation is None:
                return None
            return command, manager_ready[0]

        state["phase"] = "awaiting_convergence"
        base._replace_json(state_path, state)
        command_payload, _ = base._wait(
            "exactly one manager convergence-ready event",
            float(profile["timeouts"]["phase_s"]),
            records,
            convergence_ready,
            expected_crashed=set(base.CRASH_TARGETS),
        )
        decoded = base.decode_epoch_change_bundle(
            (run_directory / "successor.bundle").read_bytes()
        )
        base._write_json_exclusive(
            run_directory / "epochs.json",
            base.build_epochs_document(decoded, command_payload),
        )
        manager_exit_code = _wait_manager_exit(
            manager,
            records,
            timeout_s=float(profile["timeouts"]["ack_drain_s"]),
        )
        manager_events = base._event_streams(run_directory)[MANAGER_SOURCE_ID]
        ready_count = sum(
            event.get("event_type") == "adaptive_v2_ready"
            for event in manager_events
        )
        validate_manager_exit(manager_exit_code, ready_count)
        state["manager_exit_code"] = manager_exit_code
        state["ready_event_count"] = ready_count
        loss_controls = _loss_control_state(manager_events)
        state["loss_controls"] = loss_controls
        ack_source_monotonic_ns = loss_controls["activation_ack"][
            "ack_source_monotonic_ns"
        ]

        def first_common_successor_commit() -> base.CommonEpochCommit | None:
            streams = base._event_streams(run_directory)
            return base.find_first_common_epoch_commit(
                streams[base.AUTHORITATIVE_SOURCE_ID],
                base.commit_witness_timestamps(streams, base.SURVIVORS),
                participants=base.SURVIVORS,
                epoch_number=1,
                strictly_after_ns=ack_source_monotonic_ns,
            )

        common_successor = base._wait(
            "first common epoch-1 commit from every survivor",
            float(profile["timeouts"]["phase_s"]),
            [record for record in records if record is not manager],
            first_common_successor_commit,
            expected_crashed=set(base.CRASH_TARGETS),
        )
        common_payload = common_successor.observer_event.get("payload")
        if not isinstance(common_payload, dict):
            raise RunnerError("common epoch-1 commit has no payload")
        state["first_common_successor_commit"] = {
            "epoch_number": 1,
            "block_height": common_payload.get("block_height"),
            "block_hash": common_payload.get("block_hash"),
            "common_monotonic_ns": common_successor.common_ns,
            "participants": list(base.SURVIVORS),
            "authoritative_observer": base.AUTHORITATIVE_SOURCE_ID,
        }
        base._replace_json(state_path, state)
    except (
        RunnerError,
        base.RunnerError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        runtime_error = str(exc)
    finally:
        state["phase"] = "cleanup"
        state["runtime_error"] = runtime_error
        base._replace_json(state_path, state)
        if records:
            try:
                unexpected = base._shutdown_processes(records)
                if unexpected:
                    runtime_error = runtime_error or str(unexpected)
            except base.RunnerError as exc:
                runtime_error = runtime_error or str(exc)
        remaining = base._wait_listeners_stopped(ports, 5.0)
        if remaining:
            runtime_error = runtime_error or f"listeners remained active: {remaining}"
        state["phase"] = "finished"
        state["runtime_error"] = runtime_error
        state["finished_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        base._replace_json(state_path, state)

    print(f"results: {run_directory}")
    if runtime_error is not None:
        print(f"INCOMPLETE: {runtime_error}")
        return 1
    manifest = _manifest(
        run_directory=run_directory,
        run_id=run_id,
        revision=snapshot.revision,
        profile_bytes=profile_bytes,
        source_instances=source_instances,
        process_records=records,
        manager_command=manager_command,
        manager_exit_code=manager_exit_code,
        crash_markers=crash_markers,
        crash_configuration_boundary=boundary,
    )
    manifest_path = run_directory / "manifest.json"
    base._write_json_exclusive(manifest_path, manifest)
    validator = Path(__file__).resolve().with_name("convergence_validator.py")
    validated = run_directory / "validated"
    result = subprocess.run(
        [
            sys.executable,
            str(validator),
            "--manifest",
            str(manifest_path),
            "--epochs",
            str(run_directory / "epochs.json"),
            "--output-dir",
            str(validated),
        ],
        cwd=repository,
        check=False,
    )
    if result.returncode != 0:
        print("FAIL: convergence validator rejected the preserved run")
        return 1
    print(f"PASS: {validated}")
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except RunnerError as exc:
        print(f"runner error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
