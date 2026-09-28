"""Local-only launch-plan adapter for the prospective N=7 omission study.

This module deliberately prepares inputs only.  The E0 identity is derived by
the native read-only preflight over the copied topology artifact; it is never
accepted from a caller.  Process execution remains disabled.
"""
from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence

HERE = Path(__file__).resolve().parent
KAURI = HERE.parents[2]
PROFILE_V2_FILE = HERE / "profile-v2.json"
PROFILE_V2_SHA256 = "1c4f44a9290440fe0290ceacbb9719e85581246a9c423413c4e898aac15b76fa"
V2_MANAGER_ARGS = ("--required-nonresponsive", "1")
EXACT_TIMEOUT_EVIDENCE_OPTION = b"experiment-exact-timeout-attempt-evidence-v3 = true\n"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


runner = _load("n7_three_runner", HERE / "runner.py")
base = _load("n7_base_runner", KAURI / "experiments" / "adaptive" / "n7-crash-recovery" / "run.py")
comparison = _load("n7_comparison_lifecycle", KAURI / "experiments" / "adaptive" / "run_fault_comparison.py")


class AdapterError(ValueError):
    pass


def _enable_exact_timeout_attempt_evidence_v3(configs: Sequence[Path]) -> None:
    """Enable the evidence schema required by the frozen six-timeout validator."""
    if len(configs) != 7 or len(set(configs)) != 7:
        raise AdapterError("replica timeout-evidence configuration drifted")
    payloads = []
    for path in configs:
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise AdapterError("replica timeout-evidence configuration drifted") from exc
        if not payload.endswith(b"\n") or b"experiment-exact-timeout-attempt-evidence-v3" in payload:
            raise AdapterError("replica timeout-evidence configuration drifted")
        payloads.append(payload)
    for path, payload in zip(configs, payloads, strict=True):
        path.write_bytes(payload + EXACT_TIMEOUT_EVIDENCE_OPTION)


def _load_frozen_v2_profile(path: Path = PROFILE_V2_FILE) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
        profile = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError(f"cannot load frozen v2 profile: {exc}") from exc
    if hashlib.sha256(payload).hexdigest() != PROFILE_V2_SHA256:
        raise AdapterError("frozen v2 profile SHA-256 differs from the canonical profile")
    expected = {
        "schema_version": 1,
        "profile_id": runner.SCENARIO,
        "frozen": True,
        "replica_ids": list(range(7)),
        "fault_threshold": 2,
        "quorum": 5,
        "crash_targets": [],
        "transition_requests": [
            {
                "policy_intent": "fault_containment",
                "evidence_window_rule": "fresh_exact_predecessor_after_common_commit",
                "transition_artifact_id": "e0-to-e1-containment",
                "bundle_path": "transitions/e0-to-e1-containment/successor.bundle",
                "evidence_snapshot_path": "transitions/e0-to-e1-containment/evidence-snapshot.json",
                "predecessor_epoch_number": 0,
                "successor_epoch_number": 1,
                "minimum_predecessor_residency_ms": 0,
                "containment_baseline_root_source": "live_predecessor_roots",
                "policy_parameters": {},
            }
        ],
        "fanout": 2,
        "pipeline_depth": 2,
        "block_size": 1,
        "tree_switch_period_blocks": 2,
        "aggregation_timeout_s": 0.5,
        "leader_progress_timeout_s": 5.0,
        "leader_activation_grace_s": 1.0,
        "activation_delay_blocks": 5,
        "snapshot_seed": 41719,
        "required_nonresponsive": 1,
        "epoch0_tree_sha256": runner.sha256_file(runner.TREE_FILE),
        "throughput_windows": [
            {"phase": "baseline", "epoch_number": 0, "bucket_count": 1},
            {"phase": "degraded", "epoch_number": 0, "bucket_count": 1},
            {"phase": "containment", "epoch_number": 1, "bucket_count": 1},
        ],
    }
    if profile != expected:
        raise AdapterError("frozen v2 profile fields differ from the six-context no-crash contract")
    return profile


def _require_v2_profile(
    profile: Mapping[str, Any], path: Path,
) -> dict[str, Any]:
    frozen = _load_frozen_v2_profile(path)
    if dict(profile) != frozen:
        raise AdapterError("caller profile differs from the frozen v2 no-crash profile")
    return frozen


def _plan_digest(plan: Mapping[str, Any]) -> str:
    value = {key: item for key, item in plan.items() if key != "plan_sha256"}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _under_run_root(run_directory: Path, relative: str) -> Path:
    candidate = (run_directory / relative).resolve()
    try:
        candidate.relative_to(run_directory.resolve())
    except ValueError as exc:
        raise AdapterError("plan path escapes run directory") from exc
    return candidate


def _derive_e0_identity(
    run_directory: Path,
    copied_tree: Path,
    helper_binary: Path,
    *,
    invoke=subprocess.run,
) -> dict[str, Any]:
    """Run the native, read-only E0 helper against the archived tree bytes."""
    if not copied_tree.is_file() or not helper_binary.is_file():
        raise AdapterError("E0 identity tree or helper binary is unavailable")
    tree_sha256 = base.sha256_file(copied_tree)
    helper_sha256 = base.sha256_file(helper_binary)
    try:
        completed = invoke(
            (str(helper_binary), str(copied_tree)),
            cwd=str(run_directory),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise AdapterError(f"cannot invoke E0 identity helper: {exc}") from exc
    if completed.returncode != 0:
        raise AdapterError(
            "E0 identity helper failed: " + str(completed.stderr).strip()[:512]
        )
    digest = completed.stdout.strip()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise AdapterError("E0 identity helper did not emit one canonical digest")
    if base.sha256_file(copied_tree) != tree_sha256 or base.sha256_file(helper_binary) != helper_sha256:
        raise AdapterError("E0 identity input or helper changed during derivation")
    return {
        "schema_version": 1,
        "state": "DERIVED_READ_ONLY",
        "epoch_number": 0,
        "epoch_digest": digest,
        "tree_file": str(copied_tree.resolve().relative_to(run_directory.resolve())),
        "tree_file_sha256": tree_sha256,
        "helper_binary": str(helper_binary.resolve()),
        "helper_binary_sha256": helper_sha256,
        "argv": [str(helper_binary.resolve()), str(copied_tree.resolve())],
    }


def _verify_executable_local_plan(
    run_directory: Path,
    plan: Mapping[str, Any],
    *,
    e0_helper_invoke=subprocess.run,
) -> tuple[tuple[str, ...], list[tuple[str, ...]]]:
    """Fail closed unless the archived, read-only plan is still exact."""
    main = _under_run_root(run_directory, plan["main_config"])
    if base.sha256_file(main) != plan["main_config_sha256"]:
        raise AdapterError("local plan main-config hash changed")
    artifacts = plan.get("runtime_artifacts")
    if not isinstance(artifacts, list):
        raise AdapterError("local plan lacks archived runtime artifacts")
    for artifact in artifacts:
        if not isinstance(artifact, Mapping) or not isinstance(artifact.get("path"), str) or not isinstance(artifact.get("sha256"), str):
            raise AdapterError("local plan artifact has schema drift")
        if base.sha256_file(_under_run_root(run_directory, artifact["path"])) != artifact["sha256"]:
            raise AdapterError("local plan artifact hash changed")
    replica_configs = plan.get("replica_configs")
    if (not isinstance(replica_configs, list) or len(replica_configs) != 7 or
        not all(isinstance(relative, str) for relative in replica_configs) or
        len(set(replica_configs)) != 7):
        raise AdapterError("local plan lacks seven v3 replica configurations")
    for relative in replica_configs:
        payload = _under_run_root(run_directory, relative).read_bytes()
        if (not payload.endswith(EXACT_TIMEOUT_EVIDENCE_OPTION) or
            payload.count(EXACT_TIMEOUT_EVIDENCE_OPTION) != 1):
            raise AdapterError("local plan replica lacks exact v3 timeout evidence")

    if not isinstance(plan.get("e0_identity_receipt"), str) or not isinstance(plan.get("e0_identity_receipt_sha256"), str):
        raise AdapterError("local plan lacks E0 identity receipt binding")
    receipt_path = _under_run_root(run_directory, plan["e0_identity_receipt"])
    if base.sha256_file(receipt_path) != plan["e0_identity_receipt_sha256"]:
        raise AdapterError("E0 identity receipt hash changed")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if not isinstance(receipt, Mapping) or receipt != plan.get("e0_identity") or receipt.get("state") != "DERIVED_READ_ONLY":
        raise AdapterError("E0 identity receipt is not plan-bound")
    copied_tree = _under_run_root(run_directory, receipt["tree_file"])
    if base.sha256_file(copied_tree) != receipt["tree_file_sha256"]:
        raise AdapterError("E0 identity tree hash changed")
    if not all(isinstance(receipt.get(field), str) for field in ("tree_file", "tree_file_sha256", "helper_binary", "helper_binary_sha256", "epoch_digest")):
        raise AdapterError("E0 identity receipt has schema drift")
    helper_binary = Path(receipt["helper_binary"])
    if base.sha256_file(helper_binary) != receipt["helper_binary_sha256"]:
        raise AdapterError("E0 identity helper hash changed")
    derived = _derive_e0_identity(
        run_directory, copied_tree, helper_binary, invoke=e0_helper_invoke
    )
    if derived != receipt:
        raise AdapterError("E0 identity helper output drifted")
    expected_preflight = runner.preflight(receipt["epoch_digest"], copied_tree)
    if expected_preflight != plan.get("preflight"):
        raise AdapterError("local plan preflight drifted")

    argv_path = run_directory / "runtime" / "launch-arguments.json"
    archived = json.loads(argv_path.read_text(encoding="utf-8"))
    processes = archived.get("processes")
    if not isinstance(processes, list) or len(processes) != 8 or not all(isinstance(process, Mapping) for process in processes):
        raise AdapterError("archived launch arguments have schema drift")
    by_source = {process.get("source_id"): process.get("argv") for process in processes}
    expected_sources = {"adaptive-manager", *(f"replica-{replica}" for replica in range(7))}
    if set(by_source) != expected_sources or any(not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv) for argv in by_source.values()):
        raise AdapterError("archived launch arguments do not name exactly eight commands")
    if not isinstance(plan.get("manager_command"), list) or not all(isinstance(arg, str) for arg in plan["manager_command"]):
        raise AdapterError("local plan manager command has schema drift")
    if not isinstance(plan.get("replica_commands"), list) or not all(isinstance(command, list) and all(isinstance(arg, str) for arg in command) for command in plan["replica_commands"]):
        raise AdapterError("local plan replica commands have schema drift")
    manager_command = tuple(plan["manager_command"])
    replica_commands = [tuple(command) for command in plan["replica_commands"]]
    if (
        len(replica_commands) != 7
        or by_source["adaptive-manager"]
        != base.normalized_manager_argv(manager_command)
    ):
        raise AdapterError("manager command drifted from archived launch arguments")
    if any(by_source[f"replica-{replica}"] != list(command) for replica, command in enumerate(replica_commands)):
        raise AdapterError("replica command drifted from archived launch arguments")
    for replica_id, command in enumerate(replica_commands):
        config = _under_run_root(run_directory, replica_configs[replica_id])
        positions = [index for index, argument in enumerate(command) if argument == "--conf"]
        if (len(positions) != 2 or positions[1] + 1 >= len(command) or
            Path(command[positions[1] + 1]).resolve() != config):
            raise AdapterError("replica command config path drifted")
        process = next(process for process in processes if process["source_id"] == f"replica-{replica_id}")
        options = process.get("effective_options")
        if (not isinstance(options, Mapping) or
            options.get("replica_config_sha256") != base.sha256_file(config) or
            options.get("experiment_exact_timeout_attempt_evidence_v3") is not True):
            raise AdapterError("replica config hash or v3 option drifted")
    try:
        tree_arg = manager_command.index("--epoch-zero-tree-file")
    except ValueError as exc:
        raise AdapterError("manager command lacks archived E0 tree input") from exc
    if tree_arg + 1 >= len(manager_command) or Path(manager_command[tree_arg + 1]).resolve() != copied_tree:
        raise AdapterError("manager command E0 tree input drifted")
    if manager_command.count("--required-nonresponsive") != 1 or manager_command[manager_command.index("--required-nonresponsive") + 1] != "1":
        raise AdapterError("manager command lacks the frozen v2 required-nonresponsive setting")
    overlay = tuple(expected_preflight["relay_omission"]["argv_overlay"])
    if not replica_commands[runner.OMITTING_REPLICA][-len(overlay):] == overlay:
        raise AdapterError("omitting replica command lacks exact E0 overlay")
    if any("--experiment-omit-outbound-aggregate" in command for replica, command in enumerate(replica_commands) if replica != runner.OMITTING_REPLICA):
        raise AdapterError("non-omitting replica command contains experimental overlay")
    return manager_command, replica_commands


def execute_local_plan(
    run_directory: Path,
    *,
    hard_timeout_s: float,
    authorization_path: Path | None = None,
    spawn=base.spawn_process,
    cleanup=comparison._shutdown_records,
    monotonic=time.monotonic,
    e0_helper_invoke=subprocess.run,
) -> dict[str, Any]:
    """Execute one local readiness smoke with no retry and sealed outcome."""
    if hard_timeout_s <= 0:
        raise AdapterError("hard timeout must be positive")
    plan_path = run_directory / "local-launch-plan.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if plan.get("state") != "PREPARED_E0_IDENTITY_DERIVED_EXECUTION_DISABLED" or plan.get("scenario") != runner.SCENARIO or plan.get("plan_sha256") != _plan_digest(plan):
            raise AdapterError("local plan is not integrity-bound")
        if authorization_path is None:
            raise AdapterError("separate plan-bound execution authorization is required")
        authorization = json.loads(authorization_path.read_text(encoding="utf-8"))
        if not isinstance(authorization, Mapping) or authorization.get("plan_sha256") != plan["plan_sha256"] or not isinstance(authorization.get("approval_reference"), str) or not authorization["approval_reference"]:
            raise AdapterError("execution authorization is not bound to this exact plan")
        manager_command, commands = _verify_executable_local_plan(
            run_directory, plan, e0_helper_invoke=e0_helper_invoke
        )
    except (OSError, KeyError, TypeError, json.JSONDecodeError, AdapterError) as exc:
        raise AdapterError(f"cannot execute local plan: {exc}") from exc
    records: list[Any] = []
    outcome: dict[str, Any]
    try:
        manager = spawn("adaptive-manager", manager_command, run_directory / "logs" / "adaptive-manager.log", run_directory, replica_id=None)
        records.append(manager)
        for replica_id, command in enumerate(commands):
            records.append(spawn(f"replica-{replica_id}", tuple(command), run_directory / "logs" / f"replica-{replica_id}.log", run_directory, replica_id=replica_id))
        deadline = monotonic() + hard_timeout_s
        while monotonic() < deadline:
            streams = base._event_streams(run_directory)
            if all(any(event.get("event_type") == "process.ready" for event in streams[f"replica-{rid}"]) for rid in range(7)) and any(event.get("event_type") == "process.ready" for event in streams["adaptive-manager"]):
                outcome = {"status": "LOCAL_SMOKE_INCOMPLETE", "reason": "readiness only; no fault, E1, commit, or metric validated"}
                break
            if any(record.process.poll() is not None for record in records):
                raise AdapterError("a local process exited before readiness")
            time.sleep(0.05)
        else:
            raise AdapterError("local readiness hard timeout expired")
    except Exception as exc:
        try:
            cleanup(records)
        finally:
            outcome = {"status": "ABORTED", "reason": str(exc)[:512], "claim_boundary": "no retry; no figure or thesis claim"}
            base._write_json_exclusive(run_directory / "local-execute-abort.json", outcome)
        return outcome
    cleanup(records)
    outcome["claim_boundary"] = "no fault, signed E1, commit, or throughput validation"
    base._write_json_exclusive(run_directory / "local-execute-receipt.json", outcome)
    return outcome


def prepare_local_inputs(
    run_directory: Path,
    profile: Mapping[str, Any],
    bls: Sequence[Mapping[str, str]],
    tls: Sequence[Mapping[str, str]],
    issuer: Mapping[str, str],
    *,
    peer_port: int,
    client_port: int,
    manager_port: int,
    run_id: str,
    repository_revision: str,
    source_instances: Mapping[str, str],
    app_binary: Path,
    manager_binary: Path,
    e0_helper_binary: Path | None = None,
    e0_helper_invoke=subprocess.run,
    profile_path: Path = PROFILE_V2_FILE,
) -> dict[str, Any]:
    """Build and archive local-only commands without spawning any process."""
    if len(repository_revision) != 40 or any(
        character not in "0123456789abcdef" for character in repository_revision
    ):
        raise AdapterError("repository revision must be one full lower-case Git SHA")
    frozen_profile = _require_v2_profile(profile, profile_path)
    main_config, replica_configs, manager_command, commands, artifacts = base.write_runtime_inputs(
        run_directory, frozen_profile, bls, tls, issuer,
        peer_port=peer_port, client_port=client_port, manager_port=manager_port,
        run_id=run_id, source_instances=source_instances, app_binary=app_binary,
        manager_binary=manager_binary, initial_tree_file=runner.TREE_FILE,
        manager_extra_args=(*base.FULL_RUN_MANAGER_EXTRA_ARGS, *V2_MANAGER_ARGS),
    )
    _enable_exact_timeout_attempt_evidence_v3(replica_configs)
    if "tree-switch-period = 2" not in main_config.read_text(encoding="utf-8"):
        raise AdapterError("base writer did not apply frozen v2 tree switch period")
    if manager_command.count("--required-nonresponsive") != 1 or manager_command[manager_command.index("--required-nonresponsive") + 1] != "1":
        raise AdapterError("base writer did not apply frozen v2 manager setting")
    copied_tree = run_directory / "config" / "epoch0.tree"
    helper_binary = (
        manager_binary.with_name("n7-epoch0-treefile-digest")
        if e0_helper_binary is None
        else e0_helper_binary
    )
    identity = _derive_e0_identity(
        run_directory, copied_tree, helper_binary, invoke=e0_helper_invoke
    )
    identity_path = run_directory / "runtime" / "e0-identity-receipt.json"
    base._write_json_exclusive(identity_path, identity)
    issuer_public_key_path = run_directory / "runtime" / "issuer-public-key.txt"
    issuer_public_key = issuer.get("pub")
    if not isinstance(issuer_public_key, str) or not issuer_public_key or "\n" in issuer_public_key:
        raise AdapterError("epoch issuer public key is not canonical single-line text")
    base._write_private(issuer_public_key_path, (issuer_public_key + "\n").encode("ascii"))
    preflight = runner.preflight(identity["epoch_digest"], copied_tree)
    if preflight["relay_omission"].get("total_omission_contexts") != 6:
        raise AdapterError("v2 preflight does not bind six omission contexts")
    overlay = tuple(preflight["relay_omission"]["argv_overlay"])
    replica_commands = tuple(
        tuple(command) + (overlay if replica_id == runner.OMITTING_REPLICA else ())
        for replica_id, command in enumerate(commands)
    )
    argv_path = run_directory / "runtime" / "launch-arguments.json"
    document = json.loads(argv_path.read_text(encoding="utf-8"))
    processes = document.get("processes")
    if not isinstance(processes, list) or len(processes) != 8:
        raise AdapterError("base writer launch-arguments schema changed")
    for process in processes:
        if process.get("source_id") == f"replica-{runner.OMITTING_REPLICA}":
            process["argv"] = list(replica_commands[runner.OMITTING_REPLICA])
            process["effective_options"]["experiment_three_reporter_omission"] = True
        elif isinstance(process.get("source_id"), str) and process["source_id"].startswith("replica-"):
            process["effective_options"]["experiment_three_reporter_omission"] = False
        if isinstance(process.get("source_id"), str) and process["source_id"].startswith("replica-"):
            replica_id = int(process["source_id"].removeprefix("replica-"))
            process["effective_options"]["replica_config_sha256"] = base.sha256_file(replica_configs[replica_id])
            process["effective_options"]["experiment_exact_timeout_attempt_evidence_v3"] = True
    base._replace_json(argv_path, document)
    artifacts = [
        base._runtime_artifact(run_directory, argv_path, kind="launch_arguments", replica_id=None)
        if artifact.get("path") == "runtime/launch-arguments.json" else artifact
        for artifact in artifacts
    ]
    artifacts.append(
        base._runtime_artifact(
            run_directory, identity_path, kind="e0_identity_receipt", replica_id=None
        )
    )
    artifacts.append(
        base._runtime_artifact(
            run_directory, issuer_public_key_path, kind="issuer_public_key", replica_id=None
        )
    )
    plan = {
        "schema_version": 1,
        "scenario": runner.SCENARIO,
        "repository_revision": repository_revision,
        "state": "PREPARED_E0_IDENTITY_DERIVED_EXECUTION_DISABLED",
        "claim_boundary": "no process launched; derived E0 identity is preflight-only; figure and thesis claim ineligible",
        "e0_identity_receipt": str(identity_path.relative_to(run_directory)),
        "e0_identity_receipt_sha256": base.sha256_file(identity_path),
        "e0_identity": identity,
        "issuer_public_key": str(issuer_public_key_path.relative_to(run_directory)),
        "issuer_public_key_sha256": base.sha256_file(issuer_public_key_path),
        "preflight": preflight,
        "main_config": str(main_config.relative_to(run_directory)),
        "main_config_sha256": base.sha256_file(main_config),
        "replica_configs": [str(path.relative_to(run_directory)) for path in replica_configs],
        "manager_command": list(manager_command),
        "replica_commands": [list(command) for command in replica_commands],
        "runtime_artifacts": artifacts,
    }
    plan["plan_sha256"] = _plan_digest(plan)
    base._write_json_exclusive(run_directory / "local-launch-plan.json", plan)
    return plan
