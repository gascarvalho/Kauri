"""Fail-closed, no-launch producer for the N=7 sustained-role study.

This is deliberately not a successor of ``run_local.py``.  The v4 producer
uses a post-baseline static aggregate gate, while this prospective study uses
native scheduled omission across configuration identities.  This module
therefore creates only a dry-run plan and exact authorization request.  It
cannot spawn a process, create an approval, or seal a successful result.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Callable, Mapping, Sequence


HERE = Path(__file__).resolve().parent
KAURI = HERE.parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


profile = _load("n7_sustained_role_profile", HERE / "sustained_role_profile.py")
tree_runner = _load("n7_sustained_role_tree", HERE / "runner.py")
base = _load("n7_sustained_role_base", KAURI / "experiments" / "adaptive" / "n7-crash-recovery" / "run.py")


PLAN = Path("runtime/sustained-role-execution-plan.json")
REQUEST = Path("runtime/sustained-role-authorization-request.json")
PLAN_KIND = "kauri-n7-sustained-role-execution-plan-v1"
REQUEST_KIND = "kauri-n7-sustained-role-execution-authorization-request-v1"
LATE_INTERVAL_START_NS = 20_000_000_000
_ARMS = frozenset({"fixed_e0", "adaptive_e1"})
NATIVE_MODE_INTRODUCTION_REVISION = "6f898f998f793534ca01f92130daf1a168d60b11"
PATH_TIMEOUT_SELECTION_PROFILE = HERE / "profile-v4.json"


class SustainedRoleProducerError(ValueError):
    """The isolated sustained-role dry-run inputs do not bind exactly."""


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value, allow_nan=False, ensure_ascii=True, sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\n"
    except (TypeError, ValueError) as exc:
        raise SustainedRoleProducerError("document is not canonical JSON") from exc


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha_file(path: Path, label: str) -> str:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise SustainedRoleProducerError(f"{label} is not a regular file")
    try:
        return hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError as exc:
        raise SustainedRoleProducerError(f"cannot read {label}") from exc


def _write_exclusive(path: Path, payload: bytes) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        raise SustainedRoleProducerError(f"cannot create {path.name}") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _read_canonical_local_authority_plan(path: Path) -> tuple[dict[str, Any], bytes]:
    """Read the local materializer's plan without accepting mutable JSON."""
    if path.is_symlink() or not path.is_file():
        raise SustainedRoleProducerError("local authority plan is not a regular file")
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SustainedRoleProducerError("local authority plan is not canonical JSON") from exc
    if not isinstance(value, dict) or raw != _canonical(value):
        raise SustainedRoleProducerError("local authority plan is not canonical JSON")
    return value, raw


def _argv_digest(command: Sequence[str]) -> str:
    if not command or any(not isinstance(value, str) or not value for value in command):
        raise SustainedRoleProducerError("command is not a nonempty string argv")
    return _sha(_canonical({"schema_version": 1, "argv": list(command)}))


def _one_option(command: Sequence[str], option: str) -> str:
    if command.count(option) != 1:
        raise SustainedRoleProducerError(f"command must contain exactly one {option}")
    position = command.index(option)
    if position + 1 >= len(command) or not command[position + 1]:
        raise SustainedRoleProducerError(f"command has no value for {option}")
    return command[position + 1]


def _hex64(value: str, label: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise SustainedRoleProducerError(f"{label} is not a lower-case SHA-256")
    return value


def _tree_roles(tree_file: Path) -> tuple[tuple[int, ...], ...]:
    trees = tree_runner.parse_tree_file(tree_file)
    internal = tuple(
        tree_id for tree_id, members in enumerate(trees)
        if 0 < members.index(profile.ACTOR_ID) < 3
    )
    if internal != (4, 5, 6):
        raise SustainedRoleProducerError(
            "actor 1 must begin as the exact internal relay in trees 4, 5, and 6"
        )
    return trees


def _descriptor(path: Path, label: str) -> dict[str, str]:
    return {"path": str(Path(path).resolve()), "sha256": _sha_file(path, label)}


def _require_clean_snapshot(snapshot: object) -> str:
    revision = getattr(snapshot, "revision", None)
    clean = getattr(snapshot, "worktree_clean", None)
    if (not isinstance(revision, str) or len(revision) != 40 or
            any(character not in "0123456789abcdef" for character in revision) or
            clean is not True):
        raise SustainedRoleProducerError("dry-run requires one clean pushed Kauri revision")
    return revision


def _native_mode_revision_present(revision: str) -> bool:
    """Require the pinned app revision to include the new native mode."""
    completed = subprocess.run(
        ("git", "merge-base", "--is-ancestor", NATIVE_MODE_INTRODUCTION_REVISION, revision),
        cwd=KAURI, check=False, capture_output=True,
    )
    return completed.returncode == 0


def _replica_config_binding(command: Sequence[str], *, main_config: Path,
                            replica_config: Path) -> None:
    positions = [index for index, value in enumerate(command) if value == "--conf"]
    if len(positions) != 2 or any(index + 1 >= len(command) for index in positions):
        raise SustainedRoleProducerError("replica command must contain exact main and replica config bindings")
    values = tuple(Path(command[index + 1]).resolve() for index in positions)
    if values != (Path(main_config).resolve(), Path(replica_config).resolve()):
        raise SustainedRoleProducerError("replica command config binding differs from declared config bytes")


def _config_option(path: Path, option: str, label: str) -> str:
    """Read one unambiguous ``key = value`` binding from generated config bytes."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SustainedRoleProducerError(f"cannot read {label}") from exc
    prefix = f"{option} = "
    values = [line[len(prefix):] for line in lines if line.startswith(prefix)]
    if len(values) != 1 or not values[0]:
        raise SustainedRoleProducerError(f"{label} must contain exactly one {option}")
    return values[0]


def _require_materialized_profile_bindings(
    *, arm: str, epoch0_tree: Path, main_config: Path,
    replica_configs: Sequence[Path], manager: Sequence[str], hard_timeout_seconds: int,
) -> None:
    """Check fields that the base materializer exposes in argv/config bytes.

    The checks deliberately stop at generated bytes.  They do not authorize a
    process or turn secret paths and raw-clock observations into evidence.
    Those fields remain explicitly marked as independently unverifiable in the
    frozen profile contract.
    """
    try:
        profile.validate_common_manager_argv(manager)
        if arm == "adaptive_e1":
            profile.validate_adaptive_manager_argv(manager)
        else:
            profile.validate_fixed_e0_manager_argv(manager)
        if arm == "adaptive_e1":
            profile.validate_path_timeout_manager_selection(manager)
    except profile.SustainedRoleProfileError as exc:
        raise SustainedRoleProducerError(str(exc)) from exc
    if arm == "fixed_e0" and any(option.startswith("--fault-window-arm-") for option in manager):
        raise SustainedRoleProducerError("fixed-E0 manager must not carry fault-window-arm arguments")
    if Path(_one_option(manager, "--epoch-zero-tree-file")).resolve() != epoch0_tree.resolve():
        raise SustainedRoleProducerError("manager epoch-zero tree differs from declared tree bytes")
    manager_run_id = _one_option(manager, "--structured-event-run-id")
    manager_source_instance = _one_option(manager, "--structured-event-source-instance")
    if manager_source_instance == "":
        raise SustainedRoleProducerError("manager source instance is empty")
    if arm == "adaptive_e1":
        if _one_option(manager, "--fault-window-arm-deadline-seconds") != str(hard_timeout_seconds):
            raise SustainedRoleProducerError("manager fault-window arm deadline differs from plan hard timeout")
        topology_sha256 = _hex64(
            _one_option(manager, "--fault-window-arm-topology-proof-sha256"),
            "manager fault-window arm topology proof",
        )
        if topology_sha256 != _sha_file(epoch0_tree, "Epoch-0 tree"):
            raise SustainedRoleProducerError("manager fault-window arm topology proof differs from declared tree bytes")
        _hex64(
            _one_option(manager, "--fault-window-arm-request-sha256"),
            "manager fault-window arm request",
        )
        _hex64(
            _one_option(manager, "--fault-window-arm-epoch-digest"),
            "manager fault-window arm Epoch-0 digest",
        )
    manager_output = Path(_one_option(manager, "--structured-event-output")).resolve()
    if manager_output.name != "adaptive-manager.jsonl":
        raise SustainedRoleProducerError("manager output does not use the canonical source filename")
    expected_main = {
        "block-size": str(profile.CONSENSUS_PROFILE["block_size"]),
        "fan-out": str(profile.CONSENSUS_PROFILE["fanout"]),
        "async_blocks": str(profile.CONSENSUS_PROFILE["pipeline_depth"]),
        "tree-switch-period": str(profile.CONSENSUS_PROFILE["tree_switch_period_blocks"]),
        "aggregation-timeout": str(profile.CONSENSUS_PROFILE["aggregation_timeout_s"]),
        "leader-progress-timeout": str(profile.CONSENSUS_PROFILE["leader_progress_timeout_s"]),
        "leader-activation-grace": str(profile.CONSENSUS_PROFILE["leader_activation_grace_s"]),
        "epoch-change-minimum-activation-delay": str(profile.CONSENSUS_PROFILE["activation_delay_blocks"]),
        "epoch-change-maximum-activation-delay": str(profile.CONSENSUS_PROFILE["activation_delay_blocks"]),
    }
    # Keep the explicit spelling close to the base writer: it prevents a
    # profile-only update from silently drifting a materialized config.
    expected_main["tree-switch-period"] = str(profile.CONSENSUS_PROFILE["tree_switch_period_blocks"])
    for option, value in expected_main.items():
        if _config_option(main_config, option, "main configuration") != value:
            raise SustainedRoleProducerError(f"main configuration {option} differs from frozen profile")
    if Path(_config_option(main_config, "tree-generation-fpath", "main configuration")).resolve() != epoch0_tree.resolve():
        raise SustainedRoleProducerError("main configuration tree differs from declared tree bytes")
    if _config_option(main_config, "tree-generation", "main configuration") != "file":
        raise SustainedRoleProducerError("main configuration does not use the frozen file tree")
    if _config_option(main_config, "epoch-change-issuer-id", "main configuration") != str(profile.ACTOR_ID):
        raise SustainedRoleProducerError("main configuration issuer differs from frozen issuer id")
    _config_option(main_config, "epoch-change-issuer-public-key", "main configuration")
    source_instances = {manager_source_instance}
    for replica, config in enumerate(replica_configs):
        if _config_option(config, "idx", f"replica-{replica} configuration") != str(replica):
            raise SustainedRoleProducerError("replica configuration index differs from its argv identity")
        if _config_option(config, "structured-event-run-id", f"replica-{replica} configuration") != manager_run_id:
            raise SustainedRoleProducerError("replica configuration run id differs from manager argv")
        source_instance = _config_option(
            config, "structured-event-source-instance", f"replica-{replica} configuration"
        )
        if source_instance == "":
            raise SustainedRoleProducerError("replica configuration source instance is empty")
        if source_instance in source_instances:
            raise SustainedRoleProducerError("structured event source instances must be globally distinct")
        source_instances.add(source_instance)
        output = Path(_config_option(config, "structured-event-output", f"replica-{replica} configuration")).resolve()
        if output.parent != manager_output.parent or output.name != f"replica-{replica}.jsonl":
            raise SustainedRoleProducerError("replica configuration output differs from canonical source path")


def prepare_dry_run(
    output_root: Path,
    *,
    arm: str,
    epoch0_tree: Path,
    main_config: Path,
    replica_configs: Sequence[Path],
    manager_command: Sequence[str],
    replica_commands: Sequence[Sequence[str]],
    window_start_monotonic_ns: int,
    window_end_monotonic_ns: int,
    first_omission_tree: int | None = profile.FIRST_OMISSION_TREE,
    hard_timeout_seconds: int = 180,
    repository_snapshot: Callable[[Path], object] = base.verify_repository_state,
    native_mode_revision_check: Callable[[str], bool] = _native_mode_revision_present,
    _existing_exclusive_root: bool = False,
) -> dict[str, Any]:
    """Prepare immutable authorization bytes without launching any process.

    The scheduled window is intentionally supplied in the native RAW-clock
    domain before spawn.  A future execution gate must prove that the first
    physical omission and its 60-second common-commit horizon both lie inside
    this window; this dry-run does not infer either fact.
    """
    root = Path(output_root).resolve()
    if _existing_exclusive_root:
        if root.is_symlink() or not root.is_dir():
            raise SustainedRoleProducerError("integrated materialization root is not an exclusive directory")
    elif root.exists() or root.is_symlink():
        raise SustainedRoleProducerError("dry-run output root must be absent")
    if arm not in _ARMS:
        raise SustainedRoleProducerError("dry-run arm must be fixed_e0 or adaptive_e1")
    if type(hard_timeout_seconds) is not int or hard_timeout_seconds < 120:
        raise SustainedRoleProducerError("hard timeout must retain startup and cleanup reserve")
    if len(replica_configs) != 7 or len(replica_commands) != 7:
        raise SustainedRoleProducerError("dry-run requires exactly seven replica configs and commands")
    revision = _require_clean_snapshot(repository_snapshot(KAURI))
    if not native_mode_revision_check(revision):
        raise SustainedRoleProducerError("pinned revision does not include the role-scoped native mode")
    trees = _tree_roles(epoch0_tree)
    tree_descriptor = _descriptor(epoch0_tree, "Epoch-0 tree")
    main_descriptor = _descriptor(main_config, "main configuration")
    config_descriptors = [
        _descriptor(config, f"replica-{replica} configuration")
        for replica, config in enumerate(replica_configs)
    ]
    if len({item["path"] for item in config_descriptors}) != 7:
        raise SustainedRoleProducerError("replica configuration paths are not distinct")
    manager = tuple(manager_command)
    if "--experiment-byzantine-mode" in manager:
        raise SustainedRoleProducerError("manager command must not carry a replica fault mode")
    _require_materialized_profile_bindings(
        arm=arm, epoch0_tree=Path(epoch0_tree), main_config=Path(main_config),
        replica_configs=replica_configs, manager=manager,
        hard_timeout_seconds=hard_timeout_seconds,
    )
    overlay = profile.argv_overlay(
        window_start_monotonic_ns=window_start_monotonic_ns,
        window_end_monotonic_ns=window_end_monotonic_ns,
        first_omission_tree=first_omission_tree,
    )
    forbidden = {
        "--experiment-omit-outbound-aggregate",
        "--experiment-omit-outbound-direct-vote",
        "--experiment-byzantine-configuration",
        "--experiment-omission-activation-gate-path",
    }
    final_replicas: list[tuple[str, ...]] = []
    for replica, command in enumerate(replica_commands):
        candidate = tuple(command)
        if any(option in candidate for option in forbidden):
            raise SustainedRoleProducerError("sustained-role command contains a v4/static omission option")
        if "--experiment-byzantine-mode" in candidate:
            raise SustainedRoleProducerError("base replica command already contains an experiment mode")
        if not candidate or Path(candidate[0]).is_symlink() or not Path(candidate[0]).is_file():
            raise SustainedRoleProducerError("replica command executable is not a regular file")
        _replica_config_binding(
            candidate, main_config=main_config, replica_config=replica_configs[replica],
        )
        final_replicas.append(candidate + (overlay if replica == profile.ACTOR_ID else ()))
    actor = final_replicas[profile.ACTOR_ID]
    if actor[-len(overlay):] != overlay or any(
        "--experiment-byzantine-mode" in command
        for replica, command in enumerate(final_replicas) if replica != profile.ACTOR_ID
    ):
        raise SustainedRoleProducerError("scheduled fault argv is not actor-only")
    if not manager or Path(manager[0]).is_symlink() or not Path(manager[0]).is_file():
        raise SustainedRoleProducerError("manager command executable is not a regular file")
    app_sha = _sha_file(Path(final_replicas[0][0]), "replica-0 executable")
    if any(_sha_file(Path(command[0]), f"replica-{replica} executable") != app_sha
           for replica, command in enumerate(final_replicas)):
        raise SustainedRoleProducerError("all replicas must pin one identical native app executable")
    preflight = profile.preflight(
        window_start_monotonic_ns=window_start_monotonic_ns,
        window_end_monotonic_ns=window_end_monotonic_ns,
        first_omission_tree=first_omission_tree,
    )
    profile_descriptor = _descriptor(HERE / "sustained_role_profile.py", "sustained-role profile")
    selection_profile_descriptor = _descriptor(
        PATH_TIMEOUT_SELECTION_PROFILE, "path-timeout selection profile"
    )
    if selection_profile_descriptor["sha256"] != profile.PATH_TIMEOUT_SELECTION_PROFILE_SHA256:
        raise SustainedRoleProducerError("path-timeout selection profile hash differs from frozen policy")
    plan = {
        "schema_version": 1,
        "kind": PLAN_KIND,
        "state": "PREPARED_DRY_RUN_EXTERNAL_APPROVAL_REQUIRED",
        "claim_boundary": (
            "No process launched. A future no-retry execution must preserve raw "
            "events, prove a first physical omission plus its full common horizon, "
            "and obtain independent validator acceptance."
        ),
        "repository_revision": revision,
        "native_mode_introduction_revision": NATIVE_MODE_INTRODUCTION_REVISION,
        "native_fault_schedule": {"descriptor": profile_descriptor, "values": preflight},
        "manager_selection_policy": {
            "descriptor": selection_profile_descriptor,
            "values": profile.CONSENSUS_PROFILE["selection_policy"],
        },
        "consensus_profile": profile.CONSENSUS_PROFILE,
        "materialization": {
            "source": "n7-crash-recovery/run.py:write_runtime_inputs",
            "field_by_field_map": profile.MATERIALIZATION_MAP,
            "state": "PREPARE_ONLY_NOT_EXECUTABLE",
        },
        "epoch0": {"tree": tree_descriptor, "trees": [list(tree) for tree in trees]},
        "configuration": {"main": main_descriptor, "replicas": config_descriptors},
        "commands": {
            "manager": {"argv": list(manager), "sha256": _argv_digest(manager),
                        "executable_sha256": _sha_file(Path(manager[0]), "manager executable")},
            "replicas": [
                {"replica_id": replica, "argv": list(command), "sha256": _argv_digest(command),
                 "executable_sha256": _sha_file(Path(command[0]), f"replica-{replica} executable")}
                for replica, command in enumerate(final_replicas)
            ],
        },
        "comparison": {
            "arm": arm,
            "paired_comparator": "adaptive_e1" if arm == "fixed_e0" else "fixed_e0",
            "effect_scope": "containment_epoch_package_vs_mixed_role_fixed_e0",
            "causal_boundary": "package_comparison_not_isolated_leaf_placement_or_aggregate_only_control",
        },
        "evidence_contract": {
            "clock_domain": "CLOCK_MONOTONIC_RAW",
            "prearm_all_seven_e0_common_commit": "strictly_before_scheduled_window_start",
            "anchor": "first_bijected_e0_internal_aggregate_omission",
            "fixed_e0_physical_actions": "mixed_root_forward_leaf_direct_vote_internal_aggregate",
            "common_horizon_ns": profile.COMMON_HORIZON_NS,
            "scheduled_window_must_cover": "anchor_plus_common_horizon",
            "minimum_post_start_anchor_slack_ns": profile.MINIMUM_POST_START_ANCHOR_SLACK_NS,
            "raw_stream_coverage": "all_seven_replica_streams_through_anchor_plus_common_horizon",
            "manager_coverage": "clean_arm_specific_terminal_plus_hashed_jsonl_log_and_cleanup_exit",
            "late_interval": {
                "start_after_anchor_ns": LATE_INTERVAL_START_NS,
                "end_after_anchor_ns": profile.COMMON_HORIZON_NS,
                "both_arms_require_physical_omission": True,
            },
            "adaptive_only": {
                "all_seven_e1_activation_deadline_after_anchor_ns": LATE_INTERVAL_START_NS,
                "actor_1_role": "wait_exempt_leaf_in_every_signed_e1_tree",
                "post_e1_physical_action": "direct_vote_omission",
                "selection_proof_required_before_receipt": "accepted_t4_t5_t6_timeouts_select_actor_1_and_match_snapshot_cutoff_signed_bundle",
            },
            "physical_marker_join": "one_to_one_structured_identity_to_KAURI_FAULT_log_marker",
            "future_receipt_descriptors": [
                "approved_authorization", "transition_request", "signed_e1_bundle",
                "approved_issuer_public_key", "manager_evidence_snapshot",
                "manager_fault_window_arm", "manager_jsonl", "replica_jsonl_all_7",
                "native_process_logs_all_8", "cleanup_receipt",
            ],
        },
        "receipt_contract": {
            "schema_version": 1,
            "kind": "kauri-n7-sustained-role-raw-bundle-receipt-v1",
            "state": "SEALED_RAW_BUNDLE_NO_CLAIM",
            "arm": arm,
            "anchor": {
                "source_id": "replica-1",
                "source_sequence": "native_event_sequence",
                "line_sha256": "native_jsonl_line_sha256",
                "monotonic_ns": "CLOCK_MONOTONIC_RAW",
            },
            "horizon": {
                "clock": "CLOCK_MONOTONIC_RAW",
                "duration_ns": profile.COMMON_HORIZON_NS,
                "late_offset_ns": LATE_INTERVAL_START_NS,
            },
        },
        "scheduled_window": {
            "start_monotonic_ns": window_start_monotonic_ns,
            "end_monotonic_ns": window_end_monotonic_ns,
            "argv_pinned_before_launch": True,
            "attestation": {
                "must_be_written": "after_prearm_all_seven_e0_common_commit_before_scheduled_start",
                "is_not": "an_arm_or_gate",
            },
        },
        "hard_timeout_seconds": hard_timeout_seconds,
        "no_retry": True,
    }
    plan["plan_sha256"] = _sha(_canonical(plan))
    plan_bytes = _canonical(plan)
    request = {
        "schema_version": 1,
        "kind": REQUEST_KIND,
        "execution_plan_sha256": plan["plan_sha256"],
        "repository_revision": revision,
        "arm": arm,
        "scheduled_window": plan["scheduled_window"],
        "hard_timeout_seconds": hard_timeout_seconds,
        "no_retry": True,
        "claim_eligible": False,
        "figure_eligible": False,
    }
    request_bytes = _canonical(request)
    if not _existing_exclusive_root:
        root.mkdir(mode=0o700)
        (root / "runtime").mkdir(mode=0o700)
    else:
        runtime = root / "runtime"
        if runtime.is_symlink():
            raise SustainedRoleProducerError("integrated materialization runtime path is a symlink")
        runtime.mkdir(mode=0o700, exist_ok=True)
    try:
        _write_exclusive(root / PLAN, plan_bytes)
        _write_exclusive(root / REQUEST, request_bytes)
    except Exception as exc:
        abort = {
            "schema_version": 1,
            "state": "PREPARATION_ABORTED_NO_EXECUTION",
            "detail": str(exc) or type(exc).__name__,
            "no_retry": True,
        }
        try:
            _write_exclusive(root / "runtime/sustained-role-preparation-abort.json", _canonical(abort))
        except Exception:
            pass
        raise
    return {
        "state": plan["state"],
        "execution_plan_sha256": plan["plan_sha256"],
        "authorization_request_sha256": _sha(request_bytes),
        "claim_boundary": plan["claim_boundary"],
    }


def _prepare_in_existing_exclusive_root(
    root: Path,
    **kwargs: Any,
) -> dict[str, Any]:
    """Private bridge for the one creator of a materialized run root."""
    return prepare_dry_run(root, _existing_exclusive_root=True, **kwargs)


def _base_materialization_profile() -> dict[str, Any]:
    """Translate only common W19 consensus fields for the inherited writer.

    ``write_runtime_inputs`` currently admits only its historical v4 profile
    identifier when it checks its generic throughput-window schema.  This is
    an adapter constraint, not an experiment profile substitution: the sealed
    W19 plan retains ``CONSENSUS_PROFILE`` and its descriptor.  No v4 static
    omission setting is imported here.
    """
    translated = dict(profile.CONSENSUS_PROFILE)
    translated["profile_id"] = base.N7_PATH_TIMEOUT_QUORUM_V4_PROFILE_ID
    translated["transition_requests"] = [dict(profile.CONSENSUS_PROFILE["transition"])]
    translated["throughput_windows"] = [
        {"phase": "baseline", "epoch_number": 0, "bucket_count": 1},
        {"phase": "degraded", "epoch_number": 0, "bucket_count": 1},
        {"phase": "containment", "epoch_number": 1, "bucket_count": 1},
    ]
    return translated


def _regular_binary(path: Path, label: str) -> Path:
    candidate = Path(path).resolve()
    if candidate.is_symlink() or not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise SustainedRoleProducerError(f"{label} is not an executable regular file")
    return candidate


def _archive_executable_for_materialization(root: Path, source: Path, name: str) -> Path:
    """Copy a launch-time executable below the exclusive root before hashing it."""
    destination = root / "materialization-binaries" / name
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise SustainedRoleProducerError("materialization executable archive already exists")
    try:
        with source.open("rb") as incoming, os.fdopen(
            os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o700), "wb"
        ) as outgoing:
            shutil.copyfileobj(incoming, outgoing)
            outgoing.flush(); os.fsync(outgoing.fileno())
        destination.chmod(0o700)
    except OSError as exc:
        raise SustainedRoleProducerError("cannot archive materialization executable") from exc
    if _sha_file(source, "materialization executable") != _sha_file(destination, "materialization executable"):
        raise SustainedRoleProducerError("materialization executable archive hash drift")
    return destination


def _selection_args(root: Path, *, run_id: str, epoch0_tree: Path,
                    epoch_digest: str, hard_timeout_seconds: int) -> tuple[str, ...]:
    request_sha = _sha(profile.canonical_transition_request().encode("utf-8"))
    return (
        "--required-nonresponsive", "1",
        "--fault-window-arm-path", str(root / "runtime/fault-window-arm.json"),
        "--fault-window-arm-schema-version", "4",
        "--fault-window-arm-domain", profile.PATH_TIMEOUT_ARM_DOMAIN,
        "--fault-window-arm-run-id", run_id,
        "--fault-window-arm-profile-id", profile.PATH_TIMEOUT_SELECTION_PROFILE_ID,
        "--fault-window-arm-profile-sha256", profile.PATH_TIMEOUT_SELECTION_PROFILE_SHA256,
        "--fault-window-arm-topology-proof-sha256", _sha_file(epoch0_tree, "Epoch-0 tree"),
        "--fault-window-arm-request-sha256", request_sha,
        "--fault-window-arm-epoch-number", "0",
        "--fault-window-arm-epoch-digest", epoch_digest,
        "--fault-window-arm-prefault-tree-id", "4",
        "--fault-window-arm-required-tree-positions", "3",
        "--fault-window-arm-deadline-seconds", str(hard_timeout_seconds),
        "--fault-window-arm-timeout-evidence-basis", "exact_timeout_attempt_id_v1",
        "--fault-window-arm-required-observation-schema", "3",
        "--fault-window-arm-clock-domain", "same_host_clock_monotonic_raw",
        "--fault-window-arm-snapshot-evidence-basis", "exact_post_fault_path_timeout_quorum_v1",
        "--fault-window-arm-selection-cardinality-policy", "all_guarded_up_to_fault_bound_v1",
    )


_EXACT_TIMEOUT_EVIDENCE_LINE = b"experiment-exact-timeout-attempt-evidence-v3 = true\n"


def _enable_exact_timeout_attempt_evidence(configs: Sequence[Path]) -> None:
    """Apply the existing N7 exact-timeout evidence schema to every replica."""
    if len(configs) != 7 or len(set(configs)) != 7:
        raise SustainedRoleProducerError("replica timeout-evidence configuration drifted")
    for path in configs:
        if path.is_symlink() or not path.is_file():
            raise SustainedRoleProducerError("replica timeout-evidence configuration drifted")
        payload = path.read_bytes()
        if not payload.endswith(b"\n") or _EXACT_TIMEOUT_EVIDENCE_LINE in payload:
            raise SustainedRoleProducerError("replica timeout-evidence configuration drifted")
        with path.open("wb") as stream:
            stream.write(payload + _EXACT_TIMEOUT_EVIDENCE_LINE)
            stream.flush(); os.fsync(stream.fileno())


def materialize_runtime_inputs(
    root: Path,
    *,
    arm: str,
    run_id: str,
    peer_port: int,
    client_port: int,
    manager_port: int,
    hard_timeout_seconds: int,
    app_binary: Path,
    manager_binary: Path,
    keygen_binary: Path,
    tls_keygen_binary: Path,
    e0_helper_binary: Path,
    materialization_profile: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """Materialize one W19 no-launch input set below an already-owned root.

    Key generators and the read-only E0 digest helper may execute; no Kauri
    replica, client, or adaptation manager is started here.  All keys,
    configs, argv, and authority bytes remain under ``root``.
    """
    root = Path(root).resolve()
    if root.is_symlink() or not root.is_dir():
        raise SustainedRoleProducerError("materialization root is not an owned directory")
    if arm not in _ARMS or not run_id or any(character.isspace() for character in run_id):
        raise SustainedRoleProducerError("materialization arm or run id is invalid")
    app = _regular_binary(app_binary, "hotstuff app")
    manager_binary = _regular_binary(manager_binary, "adaptation manager")
    keygen = _regular_binary(keygen_binary, "BLS key generator")
    tls_keygen = _regular_binary(tls_keygen_binary, "TLS key generator")
    helper = _regular_binary(e0_helper_binary, "E0 identity helper")
    for port in (peer_port, client_port, manager_port):
        if type(port) is not int or port <= 1024 or port > 65535:
            raise SustainedRoleProducerError("materialization ports are invalid")
    writer_profile = _base_materialization_profile()
    v8_materialization = materialization_profile is not None
    if v8_materialization:
        v8_profile = _load("n7_sustained_role_v8_profile", HERE / "sustained_role_v8_profile.py")
        try:
            v8_profile.validate_materialization_profile(materialization_profile)
        except v8_profile.V8ProfileError as exc:
            raise SustainedRoleProducerError(
                "materialization profile is not the exact frozen v8 profile") from exc
        writer_profile = dict(materialization_profile)
    config = root / "config"
    config.mkdir(mode=0o700)
    try:
        # The commands and E0 receipt must never point back to a mutable build
        # directory.  v7 retains its historic preparation-only generator
        # behavior byte-for-byte.  The v8 materialization profile additionally
        # snapshots both generators *before* invoking them, so the generated
        # identities are bound to the same in-root binary closure as the
        # launch-time programs.
        app = _archive_executable_for_materialization(root, app, "hotstuff-app")
        manager_binary = _archive_executable_for_materialization(root, manager_binary, "adaptation-manager")
        helper = _archive_executable_for_materialization(root, helper, "e0-identity-helper")
        if v8_materialization:
            keygen = _archive_executable_for_materialization(root, keygen, "hotstuff-keygen")
            tls_keygen = _archive_executable_for_materialization(
                root, tls_keygen, "hotstuff-tls-keygen")
        bls, tls, issuer = base.generate_identities(keygen, tls_keygen, config)
        source_instances = {"adaptive-manager": f"{run_id}-manager"}
        source_instances.update({f"replica-{replica}": f"{run_id}-replica-{replica}" for replica in range(7)})
        main, replicas, manager, commands, _artifacts = base.write_runtime_inputs(
            root, writer_profile, bls, tls, issuer,
            peer_port=peer_port, client_port=client_port, manager_port=manager_port,
            run_id=run_id, source_instances=source_instances, app_binary=app,
            manager_binary=manager_binary, manager_extra_args=(),
            initial_tree_file=tree_runner.TREE_FILE,
        )
        _enable_exact_timeout_attempt_evidence(replicas)
        epoch0_tree = root / "config/epoch0.tree"
        adapter = _load("w19_e0_identity_deriver", HERE / "local_adapter.py")
        identity = adapter._derive_e0_identity(root, epoch0_tree, helper)
        _write_exclusive(root / "runtime/e0-identity-receipt.json", _canonical(identity))
        _write_exclusive(root / "runtime/issuer-public-key.txt", (issuer["pub"] + "\n").encode("ascii"))
        adjusted = list(manager)
        if arm == "fixed_e0":
            for option in ("--transition-request", "--bundle-output"):
                position = adjusted.index(option)
                del adjusted[position:position + 2]
        if arm == "adaptive_e1":
            adjusted.extend(_selection_args(
                root, run_id=run_id, epoch0_tree=epoch0_tree,
                epoch_digest=identity["epoch_digest"], hard_timeout_seconds=hard_timeout_seconds,
            ))
        # Keep the inherited launch metadata coherent with the exact argv that
        # the new launcher will later replay, without treating it as authority.
        launch_arguments = root / "runtime/launch-arguments.json"
        document = json.loads(launch_arguments.read_text(encoding="utf-8"))
        for process in document.get("processes", []):
            if process.get("source_id") == "adaptive-manager":
                process["argv"] = base.normalized_manager_argv(tuple(adjusted))
        base._replace_json(launch_arguments, document)
        local_plan = {
            "schema_version": 1,
            "state": "PREPARED_E0_IDENTITY_DERIVED_EXECUTION_DISABLED",
            "e0_identity_receipt": "runtime/e0-identity-receipt.json",
            "e0_identity_receipt_sha256": _sha_file(root / "runtime/e0-identity-receipt.json", "E0 identity receipt"),
            "issuer_public_key": "runtime/issuer-public-key.txt",
            "issuer_public_key_sha256": _sha_file(root / "runtime/issuer-public-key.txt", "issuer public key"),
        }
        _write_exclusive(root / "local-launch-plan.json", _canonical(local_plan))
        return {
            "epoch0_tree": epoch0_tree, "main_config": main,
            "replica_configs": tuple(replicas), "manager_command": tuple(adjusted),
            "replica_commands": tuple(commands),
        }
    except Exception as exc:
        if isinstance(exc, SustainedRoleProducerError):
            raise
        raise SustainedRoleProducerError(f"runtime materialization failed: {exc}") from exc


def prepare_production_dry_run(
    output_root: Path,
    *, arm: str, run_id: str, window_start_monotonic_ns: int,
    window_end_monotonic_ns: int, peer_port: int, client_port: int,
    manager_port: int, app_binary: Path, manager_binary: Path,
    keygen_binary: Path, tls_keygen_binary: Path, e0_helper_binary: Path,
    hard_timeout_seconds: int = 180,
    repository_snapshot: Callable[[Path], object] = base.verify_repository_state,
    native_mode_revision_check: Callable[[str], bool] = _native_mode_revision_present,
) -> dict[str, Any]:
    """Production no-launch entrypoint; repository state remains fail-closed."""
    return prepare_materialized_dry_run(
        output_root, arm=arm,
        materialize=lambda root: materialize_runtime_inputs(
            root, arm=arm, run_id=run_id, peer_port=peer_port,
            client_port=client_port, manager_port=manager_port,
            hard_timeout_seconds=hard_timeout_seconds, app_binary=app_binary,
            manager_binary=manager_binary, keygen_binary=keygen_binary,
            tls_keygen_binary=tls_keygen_binary, e0_helper_binary=e0_helper_binary,
        ), window_start_monotonic_ns=window_start_monotonic_ns,
        window_end_monotonic_ns=window_end_monotonic_ns,
        hard_timeout_seconds=hard_timeout_seconds,
        repository_snapshot=repository_snapshot,
        native_mode_revision_check=native_mode_revision_check,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare one W19 no-launch runtime and authorization request")
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--arm", choices=sorted(_ARMS), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--window-start-monotonic-ns", type=int, required=True)
    parser.add_argument("--window-end-monotonic-ns", type=int, required=True)
    parser.add_argument("--peer-port", type=int, default=18000)
    parser.add_argument("--client-port", type=int, default=19000)
    parser.add_argument("--manager-port", type=int, default=20000)
    parser.add_argument("--hard-timeout-seconds", type=int, default=180)
    parser.add_argument("--app-binary", type=Path, default=KAURI / "build-adaptive/examples/hotstuff-app")
    parser.add_argument("--manager-binary", type=Path, default=KAURI / "build-adaptive/examples/adaptation-manager")
    parser.add_argument("--keygen-binary", type=Path, default=KAURI / "build-adaptive/hotstuff-keygen")
    parser.add_argument("--tls-keygen-binary", type=Path, default=KAURI / "build-adaptive/hotstuff-tls-keygen")
    parser.add_argument("--e0-helper-binary", type=Path, default=KAURI / "build-adaptive/examples/n7-epoch0-treefile-digest")
    args = parser.parse_args(argv)
    try:
        result = prepare_production_dry_run(
            args.run_root, arm=args.arm, run_id=args.run_id,
            window_start_monotonic_ns=args.window_start_monotonic_ns,
            window_end_monotonic_ns=args.window_end_monotonic_ns,
            peer_port=args.peer_port, client_port=args.client_port,
            manager_port=args.manager_port, app_binary=args.app_binary,
            manager_binary=args.manager_binary, keygen_binary=args.keygen_binary,
            tls_keygen_binary=args.tls_keygen_binary, e0_helper_binary=args.e0_helper_binary,
            hard_timeout_seconds=args.hard_timeout_seconds,
        )
    except (SustainedRoleProducerError, OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


def prepare_materialized_dry_run(
    output_root: Path,
    *,
    arm: str,
    materialize: Callable[[Path], Mapping[str, Any]],
    window_start_monotonic_ns: int,
    window_end_monotonic_ns: int,
    hard_timeout_seconds: int = 180,
    repository_snapshot: Callable[[Path], object] = base.verify_repository_state,
    native_mode_revision_check: Callable[[str], bool] = _native_mode_revision_present,
) -> dict[str, Any]:
    """Create one exclusive root, materialize inputs there, then seal its plan.

    The regular producer deliberately requires an absent root.  A real
    materializer needs that same root for its configs, E0 identity receipt,
    and issuer public-key archive.  This adapter reconciles those constraints
    without ever accepting a pre-existing directory: ownership is acquired
    with ``mkdir`` before the callback is invoked, and a failed callback gets
    an immutable no-execution abort record in that otherwise new root.

    ``materialize`` returns only paths/argv for bytes it wrote below ``root``.
    It is intentionally injected so this module does not revive the unrelated
    v4 local adapter or acquire credentials itself.
    """
    root = Path(output_root).resolve()
    if root.exists() or root.is_symlink():
        raise SustainedRoleProducerError("integrated materialization output root must be absent")
    try:
        root.mkdir(mode=0o700)
    except OSError as exc:
        raise SustainedRoleProducerError("cannot exclusively create materialization output root") from exc
    try:
        produced = materialize(root)
        if not isinstance(produced, Mapping):
            raise SustainedRoleProducerError("materializer did not return a mapping")
        required = {"epoch0_tree", "main_config", "replica_configs", "manager_command", "replica_commands"}
        if set(produced) != required:
            raise SustainedRoleProducerError("materializer return schema drift")
        def under_root(value: Path, label: str) -> Path:
            path = Path(value).resolve()
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise SustainedRoleProducerError(f"materialized {label} escapes exclusive root") from exc
            if path.is_symlink() or not path.is_file():
                raise SustainedRoleProducerError(f"materialized {label} is not a regular file")
            return path
        epoch0_tree = under_root(Path(produced["epoch0_tree"]), "Epoch-0 tree")
        main_config = under_root(Path(produced["main_config"]), "main configuration")
        replica_configs = tuple(under_root(Path(path), f"replica-{index} configuration")
                                for index, path in enumerate(produced["replica_configs"]))
        # The materializer must preserve the authority artifacts in this exact
        # root before a launch authorization can be requested.
        authority_plan = under_root(root / "local-launch-plan.json", "local authority plan")
        authority, authority_bytes = _read_canonical_local_authority_plan(authority_plan)
        if authority.get("state") != "PREPARED_E0_IDENTITY_DERIVED_EXECUTION_DISABLED":
            raise SustainedRoleProducerError("materializer authority plan is not execution-disabled")
        for key, label in (("e0_identity_receipt", "E0 identity receipt"),
                           ("issuer_public_key", "issuer public key")):
            relative = authority.get(key)
            if not isinstance(relative, str):
                raise SustainedRoleProducerError(f"materializer authority plan lacks {label}")
            artifact = under_root(root / relative, label)
            digest = authority.get(f"{key}_sha256")
            if not isinstance(digest, str) or _sha_file(artifact, label) != digest:
                raise SustainedRoleProducerError(f"materializer {label} hash drift")
        # The ordinary producer refuses an existing root.  Temporarily remove
        # only the just-created directory is unsafe and would break embedded
        # absolute argv paths, so seal its two documents directly below.
        return _prepare_in_existing_exclusive_root(
            root, arm=arm, epoch0_tree=epoch0_tree, main_config=main_config,
            replica_configs=replica_configs, manager_command=produced["manager_command"],
            replica_commands=produced["replica_commands"],
            window_start_monotonic_ns=window_start_monotonic_ns,
            window_end_monotonic_ns=window_end_monotonic_ns,
            hard_timeout_seconds=hard_timeout_seconds, repository_snapshot=repository_snapshot,
            native_mode_revision_check=native_mode_revision_check,
        )
    except Exception as exc:
        runtime = root / "runtime"
        runtime.mkdir(mode=0o700, exist_ok=True)
        abort = runtime / "sustained-role-materialization-abort.json"
        if not abort.exists() and not abort.is_symlink():
            _write_exclusive(abort, _canonical({
                "schema_version": 1, "state": "MATERIALIZATION_ABORTED_NO_EXECUTION",
                "detail": str(exc)[:512], "no_retry": True,
            }))
        if isinstance(exc, SustainedRoleProducerError):
            raise
        raise SustainedRoleProducerError(f"integrated materialization failed: {exc}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
