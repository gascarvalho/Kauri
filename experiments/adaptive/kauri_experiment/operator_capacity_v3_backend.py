"""No-launch backend plan for a prospective W18 all-live N31 arm.

This is intentionally a process *materializer/auditor*, not a launcher.  It
only accepts an immutable materializer output and makes all future process,
quota, Stage-B, and cleanup obligations explicit.  A caller cannot turn its
result into evidence or a live run through this module.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Mapping, Sequence

from .operator_capacity_preflight import _validate_quota_profile_bytes


N = 31
Q = 21
_HEX = frozenset("0123456789abcdef")
_ARMS = {"sham": "exact_copy_sham", "treatment": "fast_priority_treatment"}
_MANIFEST_KIND = "kauri-n31-operator-capacity-v3-materialization-v1"


class OperatorCapacityV3BackendError(RuntimeError):
    pass


def _fail(message: str) -> None:
    raise OperatorCapacityV3BackendError(message)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _hex(value: object, label: str, length: int = 64) -> str:
    if not isinstance(value, str) or len(value) != length or any(char not in _HEX for char in value):
        _fail(f"{label} is not lower-case hexadecimal")
    return value


def _read_regular(path: Path, label: str, maximum: int = 512 * 1024) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        _fail(f"{label} is not a readable regular file")
        raise AssertionError from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size < 0 or before.st_size > maximum:
            _fail(f"{label} is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                _fail(f"{label} changed during read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            _fail(f"{label} changed during read")
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            _fail(f"{label} changed during read")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _json(raw: bytes, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                _fail(f"{label} repeats a JSON field")
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"{label} is not strict ASCII JSON")
        raise AssertionError from exc
    if not isinstance(value, dict):
        _fail(f"{label} is not a JSON object")
    return value


def _value(argv: Sequence[str], option: str) -> str:
    positions = [index for index, value in enumerate(argv) if value == option]
    if len(positions) != 1 or positions[0] + 1 >= len(argv):
        _fail(f"manager argv lacks exactly one {option}")
    return argv[positions[0] + 1]


def _argv_digest(argv: Sequence[str]) -> str:
    if any(not isinstance(value, str) or not value for value in argv):
        _fail("argv contains an invalid value")
    return _sha(json.dumps(list(argv), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n")


def _under(root: Path, candidate: str, label: str) -> Path:
    path = Path(candidate)
    try:
        resolved = path.resolve(strict=False)
        resolved.relative_to(root.resolve())
    except ValueError:
        _fail(f"{label} escapes the exact materialization root")
    return resolved


def _transition_policy(manager_argv: Sequence[str]) -> str:
    raw = _value(manager_argv, "--transition-request")
    document = _json(raw.encode("ascii"), "manager transition request")
    if set(document) != {
        "policy_intent", "evidence_window_rule", "transition_artifact_id",
        "bundle_path", "evidence_snapshot_path", "predecessor_epoch_number",
        "successor_epoch_number", "minimum_predecessor_residency_ms",
        "minimum_post_baseline_observation_ms", "apply_shape_selection",
        "policy_parameters",
    }:
        _fail("manager transition request schema differs")
    if (document["evidence_window_rule"] != "fresh_exact_predecessor_after_common_commit" or
            document["predecessor_epoch_number"] != 0 or document["successor_epoch_number"] != 1 or
            document["policy_parameters"] != {}):
        _fail("manager transition request does not bind the all-live E0-to-E1 contract")
    policy = document["policy_intent"]
    if policy not in {"fault_containment", "performance_optimization"}:
        _fail("manager transition request policy is unsupported")
    return policy


def prepare_no_launch_backend(
    *, materialization_root: Path, manager_argv: Sequence[str],
    replica_argv: Sequence[Sequence[str]], quota_profile: Path,
) -> dict[str, object]:
    """Audit one frozen arm and return a non-executable process/cleanup plan.

    It writes nothing and starts nothing.  Native optimization-first admission
    is already checked by the manager, but no process runner exists here, so
    this remains a review-only plan rather than a launch authorization.
    """
    root = Path(materialization_root)
    if root.is_symlink() or not root.is_dir():
        _fail("materialization root is not a real directory")
    manifest_path = root / "materialization-manifest.json"
    manifest = _json(_read_regular(manifest_path, "materialization manifest"), "materialization manifest")
    required = {
        "schema_version", "kind", "verdict", "claim_eligible", "figure_eligible",
        "arm", "stage_a_native_arm", "protocol", "slow_root_ids", "revision",
        "epoch0_tree", "binary_sha256", "artifact_sha256", "manager_argv_sha256",
        "replica_argv_sha256", "stage_a_envelope_sha256",
        "stage_a_verifier_receipt_sha256", "identity_parity_receipt_sha256",
        "tool_identity_approval_receipt_sha256", "stage_a_verifier_arguments",
        "stage_b_authorization_output", "consumption_output", "bundle_output",
    }
    if set(manifest) != required:
        _fail("materialization manifest schema differs")
    if (manifest["schema_version"] != 1 or manifest["kind"] != _MANIFEST_KIND or
            manifest["verdict"] != "MATERIALIZED_NO_EXECUTION" or
            manifest["claim_eligible"] is not False or manifest["figure_eligible"] is not False or
            manifest["arm"] not in _ARMS or manifest["stage_a_native_arm"] != _ARMS[manifest["arm"]] or
            manifest["protocol"] != {"N": N, "Q": Q, "tree_count": Q} or
            manifest["slow_root_ids"] != list(range(6))):
        _fail("materialization manifest is not the frozen W18 no-launch shape")
    _hex(manifest["revision"], "materialization revision", 40)
    for field in (
        "stage_a_envelope_sha256", "stage_a_verifier_receipt_sha256",
        "identity_parity_receipt_sha256",
        "tool_identity_approval_receipt_sha256",
    ):
        _hex(manifest[field], field)
    if not isinstance(manifest["binary_sha256"], dict) or set(manifest["binary_sha256"]) != {
        "adaptation_manager", "hotstuff_app", "identity_parity_verifier",
    }:
        _fail("materialization binary map is invalid")
    for name, digest in manifest["binary_sha256"].items():
        _hex(digest, f"binary {name}")
    if not isinstance(manifest["artifact_sha256"], dict):
        _fail("materialization artifact map is invalid")
    expected_artifacts = {"config/epoch0.tree", "config/stage-a-envelope.wire", "config/hotstuff.gen.conf"} | {
        f"config/replica-{replica}.conf" for replica in range(N)
    }
    if set(manifest["artifact_sha256"]) != expected_artifacts:
        _fail("materialization artifact map does not cover exactly N31 configuration")
    for relative, digest in manifest["artifact_sha256"].items():
        _hex(digest, f"artifact {relative}")
        if _sha(_read_regular(root / relative, f"artifact {relative}")) != digest:
            _fail("materialization artifact digest differs")
    if manifest["artifact_sha256"]["config/epoch0.tree"] != manifest["epoch0_tree"]["sha256"]:
        _fail("materialized Epoch-0 tree artifact differs from the manifest binding")
    if _argv_digest(manager_argv) != manifest["manager_argv_sha256"]:
        _fail("manager argv differs from frozen materialization manifest")
    if not isinstance(manifest["replica_argv_sha256"], list) or len(replica_argv) != N or len(manifest["replica_argv_sha256"]) != N:
        _fail("replica argv set does not cover exactly N31")
    for replica, argv in enumerate(replica_argv):
        if _argv_digest(argv) != manifest["replica_argv_sha256"][replica]:
            _fail("replica argv differs from frozen materialization manifest")
        if "--experiment-byzantine-mode" in argv:
            _fail("all-live backend rejects Byzantine replica argv")
        if _value(argv, "--structured-event-commit-observer-id") != "replica-0":
            _fail("replica argv does not retain the designated native observer")
        event_path = _under(root, _value(argv, "--structured-event-output"), "replica event output")
        if event_path != root / "raw" / f"replica-{replica}.jsonl":
            _fail("replica event output is not the exact retained raw path")
    if _value(manager_argv, "--protocol-mode") != "adaptive_v3" or "--experiment-byzantine-mode" in manager_argv:
        _fail("manager argv is not all-live adaptive-v3")
    manager_event = _under(root, _value(manager_argv, "--structured-event-output"), "manager event output")
    epoch0_tree = _under(root, _value(manager_argv, "--epoch-zero-tree-file"), "manager Epoch-0 tree input")
    stage_a_envelope = _under(root, _value(manager_argv, "--operator-capacity-stage-a-envelope"), "manager Stage-A envelope input")
    stage_b = _under(root, _value(manager_argv, "--operator-capacity-stage-b-authorization-output"), "Stage-B authorization output")
    consumption = _under(root, _value(manager_argv, "--operator-capacity-consumption-output"), "consumption output")
    bundle = _under(root, _value(manager_argv, "--bundle-output"), "successor bundle output")
    expected_paths = {
        manager_event: root / "raw/manager-events.jsonl", stage_b: root / manifest["stage_b_authorization_output"],
        consumption: root / manifest["consumption_output"], bundle: root / manifest["bundle_output"],
    }
    if any(actual != expected for actual, expected in expected_paths.items()) or epoch0_tree != root / "config/epoch0.tree" or stage_a_envelope != root / "config/stage-a-envelope.wire":
        _fail("manager output path differs from the exact materialization manifest")
    if (_sha(_read_regular(epoch0_tree, "materialized Epoch-0 tree")) != manifest["epoch0_tree"]["sha256"] or
            _sha(_read_regular(stage_a_envelope, "materialized Stage-A envelope")) != manifest["stage_a_envelope_sha256"] or
            _value(manager_argv, "--operator-capacity-stage-a-wire-sha256") != manifest["stage_a_envelope_sha256"]):
        _fail("manager input differs from the exact materialized Stage-A/Epoch-0 binding")
    expected_verifier_arguments = [
        "--epoch0-tree-file", str(epoch0_tree), "--stage-a-envelope-wire", str(stage_a_envelope),
        "--issuer-id", _value(manager_argv, "--operator-capacity-label-issuer-id"),
        "--issuer-reference", _value(manager_argv, "--operator-capacity-label-issuer-reference"),
        "--issuer-public-key-hex", _value(manager_argv, "--operator-capacity-label-issuer-public-key-hex"),
        "--issuer-public-key-fingerprint", _value(manager_argv, "--operator-capacity-label-issuer-public-key-fingerprint"),
        "--approved-capacity-digest", _value(manager_argv, "--operator-capacity-approved-capacity-digest"),
        "--arm", _ARMS[manifest["arm"]], "--source-revision", manifest["revision"],
    ]
    if manifest["stage_a_verifier_arguments"] != expected_verifier_arguments:
        _fail("persisted native Stage-A verifier invocation differs from the executable plan")
    raw = root / "raw"
    transition = root / "transitions/e0-to-e1-operator-capacity"
    if not raw.is_dir() or not transition.is_dir() or any(raw.iterdir()) or any(transition.iterdir()):
        _fail("materialization raw and transition outputs must be fresh before backend planning")
    quota_raw = _read_regular(Path(quota_profile), "frozen CPU quota profile", 64 * 1024)
    try:
        _validate_quota_profile_bytes(quota_raw)
    except Exception as exc:
        _fail("quota profile differs from frozen W18 N31 ownership contract")
        raise AssertionError from exc
    policy = _transition_policy(manager_argv)
    if policy != "performance_optimization":
        _fail("W18 operator-capacity materialization requires optimization-first policy")
    return {
        "schema_version": 1,
        "kind": "kauri-n31-operator-capacity-v3-no-launch-backend-plan-v1",
        "verdict": "BACKEND_PLAN_REVIEW_REQUIRED_NO_EXECUTION",
        "claim_eligible": False, "figure_eligible": False, "launch_permitted": False,
        "materialization_manifest_sha256": _sha(_read_regular(manifest_path, "materialization manifest")),
        "arm": manifest["arm"], "revision": manifest["revision"], "automatic_retries": 0,
        "epoch0_tree": dict(manifest["epoch0_tree"]),
        "binary_sha256": dict(manifest["binary_sha256"]),
        "quota_ownership": {
            "replica_ids": list(range(N)), "launcher": "systemd-user-scope-cpu-quota-v1",
            "manager_visibility": "none", "scope_count": N,
        },
        "stage_a": {
            "envelope_sha256": manifest["stage_a_envelope_sha256"],
            "native_receipt_sha256": manifest["stage_a_verifier_receipt_sha256"],
            "identity_parity_receipt_sha256": manifest["identity_parity_receipt_sha256"],
            "tool_identity_approval_receipt_sha256":
                manifest["tool_identity_approval_receipt_sha256"],
            "verifier_arguments": list(manifest["stage_a_verifier_arguments"]),
        },
        "stage_b": {
            "authorization_output": str(stage_b.relative_to(root)),
            "consumption_output": str(consumption.relative_to(root)),
            "must_be_exclusively_written_before_successor_publication": True,
            "must_be_independently_verified_after_manager_terminal": True,
        },
        "cleanup_contract": {
            "required_order": ["stop_quota_monitor", "terminate_manager_and_replicas", "terminate_owned_replica_scopes", "verify_scope_cleanup"],
            "all_31_replica_scope_ownership_required": True,
            "manager_exit_zero_and_success_terminal_required": True,
            "raw_retention_required": True,
        },
        "native_policy_order_repaired": True,
        # This plan remains non-executable by itself.  A separate runner may
        # consume it only after it reopens the external authorization and the
        # complete Stage-A authority chain at the process-spawn edge.
        "execution_blocker": "EXTERNAL_AUTHORIZATION_AND_PRESPAWN_AUTHORITY_REQUIRED",
    }


def execution_not_implemented() -> None:
    """Hard stop: the plan is never a process-launch authorization."""
    _fail("W18 v3 backend execution is blocked: no process runner is implemented")
