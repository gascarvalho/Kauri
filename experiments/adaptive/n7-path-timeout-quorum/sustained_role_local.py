"""Fail-closed, no-launch producer for the N=7 sustained-role study.

This is deliberately not a successor of ``run_local.py``.  The v4 producer
uses a post-baseline static aggregate gate, while this prospective study uses
native scheduled omission across configuration identities.  This module
therefore creates only a dry-run plan and exact authorization request.  It
cannot spawn a process, create an approval, or seal a successful result.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
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
    hard_timeout_seconds: int = 180,
    repository_snapshot: Callable[[Path], object] = base.verify_repository_state,
    native_mode_revision_check: Callable[[str], bool] = _native_mode_revision_present,
) -> dict[str, Any]:
    """Prepare immutable authorization bytes without launching any process.

    The scheduled window is intentionally supplied in the native RAW-clock
    domain before spawn.  A future execution gate must prove that the first
    physical omission and its 60-second common-commit horizon both lie inside
    this window; this dry-run does not infer either fact.
    """
    root = Path(output_root).resolve()
    if root.exists() or root.is_symlink():
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
    if ("--experiment-byzantine-mode" in manager or
            _one_option(manager, "--required-nonresponsive") != "1" or
            Path(_one_option(manager, "--epoch-zero-tree-file")).resolve()
            != Path(epoch0_tree).resolve()):
        raise SustainedRoleProducerError("manager command does not retain the exact N7 containment Epoch-0 binding")
    overlay = profile.argv_overlay(
        window_start_monotonic_ns=window_start_monotonic_ns,
        window_end_monotonic_ns=window_end_monotonic_ns,
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
    )
    profile_descriptor = _descriptor(HERE / "sustained_role_profile.py", "sustained-role profile")
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
        "profile": {"descriptor": profile_descriptor, "values": preflight},
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
            "effect_scope": "containment_epoch_package_vs_fixed_e0",
            "causal_boundary": "package_comparison_not_isolated_leaf_placement",
        },
        "evidence_contract": {
            "clock_domain": "CLOCK_MONOTONIC_RAW",
            "prearm_all_seven_e0_common_commit": "strictly_before_scheduled_window_start",
            "anchor": "first_admitted_native_e0_aggregate_omission",
            "common_horizon_ns": profile.COMMON_HORIZON_NS,
            "scheduled_window_must_cover": "anchor_plus_common_horizon",
            "minimum_post_start_anchor_slack_ns": profile.MINIMUM_POST_START_ANCHOR_SLACK_NS,
            "raw_stream_coverage": "all_sources_through_anchor_plus_common_horizon",
            "late_interval": {
                "start_after_anchor_ns": LATE_INTERVAL_START_NS,
                "end_after_anchor_ns": profile.COMMON_HORIZON_NS,
                "both_arms_require_physical_omission": True,
            },
            "adaptive_only": {
                "all_seven_e1_activation_deadline_after_anchor_ns": LATE_INTERVAL_START_NS,
                "actor_1_role": "wait_exempt_leaf_in_every_signed_e1_tree",
                "post_e1_physical_action": "direct_vote_omission",
            },
            "physical_marker_join": "one_to_one_structured_identity_to_KAURI_FAULT_log_marker",
            "future_receipt_descriptors": [
                "approved_authorization", "transition_request", "signed_e1_bundle",
                "approved_issuer_public_key", "manager_jsonl", "replica_jsonl_all_7",
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
    root.mkdir(mode=0o700)
    (root / "runtime").mkdir(mode=0o700)
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
