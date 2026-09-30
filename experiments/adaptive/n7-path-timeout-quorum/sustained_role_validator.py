#!/usr/bin/env python3
"""Independent, fail-closed raw-bundle validator for the sustained N=7 study.

This module accepts neither producer summaries nor caller metrics.  It reopens
every descriptor named by the sealed receipt and is deliberately conservative:
an unsupported native event/bundle shape is a validation failure, not a
partial pass.  It never launches a process and never makes a thesis claim.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


KIND = "kauri-n7-sustained-role-raw-bundle-receipt-v1"
_ARMS = frozenset({"fixed_e0", "adaptive_e1"})
_HEX = frozenset("0123456789abcdef")
_MAX_RAW = 16 * 1024 * 1024
_MAX_SMALL = 256 * 1024
_HORIZON_NS = 60_000_000_000
_LATE_NS = 20_000_000_000

# This is deliberately an executable-interface checklist, not a guessed
# receipt schema.  The fixed-E0 launcher cannot produce any of these E1
# authorities.  An adaptive launcher must freeze all of them before this
# independent validator can enable an accepting adaptive branch.
ADAPTIVE_E1_REQUIRED_ARTIFACTS = (
    "canonical signed adaptive-v3 E1 bundle bytes and SHA-256",
    "canonical issuer public-key bytes and SHA-256",
    "independent native bundle decode/signature verification bound to that issuer",
    "decoded E1 predecessor E0 digest, successor E1 digest, and activation height",
    "decoded all-tree N=7 membership with actor 1 wait-exempt leaf in every tree",
    "all-seven epoch.command_committed raw-event bindings to the decoded E0-to-E1 command",
    "all-seven epoch.activated raw events for that exact E1 digest by anchor plus 20 seconds",
    "seven replica JSONL streams, eight logs, and clean eight-process cleanup through anchor plus 60 seconds",
)


class ValidationError(ValueError):
    """The raw bundle cannot support even a component-only verdict."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"


def _hex64(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in _HEX for char in value):
        raise ValidationError(f"{label} must be a lower-case SHA-256")
    return value


def _strict_object(raw: bytes, label: str) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValidationError(f"{label} has duplicate JSON key")
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label} is not a JSON object")
    return value


def _read_descriptor(root: Path, descriptor: object, label: str, limit: int) -> bytes:
    if not isinstance(descriptor, Mapping) or set(descriptor) != {"path", "sha256"}:
        raise ValidationError(f"{label} descriptor has schema drift")
    relative = descriptor["path"]
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValidationError(f"{label} descriptor path is not a safe relative child")
    digest = _hex64(descriptor["sha256"], f"{label} descriptor")
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ValidationError(f"{label} descriptor path escapes bundle root") from exc
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValidationError(f"{label} is not a bounded regular file")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValidationError(f"{label} SHA-256 differs from receipt")
    return raw


def _require_descriptor_list(artifacts: Mapping[str, Any], key: str, count: int) -> list[Mapping[str, str]]:
    value = artifacts.get(key)
    if not isinstance(value, list) or len(value) != count:
        raise ValidationError(f"receipt lacks exactly {count} {key}")
    return list(value)


def _config_option(raw: bytes, option: str, label: str) -> str:
    """Read one exact ``key = value`` entry from archived native config bytes.

    Replica source instances and the designated observer are consumed by the
    native application from configuration, not replica argv.  This deliberately
    small parser is only used for those immutable scalar bindings.
    """
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{label} is not UTF-8 configuration") from exc
    prefix = f"{option} = "
    values = [line[len(prefix):] for line in lines if line.startswith(prefix)]
    if len(values) != 1 or not values[0] or values[0].strip() != values[0]:
        raise ValidationError(f"{label} lacks one exact {option} binding")
    return values[0]


def _parse_jsonl(raw: bytes, *, run_id: str, source_id: str) -> list[dict[str, Any]]:
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{source_id} JSONL is not UTF-8") from exc
    if not lines:
        raise ValidationError(f"{source_id} JSONL is empty")
    events: list[dict[str, Any]] = []
    previous_sequence = 0
    previous_ns = -1
    source_instance: str | None = None
    expected_kind = "adaptation_manager" if source_id == "adaptive-manager" else "replica"
    for line in lines:
        event = _strict_object(line.encode("utf-8"), f"{source_id} JSONL event")
        required = {
            "event_schema_version", "run_id", "source_kind", "source_id",
            "source_instance", "source_sequence", "source_monotonic_ns", "event_type", "payload",
        }
        if set(event) != required or event["event_schema_version"] != 1 or event["run_id"] != run_id:
            raise ValidationError(f"{source_id} JSONL envelope drifted")
        if event["source_id"] != source_id or not isinstance(event["source_instance"], str) or not event["source_instance"]:
            raise ValidationError(f"{source_id} JSONL source identity drifted")
        if event["source_kind"] != expected_kind:
            raise ValidationError(f"{source_id} JSONL source kind drifted")
        if source_instance is None:
            source_instance = event["source_instance"]
        elif event["source_instance"] != source_instance:
            raise ValidationError(f"{source_id} JSONL mixes source instances")
        if type(event["source_sequence"]) is not int or event["source_sequence"] <= previous_sequence:
            raise ValidationError(f"{source_id} JSONL sequence is not strictly increasing")
        if type(event["source_monotonic_ns"]) is not int or event["source_monotonic_ns"] < previous_ns:
            raise ValidationError(f"{source_id} JSONL monotonic clock regressed")
        previous_sequence, previous_ns = event["source_sequence"], event["source_monotonic_ns"]
        event["_raw_line_sha256"] = hashlib.sha256(line.encode("utf-8")).hexdigest()
        events.append(event)
    return events


_MARKER = re.compile(r"(?:^|\s)KAURI_FAULT\s+(?P<body>[^\n]+)")


def _marker_fields(raw: bytes) -> list[dict[str, str]]:
    """Parse the native key=value marker grammar emitted by the C++ adapter."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("native process log is not UTF-8") from exc
    result: list[dict[str, str]] = []
    for match in _MARKER.finditer(text):
        fields: dict[str, str] = {}
        for token in match.group("body").split():
            if "=" not in token:
                break
            key, value = token.split("=", 1)
            if not key or not value or key in fields:
                raise ValidationError("native KAURI_FAULT marker is malformed")
            fields[key] = value
        if fields:
            result.append(fields)
    return result


def _opportunity_identity(event: Mapping[str, Any]) -> tuple[str, ...]:
    payload = event.get("payload")
    if not isinstance(payload, Mapping) or event.get("event_type") != "fault.contribution_opportunity":
        raise ValidationError("fault opportunity payload is invalid")
    proposal = payload.get("proposal")
    required = {
        "actor", "proposal", "physical_role", "parent_replica",
        "authenticated_proposal_source_replica", "expected_message_type", "cohort",
        "diagnostic_window", "window_start_monotonic_ns", "window_end_monotonic_ns",
        "decision_monotonic_ns", "contribution_ordinal", "role_contribution_ordinal",
        "scheduled_action", "responsive_omission_period", "fault_threshold",
        "hard_actor_count", "responsive_degraded_actor_count", "fault_mode", "view_generation",
    }
    if not isinstance(proposal, Mapping) or set(payload) != required or set(proposal) != {"epoch_number", "tree_id", "epoch_digest", "block_hash"}:
        raise ValidationError("fault opportunity schema drifted")
    if (payload["actor"] != 1 or payload["fault_mode"] != "role_scoped_persistent_selected_omission_v1" or
            payload["cohort"] != "hard" or payload["hard_actor_count"] != 1 or
            payload["responsive_degraded_actor_count"] != 0 or payload["fault_threshold"] != 2 or
            payload["responsive_omission_period"] != 0 or payload["contribution_ordinal"] != 0 or
            payload["role_contribution_ordinal"] != 0 or
            payload["authenticated_proposal_source_replica"] != payload["parent_replica"]):
        raise ValidationError("fault opportunity does not describe one authenticated hard actor")
    if payload["physical_role"] == "internal":
        expected = ("aggregate_relay", "omit_aggregate")
    elif payload["physical_role"] == "leaf":
        expected = ("direct_vote", "omit_direct_vote")
    else:
        raise ValidationError("fault opportunity has an unsupported physical role")
    if (payload["expected_message_type"], payload["scheduled_action"]) != expected:
        raise ValidationError("fault opportunity action differs from its physical role")
    if (type(payload["decision_monotonic_ns"]) is not int or
            payload["decision_monotonic_ns"] < payload["window_start_monotonic_ns"] or
            payload["decision_monotonic_ns"] >= payload["window_end_monotonic_ns"]):
        raise ValidationError("fault opportunity decision lies outside scheduled window")
    for label, value in (("proposal epoch digest", proposal["epoch_digest"]), ("proposal block hash", proposal["block_hash"])):
        _hex64(value, label)
    if type(proposal["epoch_number"]) is not int or proposal["epoch_number"] not in (0, 1) or type(proposal["tree_id"]) is not int or proposal["tree_id"] not in range(7):
        raise ValidationError("fault opportunity proposal identity is invalid")
    return tuple(str(value) for value in (
        payload["fault_mode"], proposal["epoch_number"], proposal["tree_id"], proposal["epoch_digest"],
        proposal["block_hash"], payload["diagnostic_window"], payload["window_start_monotonic_ns"],
        payload["window_end_monotonic_ns"], payload["actor"], payload["scheduled_action"],
        payload["decision_monotonic_ns"], payload["cohort"], payload["hard_actor_count"],
        payload["responsive_degraded_actor_count"], payload["fault_threshold"],
        payload["responsive_omission_period"], payload["contribution_ordinal"],
        payload["physical_role"], payload["role_contribution_ordinal"],
        payload["authenticated_proposal_source_replica"],
    ))


def _marker_identity(marker: Mapping[str, str]) -> tuple[str, ...]:
    keys = (
        "fault", "proposal_epoch", "proposal_tree", "proposal_epoch_digest", "proposal_block_hash",
        "window", "window_start_monotonic_ns", "window_end_monotonic_ns", "actor", "action",
        "monotonic_ns", "cohort", "hard_actor_count", "responsive_degraded_actor_count",
        "fault_threshold", "responsive_omission_period", "contribution_ordinal", "contribution_role",
        "role_contribution_ordinal", "authenticated_proposal_source_replica",
    )
    if any(key not in marker for key in keys):
        raise ValidationError("native KAURI_FAULT marker lacks sustained-role fields")
    if marker.get("max_omissions_per_proposal") != "1" or marker.get("fault_threshold") != "2":
        raise ValidationError("native KAURI_FAULT marker differs from frozen f=2 one-omission schedule")
    _hex64(marker["proposal_epoch_digest"], "native marker proposal epoch digest")
    _hex64(marker["proposal_block_hash"], "native marker proposal block hash")
    return tuple(marker[key] for key in keys)


def _check_receipt(receipt: Mapping[str, Any]) -> None:
    required = {"schema_version", "kind", "state", "arm", "run_id", "plan_sha256", "anchor", "horizon", "artifacts", "launch_binding", "receipt_sha256"}
    if set(receipt) != required or receipt.get("schema_version") != 1 or receipt.get("kind") != KIND:
        raise ValidationError("receipt schema drifted")
    if receipt.get("state") != "SEALED_RAW_BUNDLE_NO_CLAIM" or receipt.get("arm") not in _ARMS:
        raise ValidationError("receipt state or arm is invalid")
    if not isinstance(receipt.get("run_id"), str) or not receipt["run_id"]:
        raise ValidationError("receipt run id is invalid")
    _hex64(receipt.get("plan_sha256"), "receipt plan")
    semantic = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if receipt["receipt_sha256"] != hashlib.sha256(_canonical(semantic)).hexdigest():
        raise ValidationError("receipt SHA-256 does not recompute")
    if receipt.get("horizon") != {"clock": "CLOCK_MONOTONIC_RAW", "duration_ns": _HORIZON_NS, "late_offset_ns": _LATE_NS}:
        raise ValidationError("receipt horizon differs from frozen contract")
    anchor = receipt.get("anchor")
    if not isinstance(anchor, Mapping) or set(anchor) != {"source_id", "source_sequence", "line_sha256", "monotonic_ns"}:
        raise ValidationError("receipt anchor schema drifted")
    if anchor.get("source_id") != "replica-1" or type(anchor.get("source_sequence")) is not int or anchor["source_sequence"] <= 0 or type(anchor.get("monotonic_ns")) is not int or anchor["monotonic_ns"] <= 0:
        raise ValidationError("receipt anchor identity is invalid")
    _hex64(anchor.get("line_sha256"), "receipt anchor line")
    binding = receipt.get("launch_binding")
    expected_binding = {
        "request_sha256", "approval_sha256", "e0_digest", "scheduled_window",
        "manager_argv_sha256", "manager_executable_sha256", "replica_argv_sha256",
        "replica_executable_sha256", "native_profile_sha256", "selection_profile_sha256",
        "exit_codes",
    }
    if not isinstance(binding, Mapping) or set(binding) != expected_binding:
        raise ValidationError("receipt launch binding has schema drift")
    for key in ("request_sha256", "approval_sha256", "e0_digest", "manager_argv_sha256",
                "manager_executable_sha256", "replica_executable_sha256", "native_profile_sha256",
                "selection_profile_sha256"):
        _hex64(binding.get(key), f"receipt launch binding {key}")
    if (not isinstance(binding["replica_argv_sha256"], list) or len(binding["replica_argv_sha256"]) != 7 or
            any(_hex64(value, "receipt replica argv") != value for value in binding["replica_argv_sha256"])):
        raise ValidationError("receipt replica argv binding is invalid")
    window = binding["scheduled_window"]
    if (not isinstance(window, Mapping) or set(window) != {"start_monotonic_ns", "end_monotonic_ns"} or
            type(window["start_monotonic_ns"]) is not int or type(window["end_monotonic_ns"]) is not int or
            window["end_monotonic_ns"] - window["start_monotonic_ns"] < _HORIZON_NS):
        raise ValidationError("receipt scheduled window cannot cover the common horizon")
    exits = binding["exit_codes"]
    if (not isinstance(exits, Mapping) or set(exits) != {"adaptive-manager", *(f"replica-{i}" for i in range(7))} or
            any(type(code) is not int or code != 0 for code in exits.values())):
        raise ValidationError("receipt does not bind eight clean process exits")


def _canonical_object(raw: bytes, label: str) -> dict[str, Any]:
    value = _strict_object(raw, label)
    if raw != _canonical(value):
        raise ValidationError(f"{label} is not canonical JSON")
    return value


def _u16(value: int) -> bytes:
    return value.to_bytes(2, "big")


def _u32(value: int) -> bytes:
    return value.to_bytes(4, "big")


def _u64(value: int) -> bytes:
    return value.to_bytes(8, "big")


def _source_tree_tokens(line: bytes, line_number: int) -> list[bytes]:
    """Mirror the ASCII token grammar consumed by configuration.cpp.

    The sustained N=7 profile is deliberately ASCII-only.  Rejecting bytes
    outside this profile avoids a locale-dependent Python interpretation while
    remaining a strict subset of the native ``std::istringstream`` grammar.
    """
    if not line or any(byte > 0x7f for byte in line):
        raise ValidationError(f"Epoch-0 tree line {line_number} is not supported ASCII")
    tokens = line.split()
    if not tokens:
        raise ValidationError(f"Epoch-0 tree line {line_number} is empty")
    return tokens


def _source_decimal(token: bytes, label: str, line_number: int) -> int:
    if not token or any(byte < ord("0") or byte > ord("9") for byte in token):
        raise ValidationError(f"Epoch-0 tree line {line_number} has invalid {label}")
    value = int(token)
    if value > (1 << 64) - 1:
        raise ValidationError(f"Epoch-0 tree line {line_number} {label} overflows uint64")
    return value


def _source_option(token: bytes, prefix: bytes, label: str, line_number: int) -> int:
    if not token.startswith(prefix):
        raise ValidationError(f"Epoch-0 tree line {line_number} lacks {prefix.decode()}<value>")
    value = _source_decimal(token[len(prefix):], label, line_number)
    if not 1 <= value <= 255:
        raise ValidationError(f"Epoch-0 tree line {line_number} {label} is outside [1, 255]")
    return value


def _source_adaptive_v2_epoch_zero_digest(tree_raw: bytes) -> str:
    """Recompute the N=7 Epoch-0 digest from trusted source semantics.

    This is a local transliteration of ``configuration.cpp``'s
    ``parse_adaptive_v2_epoch_zero_tree_bytes``,
    ``adaptive_v2_epoch_zero_input``, and ``compute_epoch_digest``.  It
    consumes archived tree *data* only; it neither loads nor executes any
    receipt- or bundle-supplied code.
    """
    membership = tuple(range(7))
    trees: list[tuple[int, int, int, tuple[int, ...]]] = []
    # std::getline accepts a final unterminated line and does not create an
    # extra empty line after a trailing newline.
    lines = tree_raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    for line_number, line in enumerate(lines, start=1):
        if len(trees) >= len(membership):
            raise ValidationError("Epoch-0 tree has more than seven trees")
        fields = _source_tree_tokens(line, line_number)
        if len(fields) < 2:
            raise ValidationError(f"Epoch-0 tree line {line_number} lacks a header")
        fanout = _source_option(fields[0], b"fan:", "fanout", line_number)
        pipeline = _source_option(fields[1], b"pipe:", "pipeline stretch", line_number)
        members = tuple(_source_decimal(token, "replica id", line_number) for token in fields[2:])
        if any(member > 0xffff for member in members) or tuple(sorted(members)) != membership:
            raise ValidationError(f"Epoch-0 tree line {line_number} membership differs from replicas 0..6")
        trees.append((len(trees), fanout, pipeline, members))
    if not trees:
        raise ValidationError("Epoch-0 tree has no valid trees")

    membership_bytes = b"kauri-membership-v1" + _u32(len(membership)) + b"".join(
        _u16(member) for member in membership
    )
    membership_digest = hashlib.sha256(membership_bytes).digest()
    canonical = bytearray(b"kauri-epoch-definition-v2")
    canonical.extend(_u32(2))
    canonical.extend(_u32(0))
    canonical.extend(b"\0" * 32)
    canonical.extend(membership_digest)
    canonical.extend(_u64(0))
    for value in (b"adaptive-v2-bootstrap", b"adaptive-v2-bootstrap-epoch-zero"):
        canonical.extend(_u32(len(value)))
        canonical.extend(value)
    canonical.extend(_u64(0))
    canonical.extend(_u32(len(trees)))
    for tree_id, fanout, pipeline, members in trees:
        canonical.extend(_u32(tree_id))
        canonical.extend(_u32(fanout))
        canonical.extend(_u32(pipeline))
        canonical.extend(_u32(len(members)))
        canonical.extend(b"".join(_u16(member) for member in members))
        canonical.extend(_u32(0))  # adaptive-v2 wait_exempt_leaves
    return hashlib.sha256(canonical).hexdigest()


def _validate_source_derived_e0(root: Path, artifacts: Mapping[str, Any],
                                *, expected_digest: str) -> dict[str, Any]:
    """Bind Epoch-0 identity to an independent source-derived digest."""
    e0 = _canonical_object(_read_descriptor(root, artifacts["e0_identity_receipt"],
                                            "E0 identity receipt", _MAX_SMALL), "E0 identity receipt")
    required = {"schema_version", "state", "epoch_number", "epoch_digest", "tree_file",
                "tree_file_sha256", "helper_binary", "helper_binary_sha256", "argv"}
    if (set(e0) != required or e0["schema_version"] != 1 or
            e0["state"] != "DERIVED_READ_ONLY" or e0["epoch_number"] != 0 or
            _hex64(e0["epoch_digest"], "E0 identity digest") != expected_digest or
            not isinstance(e0["tree_file"], str) or not e0["tree_file"] or
            not isinstance(e0["helper_binary"], str) or not e0["helper_binary"] or
            not isinstance(e0["argv"], list) or len(e0["argv"]) != 2 or
            e0["argv"][0] != e0["helper_binary"] or not isinstance(e0["argv"][1], str)):
        raise ValidationError("source-derived E0 identity schema differs")
    tree_raw = _read_descriptor(root, artifacts["epoch0_tree"], "archived Epoch-0 tree", _MAX_SMALL)
    helper_raw = _read_descriptor(root, artifacts["e0_identity_helper"], "archived E0 helper", _MAX_RAW)
    if (hashlib.sha256(tree_raw).hexdigest() != e0["tree_file_sha256"] or
            hashlib.sha256(helper_raw).hexdigest() != _hex64(e0["helper_binary_sha256"], "E0 helper digest")):
        raise ValidationError("source-derived E0 tree or helper bytes differ")
    if _source_adaptive_v2_epoch_zero_digest(tree_raw) != expected_digest:
        raise ValidationError("source-derived E0 digest does not match archived tree")
    return e0


def _plan_argv_digest(argv: object, label: str) -> str:
    if not isinstance(argv, list) or not argv or any(not isinstance(value, str) or not value for value in argv):
        raise ValidationError(f"{label} argv is malformed")
    return hashlib.sha256(_canonical({"schema_version": 1, "argv": argv})).hexdigest()


def _descriptor_digest(value: object, label: str) -> str:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256"}:
        raise ValidationError(f"{label} descriptor is malformed")
    if not isinstance(value["path"], str) or not value["path"]:
        raise ValidationError(f"{label} descriptor path is malformed")
    return _hex64(value["sha256"], f"{label} descriptor")


def _plan_manager_argv(plan: Mapping[str, Any], *, e0_digest: str, arm: str) -> list[str]:
    commands = plan.get("commands")
    manager = commands.get("manager") if isinstance(commands, Mapping) else None
    argv = list(manager["argv"]) if isinstance(manager, Mapping) and isinstance(manager.get("argv"), list) else None
    _plan_argv_digest(argv, "manager")
    if arm == "adaptive_e1":
        return argv
    schedule = plan.get("scheduled_window")
    native = plan.get("native_fault_schedule")
    descriptor = native.get("descriptor") if isinstance(native, Mapping) else None
    expected_schedule = {"start_monotonic_ns", "end_monotonic_ns", "argv_pinned_before_launch", "attestation"}
    expected_attestation = {
        "must_be_written": "after_prearm_all_seven_e0_common_commit_before_scheduled_start",
        "is_not": "an_arm_or_gate",
    }
    if (not isinstance(schedule, Mapping) or set(schedule) != expected_schedule or
            type(schedule["start_monotonic_ns"]) is not int or
            type(schedule["end_monotonic_ns"]) is not int or
            schedule["argv_pinned_before_launch"] is not True or
            schedule["attestation"] != expected_attestation):
        raise ValidationError("fixed-E0 plan schedule is malformed")
    profile_sha = _descriptor_digest(descriptor, "native profile")
    if argv.count("--structured-event-run-id") != 1:
        raise ValidationError("fixed-E0 manager lacks one structured-event run ID")
    run_index = argv.index("--structured-event-run-id")
    if run_index + 1 >= len(argv) or not argv[run_index + 1]:
        raise ValidationError("fixed-E0 manager structured-event run ID is malformed")
    return argv + [
        "--scheduled-fixed-e0-control", "--scheduled-fixed-e0-run-id", argv[run_index + 1],
        "--scheduled-fixed-e0-profile-sha256", profile_sha,
        "--scheduled-fixed-e0-epoch-zero-digest", e0_digest,
        "--scheduled-fixed-e0-window-start-monotonic-ns", str(schedule["start_monotonic_ns"]),
        "--scheduled-fixed-e0-window-end-monotonic-ns", str(schedule["end_monotonic_ns"]),
    ]


def _validate_archived_launch_binding(plan: Mapping[str, Any], artifacts: Mapping[str, Any],
                                      *, receipt: Mapping[str, Any]) -> list[str]:
    """Recompute every receipt launch binding from sealed plan/artifact bytes."""
    binding = receipt["launch_binding"]
    commands = plan.get("commands")
    configuration = plan.get("configuration")
    epoch0 = plan.get("epoch0")
    native = plan.get("native_fault_schedule")
    selection = plan.get("manager_selection_policy")
    if not all(isinstance(value, Mapping) for value in (commands, configuration, epoch0, native, selection)):
        raise ValidationError("execution plan lacks archived launch bindings")
    manager = commands.get("manager")
    replicas = commands.get("replicas")
    if not isinstance(manager, Mapping) or not isinstance(replicas, list) or len(replicas) != 7:
        raise ValidationError("execution plan process bindings are malformed")
    manager_argv = _plan_manager_argv(plan, e0_digest=binding["e0_digest"], arm=receipt["arm"])
    if (_plan_argv_digest(manager.get("argv"), "manager") != _hex64(manager.get("sha256"), "manager argv") or
            _hex64(manager.get("executable_sha256"), "manager executable") !=
            _descriptor_digest(artifacts["executables"]["adaptation_manager"], "archived manager executable") or
            hashlib.sha256(_canonical({"argv": manager_argv})).hexdigest() != binding["manager_argv_sha256"] or
            manager["executable_sha256"] != binding["manager_executable_sha256"]):
        raise ValidationError("manager launch binding differs from archived plan or executable")
    replica_hashes: list[str] = []
    for replica, row in enumerate(replicas):
        if not isinstance(row, Mapping) or row.get("replica_id") != replica:
            raise ValidationError("replica launch binding is malformed")
        row_digest = _plan_argv_digest(row.get("argv"), f"replica-{replica}")
        if row_digest != _hex64(row.get("sha256"), f"replica-{replica} argv"):
            raise ValidationError(f"replica-{replica} argv differs from archived plan")
        if _hex64(row.get("executable_sha256"), f"replica-{replica} executable") != _descriptor_digest(
                artifacts["executables"]["hotstuff_app"], "archived replica executable"):
            raise ValidationError(f"replica-{replica} executable differs from archived artifact")
        replica_hashes.append(row_digest)
    if (replica_hashes != binding["replica_argv_sha256"] or
            any(row["executable_sha256"] != binding["replica_executable_sha256"] for row in replicas)):
        raise ValidationError("replica receipt launch binding differs from archived plan")
    expected_descriptors = (
        (epoch0.get("tree"), artifacts["epoch0_tree"], "Epoch-0 tree"),
        (configuration.get("main"), artifacts["main_config"], "main config"),
        (native.get("descriptor"), artifacts["profile"], "native profile"),
    )
    for planned, archived, label in expected_descriptors:
        if _descriptor_digest(planned, f"planned {label}") != _descriptor_digest(archived, f"archived {label}"):
            raise ValidationError(f"{label} differs from archived plan binding")
    config_rows = configuration.get("replicas")
    if not isinstance(config_rows, list) or len(config_rows) != 7:
        raise ValidationError("execution plan lacks seven replica config bindings")
    for replica, planned in enumerate(config_rows):
        if _descriptor_digest(planned, f"planned replica-{replica} config") != _descriptor_digest(
                artifacts["replica_configs"][replica], f"archived replica-{replica} config"):
            raise ValidationError(f"replica-{replica} config differs from archived plan binding")
    if (_descriptor_digest(native.get("descriptor"), "native profile") != binding["native_profile_sha256"] or
            _descriptor_digest(selection.get("descriptor"), "selection profile") != binding["selection_profile_sha256"]):
        raise ValidationError("profile launch binding differs from archived plan")
    return manager_argv


def _validate_commit_metrics(streams: Mapping[str, Sequence[Mapping[str, Any]]], *, anchor_ns: int,
                             e0_digest: str, arm: str, e1_digest: str | None = None,
                             designated_observer: int = 2) -> dict[str, int]:
    """Count common completion from one authority and seven raw witnesses."""
    if designated_observer not in range(7):
        raise ValidationError("designated commit observer is outside the N=7 membership")
    observer_source = f"replica-{designated_observer}"
    end_ns = anchor_ns + _HORIZON_NS
    authoritative: dict[tuple[int, str], tuple[Any, ...]] = {}
    observed: dict[tuple[int, str], dict[str, tuple[Any, ...]]] = {}
    for source, events in streams.items():
        prior_ns = -1
        for event in events:
            if event["source_monotonic_ns"] < prior_ns:
                raise ValidationError(f"{source} commit evidence clock regressed")
            prior_ns = event["source_monotonic_ns"]
            if not anchor_ns <= event["source_monotonic_ns"] < end_ns:
                continue
            if event["event_type"] not in {"block.committed", "block.commit_observed"}:
                continue
            payload = event["payload"]
            fields = ({"block_height", "block_hash", "parent_hash", "transaction_count", "designated_observer", "decision_proof", "view_generation", "commit_batch_index"}
                      if event["event_type"] == "block.committed" else {"block_height", "block_hash", "parent_hash", "transaction_count", "commit_batch_index"})
            if not isinstance(payload, Mapping) or set(payload) != fields:
                raise ValidationError("commit-derived metric payload has schema drift")
            if type(payload["block_height"]) is not int or payload["block_height"] <= 0:
                raise ValidationError("commit-derived metric block height is invalid")
            block_hash = _hex64(payload["block_hash"], "commit-derived block hash")
            parent = payload["parent_hash"]
            if parent is not None:
                _hex64(parent, "commit-derived parent hash")
            if type(payload["transaction_count"]) is not int or payload["transaction_count"] < 0 or type(payload["commit_batch_index"]) is not int or payload["commit_batch_index"] < 0:
                raise ValidationError("commit-derived metric counters are invalid")
            key = (payload["block_height"], block_hash)
            # Batch indices are local to each reporter, not a block identity.
            metadata = (parent, payload["transaction_count"])
            if event["event_type"] == "block.commit_observed":
                if source in observed.setdefault(key, {}):
                    raise ValidationError(f"{source} repeats commit-observed identity")
                observed[key][source] = metadata
                continue
            if type(payload["designated_observer"]) is not bool:
                raise ValidationError("commit-derived designated observer flag is invalid")
            proof = payload["decision_proof"]
            if (not isinstance(proof, Mapping) or set(proof) != {"epoch_number", "tree_id", "epoch_digest", "block_hash"} or
                    type(proof["epoch_number"]) is not int or type(proof["tree_id"]) is not int or proof["block_hash"] != block_hash):
                raise ValidationError("commit-derived metric decision proof drifted")
            if payload["designated_observer"] != (source == observer_source):
                raise ValidationError("designated-observer flag disagrees with archived replica configuration")
            if source != observer_source:
                continue
            if (proof["epoch_number"] not in (0, 1) or (proof["epoch_number"] == 0 and proof["epoch_digest"] != e0_digest) or
                    (proof["epoch_number"] == 1 and proof["epoch_digest"] != e1_digest) or
                    (arm == "fixed_e0" and proof["epoch_number"] != 0)):
                raise ValidationError("authoritative commit proof does not bind this arm")
            if key in authoritative:
                raise ValidationError(f"{observer_source} repeats authoritative commit identity")
            authoritative[key] = metadata
    common = {key for key, metadata in authoritative.items() if observed.get(key) == {source: metadata for source in streams}}
    if len(common) != len(authoritative):
        raise ValidationError("authoritative commits lack matching all-seven raw commit observations")
    return {"common_completed_commits": len(common)}


def _validate_prearm_e0_common_commit(
    streams: Mapping[str, Sequence[Mapping[str, Any]]], *, scheduled_start_ns: int,
    e0_digest: str, designated_observer: int,
) -> None:
    """Independently require one all-seven E0 commit before the fault window.

    A launcher attestation says that prearm happened; it does not establish
    that the replicated system had a common committed baseline.  This replay
    makes that baseline a raw-evidence requirement while intentionally leaving
    the post-anchor count free to be zero: zero is a measured negative outcome
    in the frozen campaign, not malformed evidence.
    """
    if designated_observer not in range(7):
        raise ValidationError("designated commit observer is outside the N=7 membership")
    observer_source = f"replica-{designated_observer}"
    for event in streams.get(observer_source, ()):
        if event.get("event_type") != "block.committed":
            continue
        if event.get("source_monotonic_ns", scheduled_start_ns) >= scheduled_start_ns:
            continue
        payload = event.get("payload")
        fields = {"block_height", "block_hash", "parent_hash", "transaction_count",
                  "designated_observer", "decision_proof", "view_generation", "commit_batch_index"}
        if not isinstance(payload, Mapping) or set(payload) != fields:
            raise ValidationError("prearm authoritative commit payload has schema drift")
        if payload.get("designated_observer") is not True:
            raise ValidationError("prearm authoritative commit is not designated")
        height = payload.get("block_height")
        block_hash = payload.get("block_hash")
        proof = payload.get("decision_proof")
        if (type(height) is not int or height <= 0 or
                not isinstance(block_hash, str) or
                not isinstance(proof, Mapping) or
                set(proof) != {"epoch_number", "tree_id", "epoch_digest", "block_hash"} or
                proof.get("epoch_number") != 0 or proof.get("epoch_digest") != e0_digest or
                proof.get("block_hash") != block_hash):
            raise ValidationError("prearm authoritative commit does not bind Epoch 0")
        metadata = (payload.get("parent_hash"), payload.get("transaction_count"))
        if type(metadata[1]) is not int or metadata[1] < 0:
            raise ValidationError("prearm authoritative commit counters are invalid")
        if metadata[0] is not None:
            _hex64(metadata[0], "prearm authoritative parent hash")
        _hex64(block_hash, "prearm authoritative block hash")
        witnesses: dict[str, tuple[Any, ...]] = {}
        for source, events in streams.items():
            for observed in events:
                if (observed.get("event_type") != "block.commit_observed" or
                        observed.get("source_monotonic_ns", scheduled_start_ns) >= scheduled_start_ns):
                    continue
                candidate = observed.get("payload")
                if (not isinstance(candidate, Mapping) or
                        set(candidate) != {"block_height", "block_hash", "parent_hash",
                                           "transaction_count", "commit_batch_index"} or
                        candidate.get("block_height") != height or
                        candidate.get("block_hash") != block_hash):
                    continue
                candidate_metadata = (candidate.get("parent_hash"), candidate.get("transaction_count"))
                if candidate_metadata != metadata:
                    raise ValidationError("prearm commit witness metadata differs from authority")
                if source in witnesses:
                    raise ValidationError(f"{source} repeats prearm commit observation")
                witnesses[source] = candidate_metadata
        if witnesses == {source: metadata for source in streams}:
            return
    raise ValidationError("no authoritative all-seven Epoch-0 commit precedes scheduled fault start")


def _validate_fixed_e0_manager(events: Sequence[Mapping[str, Any]], *, receipt: Mapping[str, Any], anchor_ns: int) -> None:
    binding = receipt["launch_binding"]
    window = binding["scheduled_window"]
    payload_expected = {"run_id", "profile_sha256", "epoch_zero_digest", "window_start_monotonic_ns", "window_end_monotonic_ns"}
    observations: list[Mapping[str, Any]] = []
    terminal: Mapping[str, Any] | None = None
    for event in events:
        if event["event_type"] not in {"scheduled_fixed_e0_control.observation", "scheduled_fixed_e0_control.terminal"}:
            continue
        payload = event["payload"]
        if (not isinstance(payload, Mapping) or set(payload) != payload_expected or payload["run_id"] != receipt["run_id"] or
                payload["profile_sha256"] != binding["native_profile_sha256"] or payload["epoch_zero_digest"] != binding["e0_digest"] or
                payload["window_start_monotonic_ns"] != window["start_monotonic_ns"] or payload["window_end_monotonic_ns"] != window["end_monotonic_ns"]):
            raise ValidationError("scheduled fixed-E0 manager evidence is not bound to this receipt")
        if event["event_type"] == "scheduled_fixed_e0_control.observation":
            if terminal is not None:
                raise ValidationError("scheduled fixed-E0 observation follows terminal")
            observations.append(event)
        elif terminal is not None:
            raise ValidationError("scheduled fixed-E0 terminal is duplicated")
        else:
            terminal = event
    if not observations or terminal is None:
        raise ValidationError("fixed-E0 receipt lacks scheduled observation or no-successor terminal")
    if (observations[0]["source_monotonic_ns"] < window["start_monotonic_ns"] or
            observations[0]["source_monotonic_ns"] > anchor_ns + _LATE_NS or
            observations[-1]["source_monotonic_ns"] < anchor_ns + _LATE_NS or
            terminal["source_monotonic_ns"] < anchor_ns + _HORIZON_NS):
        raise ValidationError("fixed-E0 manager evidence does not span the 60-second observation horizon")


def _validate_adaptive_activation(streams: Mapping[str, Sequence[Mapping[str, Any]]], *, anchor_ns: int) -> None:
    """Require one exact E1 activation per replica by the frozen +20s deadline."""
    deadline = anchor_ns + _LATE_NS
    identity: tuple[int, str, int] | None = None
    for replica in range(7):
        matches = []
        for event in streams[f"replica-{replica}"]:
            if event["event_type"] != "epoch.activated":
                continue
            payload = event["payload"]
            expected = {"epoch_number", "tree_id", "epoch_digest", "activation_height"}
            if (not isinstance(payload, Mapping) or set(payload) != expected or
                    payload["epoch_number"] != 1 or type(payload["tree_id"]) is not int or
                    payload["tree_id"] not in range(7) or type(payload["activation_height"]) is not int or
                    event["source_monotonic_ns"] > deadline):
                raise ValidationError("E1 activation event has invalid schema or misses +20s deadline")
            matches.append((payload["epoch_number"], _hex64(payload["epoch_digest"], "E1 activation digest"), payload["activation_height"]))
        if len(matches) != 1:
            raise ValidationError("adaptive receipt lacks exactly one timely E1 activation per replica")
        if identity is None:
            identity = matches[0]
        elif matches[0] != identity:
            raise ValidationError("all-seven E1 activations do not bind one exact successor")


def _e1_command_identity(payload: object, *, bundle: Any, e0_digest: str) -> tuple[int, str, str, int, str, int, str, int, int] | None:
    """Return the complete committed-E1 identity, never a successor-only key."""
    fields = {"command_block_height", "command_block_hash", "payload_digest", "predecessor_epoch_number", "predecessor_epoch_digest", "successor_epoch_number", "successor_epoch_digest", "activation_delay_blocks", "activation_height"}
    if not isinstance(payload, Mapping) or set(payload) != fields:
        raise ValidationError("E1 command event schema drifted")
    if not (payload["predecessor_epoch_number"] == 0 and
            payload["predecessor_epoch_digest"] == e0_digest and
            payload["successor_epoch_number"] == 1 and
            payload["successor_epoch_digest"] == bundle.epoch_digest and
            payload["payload_digest"] == bundle.command.payload_digest and
            payload["activation_delay_blocks"] == bundle.command.activation_delay_blocks):
        return None
    if (type(payload["command_block_height"]) is not int or
            payload["command_block_height"] <= 0 or
            type(payload["activation_height"]) is not int or
            payload["activation_height"] != payload["command_block_height"] + payload["activation_delay_blocks"]):
        raise ValidationError("E1 command height or activation delay is invalid")
    return (
        payload["command_block_height"], _hex64(payload["command_block_hash"], "E1 command block hash"),
        payload["payload_digest"], payload["predecessor_epoch_number"], payload["predecessor_epoch_digest"],
        payload["successor_epoch_number"], payload["successor_epoch_digest"], payload["activation_delay_blocks"],
        payload["activation_height"],
    )


def _factorial_bundle_decoder():
    """Load the established independent native-wire decoder, never a launcher."""
    package_root = str(Path(__file__).resolve().parents[1])
    inserted = package_root not in sys.path
    if inserted:
        sys.path.insert(0, package_root)
    try:
        return importlib.import_module("kauri_experiment.factorial_validation")
    except ImportError as exc:
        raise ValidationError("independent adaptive-v2 bundle decoder is unavailable") from exc
    finally:
        if inserted:
            sys.path.remove(package_root)


def _validate_adaptive_bundle_and_activation(root: Path, artifacts: Mapping[str, Any], *, receipt: Mapping[str, Any], streams: Mapping[str, Sequence[Mapping[str, Any]]], anchor_ns: int) -> Any:
    """Verify native signed E1 bytes before joining them to raw activation."""
    issuer_raw = _read_descriptor(root, artifacts["issuer_public_key"], "E1 issuer public key", _MAX_SMALL)
    try:
        issuer = issuer_raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValidationError("E1 issuer public key is not ASCII") from exc
    if not re.fullmatch(r"[0-9a-f]{66}\n", issuer):
        raise ValidationError("E1 issuer public key is not canonical compressed secp256k1 text")
    bundle_raw = _read_descriptor(root, artifacts["e1_bundle"], "signed E1 bundle", _MAX_SMALL)
    try:
        bundle = _factorial_bundle_decoder().decode_adaptive_v3_epoch_change_bundle(bundle_raw, issuer_public_key=issuer[:-1])
    except (ValueError, OSError, ImportError) as exc:
        raise ValidationError(f"signed E1 bundle does not independently verify: {exc}") from exc
    e0 = receipt["launch_binding"]["e0_digest"]
    if bundle.command.predecessor_epoch_digest != e0 or bundle.previous_epoch_digest != e0 or bundle.epoch_number != 1 or bundle.command.successor_epoch_number != 1 or bundle.epoch_digest != bundle.command.successor_epoch_digest:
        raise ValidationError("signed E1 bundle does not bind the exact E0 predecessor")
    if (len(bundle.trees) != 7 or tuple(tree.tree_id for tree in bundle.trees) != tuple(range(7)) or
            bundle.generation_seed != 41719 or bundle.command.activation_delay_blocks != 5):
        raise ValidationError("signed E1 bundle differs from frozen seven-tree generation")
    for tree in bundle.trees:
        first_leaf = (len(tree.members) - 2) // tree.fanout + 1
        if (tree.fanout != 2 or tree.pipeline_stretch != 2 or
                tuple(sorted(tree.members)) != tuple(range(7)) or
                tuple(tree.wait_exempt) != (1,) or tree.members.index(1) < first_leaf):
            raise ValidationError("signed E1 bundle does not place actor 1 as the wait-exempt leaf in every tree")
    deadline = anchor_ns + _LATE_NS
    all_seven_command_identity: tuple[int, str, str, int, str, int, str, int, int] | None = None
    all_seven_activation_identity: tuple[str, int] | None = None
    for replica in range(7):
        commands = []
        activations = []
        for event in streams[f"replica-{replica}"]:
            payload = event["payload"]
            if event["event_type"] == "epoch.command_committed":
                if _e1_command_identity(payload, bundle=bundle, e0_digest=e0) is not None:
                    commands.append(event)
            elif event["event_type"] == "epoch.activated" and commands:
                fields = {"epoch_number", "tree_id", "epoch_digest", "activation_height"}
                if (isinstance(payload, Mapping) and set(payload) == fields and
                        payload.get("epoch_number") == 1 and payload.get("epoch_digest") == bundle.epoch_digest and
                        type(payload.get("tree_id")) is int and payload["tree_id"] in {tree.tree_id for tree in bundle.trees} and
                        payload.get("activation_height") == commands[0]["payload"]["activation_height"]):
                    activations.append(event)
        if len(commands) != 1 or len(activations) != 1:
            raise ValidationError("each replica must bind exactly one E1 command and activation")
        command_identity = _e1_command_identity(commands[0]["payload"], bundle=bundle, e0_digest=e0)
        assert command_identity is not None
        if all_seven_command_identity is None:
            all_seven_command_identity = command_identity
        elif command_identity != all_seven_command_identity:
            raise ValidationError("all-seven E1 commands do not bind one exact committed identity")
        if not (commands[0]["source_sequence"] < activations[0]["source_sequence"] and activations[0]["source_monotonic_ns"] <= deadline):
            raise ValidationError("E1 activation is not after its command or misses anchor-plus-20 seconds")
        activation = activations[0]["payload"]
        identity = (activation["epoch_digest"], activation["activation_height"])
        if all_seven_activation_identity is None:
            all_seven_activation_identity = identity
        elif identity != all_seven_activation_identity:
            raise ValidationError("all-seven E1 activation envelopes do not bind one exact native identity")
    return bundle


def _validate_adaptive_causality(root: Path, artifacts: Mapping[str, Any], *,
                                 receipt: Mapping[str, Any],
                                 manager_events: Sequence[Mapping[str, Any]],
                                 actor_events: Sequence[Mapping[str, Any]],
                                 bundle: Any) -> None:
    """Join scheduled physical omissions to the v3 manager's accepted prefix.

    The scheduled fault is not controlled by the legacy static injection gate.
    Its native contribution-opportunity event and paired fault log prove the
    physical action; the manager arm and accepted observations prove which
    subsequent timeout evidence the adaptive controller consumed.
    """
    arm_raw = _read_descriptor(root, artifacts["manager_fault_window_arm"],
                               "manager fault-window arm", _MAX_SMALL)
    arm = _canonical_object(arm_raw, "manager fault-window arm")
    arm_fields = {"schema_version", "kind", "run_id", "profile_id", "profile_sha256",
                  "topology_proof_sha256", "request_sha256", "epoch_number", "epoch_digest",
                  "fault_receipt_sha256", "evidence_start_monotonic_ns", "prefault_tree_id",
                  "required_tree_positions", "required_tree_ids", "clock_domain",
                  "required_observation_schema", "timeout_evidence_basis",
                  "snapshot_evidence_basis", "selection_cardinality_policy"}
    transition_raw = _read_descriptor(root, artifacts["transition_request"],
                                      "transition request", _MAX_SMALL)
    binding = receipt["launch_binding"]
    if (set(arm) != arm_fields or arm.get("schema_version") != 4 or
            arm.get("kind") != "kauri-focused-fault-window-arm-v4" or
            arm.get("run_id") != receipt["run_id"] or
            arm.get("profile_id") != "n7-path-local-timeout-quorum-v4" or
            arm.get("profile_sha256") != binding["selection_profile_sha256"] or
            arm.get("topology_proof_sha256") != artifacts["epoch0_tree"]["sha256"] or
            arm.get("request_sha256") != hashlib.sha256(transition_raw.removesuffix(b"\n")).hexdigest() or
            arm.get("fault_receipt_sha256") != binding["approval_sha256"] or
            arm.get("epoch_number") != 0 or arm.get("epoch_digest") != binding["e0_digest"] or
            type(arm.get("evidence_start_monotonic_ns")) is not int or
            arm["evidence_start_monotonic_ns"] <= 0 or
            arm.get("prefault_tree_id") != 4 or arm.get("required_tree_positions") != 3 or
            arm.get("required_tree_ids") != [4, 5, 6] or
            arm.get("clock_domain") != "same_host_clock_monotonic_raw" or
            arm.get("required_observation_schema") != 3 or
            arm.get("timeout_evidence_basis") != "exact_timeout_attempt_id_v1" or
            arm.get("snapshot_evidence_basis") != "exact_post_fault_path_timeout_quorum_v1" or
            arm.get("selection_cardinality_policy") != "all_guarded_up_to_fault_bound_v1"):
        raise ValidationError("native fault-window arm differs from frozen W19 evidence contract")
    armed = [event for event in manager_events if event["event_type"] == "fault_window_armed"]
    expected_arm_payload = {**arm, "fault_window_arm_sha256": hashlib.sha256(arm_raw).hexdigest()}
    if len(armed) != 1 or armed[0]["payload"] != expected_arm_payload:
        raise ValidationError("manager did not emit the exact archived fault-window arm")
    arm_event = armed[0]
    anchor_ns = receipt["anchor"]["monotonic_ns"]
    if not (arm_event["source_monotonic_ns"] < anchor_ns and
            arm["evidence_start_monotonic_ns"] <= anchor_ns):
        raise ValidationError("manager fault-window arm does not precede physical omission")

    physical: dict[tuple[int, int, str], Mapping[str, Any]] = {}
    for event in actor_events:
        if event["event_type"] != "fault.contribution_opportunity":
            continue
        payload = event["payload"]
        proposal = payload.get("proposal") if isinstance(payload, Mapping) else None
        if (not isinstance(proposal, Mapping) or payload.get("actor") != 1 or
                payload.get("physical_role") != "internal" or
                payload.get("scheduled_action") != "omit_aggregate" or
                proposal.get("epoch_number") != 0 or
                proposal.get("epoch_digest") != binding["e0_digest"]):
            continue
        key = (proposal.get("tree_id"), payload.get("parent_replica"),
               proposal.get("block_hash"))
        if key in physical:
            raise ValidationError("native physical omission context is duplicated")
        physical[key] = event

    snapshot_events = [event for event in manager_events
                       if event["event_type"] == "adaptive_v2_evidence_snapshot"]
    snapshot_raw = _read_descriptor(root, artifacts["manager_evidence_snapshot"],
                                    "manager evidence snapshot", _MAX_SMALL)
    snapshot = _strict_object(snapshot_raw, "manager evidence snapshot")
    if len(snapshot_events) != 1 or snapshot_events[0]["payload"] != snapshot:
        raise ValidationError("sealed manager snapshot differs from its native event")
    snapshot_event = snapshot_events[0]
    if (snapshot.get("schema_version") != 2 or snapshot.get("policy_intent") != "fault_containment" or
            snapshot.get("transition_artifact_id") != "e0-to-e1-containment" or
            snapshot.get("predecessor_epoch_number") != 0 or
            snapshot.get("predecessor_epoch_digest") != binding["e0_digest"] or
            type(snapshot.get("baseline_cutoff")) is not int or snapshot["baseline_cutoff"] <= 0 or
            type(snapshot.get("current_cutoff")) is not int or
            snapshot["current_cutoff"] <= snapshot["baseline_cutoff"] or
            type(snapshot.get("accepted_prefix_count")) is not int or
            snapshot["accepted_prefix_count"] < 6 or
            snapshot["accepted_prefix_count"] > snapshot["current_cutoff"] or
            snapshot.get("evidence_snapshot_id") != bundle.evidence_snapshot_id or
            snapshot["current_cutoff"] != bundle.evidence_cutoff or
            snapshot_event["source_sequence"] <= arm_event["source_sequence"]):
        raise ValidationError("v3 evidence snapshot does not bind the selected exact prefix")

    qualifying: dict[int, set[str]] = {reporter: set() for reporter in (4, 5, 6)}
    last_ingestion = 0
    for event in manager_events:
        if event["event_type"] != "evidence.observation_accepted":
            continue
        payload = event["payload"]
        if not isinstance(payload, Mapping) or type(payload.get("ingestion_sequence")) is not int:
            raise ValidationError("manager accepted observation has invalid ingestion sequence")
        ingestion = payload["ingestion_sequence"]
        if ingestion <= last_ingestion:
            raise ValidationError("manager accepted evidence order is not strict")
        last_ingestion = ingestion
        if ingestion > snapshot["current_cutoff"]:
            continue
        observation = payload.get("observation")
        if not isinstance(observation, Mapping):
            raise ValidationError("manager accepted observation payload is absent")
        config = observation.get("configuration")
        if (not isinstance(config, Mapping) or observation.get("observed_replica_id") != 1 or
                observation.get("reporter_id") not in qualifying or
                config.get("tree_id") != observation["reporter_id"] or
                config.get("tree_id") not in arm["required_tree_ids"] or
                config.get("epoch_number") != 0 or config.get("epoch_digest") != binding["e0_digest"] or
                observation.get("expected_message_type") != "aggregate_relay"):
            continue
        if (observation.get("schema_version") != 3 or observation.get("outcome") != "timeout" or
                observation.get("response_duration_us") != 0 or observation.get("signer_set") != [] or
                type(observation.get("attempt_start_monotonic_ns")) is not int or
                type(observation.get("deadline_duration_us")) is not int or
                type(observation.get("reporter_monotonic_ns")) is not int or
                observation["deadline_duration_us"] <= 0 or
                event["source_sequence"] <= arm_event["source_sequence"] or
                event["source_sequence"] >= snapshot_event["source_sequence"] or
                observation["attempt_start_monotonic_ns"] <= arm_event["source_monotonic_ns"]):
            raise ValidationError("required actor-1 reporter context is not a post-arm timeout")
        block_hash = _hex64(observation.get("block_hash"), "accepted timeout block")
        key = (config["tree_id"], observation["reporter_id"], block_hash)
        omitted = physical.get(key)
        if omitted is None or not (observation["attempt_start_monotonic_ns"] <=
                                   omitted["source_monotonic_ns"] <
                                   observation["attempt_start_monotonic_ns"] +
                                   observation["deadline_duration_us"] * 1000 <=
                                   observation["reporter_monotonic_ns"] <=
                                   event["source_monotonic_ns"]):
            raise ValidationError("accepted timeout lacks its exact native physical omission")
        if block_hash in qualifying[observation["reporter_id"]]:
            raise ValidationError("reporter reuses one physical omission context")
        qualifying[observation["reporter_id"]].add(block_hash)
    if any(len(blocks) < 2 for blocks in qualifying.values()):
        raise ValidationError("three internal reporters lack two exact post-arm timeouts each")

    terminals = [event for event in manager_events
                 if event["event_type"] == "adaptive_v2_session_terminal"]
    good = [event for event in terminals if isinstance(event.get("payload"), Mapping) and
            event["payload"].get("outcome") == "advanced" and
            event["payload"].get("reason") == "successor_converged" and
            event["payload"].get("policy_intent") == "fault_containment" and
            event["payload"].get("predecessor_epoch_number") == 0 and
            event["payload"].get("predecessor_epoch_digest") == binding["e0_digest"] and
            event["payload"].get("successor_epoch_number") == 1 and
            event["payload"].get("successor_epoch_digest") == bundle.epoch_digest and
            event["payload"].get("command_payload_digest") == bundle.command.payload_digest and
            event["payload"].get("current_evidence_cutoff") == snapshot["current_cutoff"] and
            event["source_sequence"] > snapshot_event["source_sequence"]]
    if len(good) != 1:
        raise ValidationError("native manager lacks one success terminal bound to selected E1")


def validate_raw_bundle(root: Path, receipt_path: Path) -> dict[str, Any]:
    """Reopen a sealed bundle and return only a no-claim component verdict."""
    root = Path(root).resolve()
    if root.is_symlink() or not root.is_dir():
        raise ValidationError("bundle root is not a directory")
    raw_receipt = _read_descriptor(root, {"path": str(receipt_path), "sha256": hashlib.sha256((root / receipt_path).read_bytes()).hexdigest()}, "receipt", _MAX_SMALL)
    receipt = _strict_object(raw_receipt, "receipt")
    _check_receipt(receipt)
    artifacts = receipt["artifacts"]
    if not isinstance(artifacts, Mapping):
        raise ValidationError("receipt artifacts are invalid")
    common = {"profile", "epoch0_tree", "main_config", "execution_plan", "authorization_request", "approved_authorization", "e0_identity_receipt", "e0_identity_helper", "finalization_receipt", "fault_window_attestation", "manager_events", "manager_log", "cleanup", "replica_events", "replica_logs", "replica_configs", "executables"}
    adaptive = {"transition_request", "issuer_public_key", "e1_bundle", "manager_evidence_snapshot", "manager_fault_window_arm"}
    expected_artifacts = common | (adaptive if receipt["arm"] == "adaptive_e1" else set())
    if set(artifacts) != expected_artifacts:
        raise ValidationError("receipt artifact contract does not match its arm")
    for key in common - {"replica_events", "replica_logs", "replica_configs", "executables"}:
        _read_descriptor(root, artifacts[key], key,
                         _MAX_RAW if key.endswith(("events", "log")) or key == "e0_identity_helper"
                         else _MAX_SMALL)
    replica_events = _require_descriptor_list(artifacts, "replica_events", 7)
    replica_logs = _require_descriptor_list(artifacts, "replica_logs", 7)
    replica_configs = _require_descriptor_list(artifacts, "replica_configs", 7)
    executables = artifacts.get("executables")
    if not isinstance(executables, Mapping) or set(executables) != {"hotstuff_app", "adaptation_manager"}:
        raise ValidationError("receipt executable descriptors drifted")
    for key, descriptor in executables.items():
        _read_descriptor(root, descriptor, key, _MAX_RAW)
    run_id = receipt["run_id"]
    streams = {
        f"replica-{replica}": _parse_jsonl(_read_descriptor(root, replica_events[replica], f"replica-{replica} events", _MAX_RAW), run_id=run_id, source_id=f"replica-{replica}")
        for replica in range(7)
    }
    replica_config_raw = {
        replica: _read_descriptor(root, descriptor, f"replica-{replica} config", _MAX_SMALL)
        for replica, descriptor in enumerate(replica_configs)
    }
    logs = {
        replica: _read_descriptor(root, descriptor, f"replica-{replica} log", _MAX_RAW)
        for replica, descriptor in enumerate(replica_logs)
    }
    manager_events = _parse_jsonl(
        _read_descriptor(root, artifacts["manager_events"], "manager events", _MAX_RAW),
        run_id=run_id, source_id="adaptive-manager",
    )
    if any(event["source_kind"] != "adaptation_manager" for event in manager_events):
        raise ValidationError("manager JSONL source kind is not adaptation_manager")
    # Reopen the E0 authority; the receipt field is insufficient on its own.
    e0 = _validate_source_derived_e0(root, artifacts,
                                     expected_digest=receipt["launch_binding"]["e0_digest"])
    plan_raw = _read_descriptor(root, artifacts["execution_plan"], "execution plan", _MAX_SMALL)
    plan = _canonical_object(plan_raw, "execution plan")
    if (plan.get("plan_sha256") != receipt["plan_sha256"] or
            hashlib.sha256(_canonical({key: value for key, value in plan.items() if key != "plan_sha256"})).hexdigest() != receipt["plan_sha256"] or
            plan.get("comparison", {}).get("arm") != receipt["arm"] or plan.get("no_retry") is not True):
        raise ValidationError("execution plan does not bind exact no-retry receipt")
    manager_argv = _validate_archived_launch_binding(plan, artifacts, receipt=receipt)
    commands = plan.get("commands")
    if (not isinstance(commands, Mapping) or
            not isinstance(commands.get("manager"), Mapping) or
            not isinstance(commands["manager"].get("argv"), list) or
            not isinstance(commands.get("replicas"), list) or
            len(commands["replicas"]) != 7):
        raise ValidationError("execution plan lacks source-instance launch bindings")
    if manager_argv.count("--structured-event-source-instance") != 1:
        raise ValidationError("adaptive-manager launch argv lacks a unique source instance")
    manager_index = manager_argv.index("--structured-event-source-instance")
    if (manager_index + 1 >= len(manager_argv) or
            manager_events[0]["source_instance"] != manager_argv[manager_index + 1]):
        raise ValidationError("adaptive-manager JSONL source instance differs from launch plan")
    designated_observer: int | None = None
    for replica, row in enumerate(commands["replicas"]):
        if not isinstance(row, Mapping) or row.get("replica_id") != replica or not isinstance(row.get("argv"), list):
            raise ValidationError("replica source-instance launch binding is malformed")
        source = f"replica-{replica}"
        config = replica_config_raw[replica]
        if _config_option(config, "idx", f"{source} config") != str(replica):
            raise ValidationError(f"{source} config index differs from planned replica")
        if _config_option(config, "structured-event-run-id", f"{source} config") != run_id:
            raise ValidationError(f"{source} config run ID differs from receipt")
        if streams[source][0]["source_instance"] != _config_option(
                config, "structured-event-source-instance", f"{source} config"):
            raise ValidationError(f"{source} JSONL source instance differs from archived config")
        observer_id = _config_option(config, "structured-event-commit-observer-id", f"{source} config")
        if observer_id != "replica-2":
            raise ValidationError(f"{source} config does not bind replica-2 as designated observer")
        observer_instance = _config_option(
            config, "structured-event-commit-observer-instance", f"{source} config")
        if observer_instance != _config_option(
                replica_config_raw[2], "structured-event-source-instance", "replica-2 config"):
            raise ValidationError(f"{source} config observer instance differs from replica-2 config")
        designated_observer = 2
    if designated_observer is None:
        raise ValidationError("receipt has no designated commit observer")
    profile_raw = _read_descriptor(root, artifacts["profile"], "profile", _MAX_SMALL)
    if hashlib.sha256(profile_raw).hexdigest() != receipt["launch_binding"]["native_profile_sha256"]:
        raise ValidationError("archived native profile differs from receipt binding")
    request_raw = _read_descriptor(root, artifacts["authorization_request"], "authorization request", _MAX_SMALL)
    approval_raw = _read_descriptor(root, artifacts["approved_authorization"], "approved authorization", _MAX_SMALL)
    if (hashlib.sha256(request_raw).hexdigest() != receipt["launch_binding"]["request_sha256"] or
            hashlib.sha256(approval_raw).hexdigest() != receipt["launch_binding"]["approval_sha256"]):
        raise ValidationError("authorization bytes differ from receipt launch binding")
    request = _canonical_object(request_raw, "authorization request")
    if (set(request) != {"schema_version", "kind", "execution_plan_sha256", "repository_revision",
                         "arm", "scheduled_window", "hard_timeout_seconds", "no_retry",
                         "claim_eligible", "figure_eligible"} or
            request["schema_version"] != 1 or
            request["kind"] != "kauri-n7-sustained-role-execution-authorization-request-v1" or
            request["execution_plan_sha256"] != receipt["plan_sha256"] or
            request["repository_revision"] != plan.get("repository_revision") or
            request["arm"] != receipt["arm"] or
            request["scheduled_window"] != plan.get("scheduled_window") or
            {key: request["scheduled_window"].get(key) for key in ("start_monotonic_ns", "end_monotonic_ns")} !=
            receipt["launch_binding"]["scheduled_window"] or
            type(request["hard_timeout_seconds"]) is not int or
            request["hard_timeout_seconds"] < 120 or request["no_retry"] is not True or
            request["claim_eligible"] is not False or request["figure_eligible"] is not False):
        raise ValidationError("authorization request is not the frozen no-retry plan")
    approval = _canonical_object(approval_raw, "approved authorization")
    approval_kind = ("kauri-n7-sustained-role-fixed-e0-launch-authorization-v1"
                     if receipt["arm"] == "fixed_e0" else
                     "kauri-n7-sustained-role-adaptive-e1-launch-authorization-v1")
    if (set(approval) != {"schema_version", "kind", "request_sha256", "plan_sha256",
                          "approval_reference", "approved_utc", "no_retry"} or
            approval["schema_version"] != 1 or approval["kind"] != approval_kind or
            approval["request_sha256"] != hashlib.sha256(request_raw).hexdigest() or
            approval["plan_sha256"] != receipt["plan_sha256"] or
            not isinstance(approval["approval_reference"], str) or
            not approval["approval_reference"].strip() or
            not isinstance(approval["approved_utc"], str) or
            not approval["approved_utc"].endswith("Z") or approval["no_retry"] is not True):
        raise ValidationError("approved authorization does not bind exact external request")
    attestation = _canonical_object(_read_descriptor(root, artifacts["fault_window_attestation"], "prearm attestation", _MAX_SMALL), "prearm attestation")
    expected_attestation = ({"schema_version", "kind", "run_id", "epoch_zero_digest", "prearm_monotonic_ns", "scheduled_start_monotonic_ns", "no_retry"} if receipt["arm"] == "fixed_e0" else {"schema_version", "kind", "run_id", "prearm_monotonic_ns", "scheduled_start_monotonic_ns", "no_retry"})
    expected_kind = "kauri-n7-sustained-role-prearm-v1" if receipt["arm"] == "fixed_e0" else "kauri-n7-sustained-role-adaptive-e1-prearm-v1"
    if (set(attestation) != expected_attestation or attestation["schema_version"] != 1 or
            attestation["kind"] != expected_kind or attestation["run_id"] != run_id or attestation["no_retry"] is not True or
            (receipt["arm"] == "fixed_e0" and attestation["epoch_zero_digest"] != receipt["launch_binding"]["e0_digest"]) or
            type(attestation["prearm_monotonic_ns"]) is not int or attestation["prearm_monotonic_ns"] >= receipt["launch_binding"]["scheduled_window"]["start_monotonic_ns"] or
            attestation["scheduled_start_monotonic_ns"] != receipt["launch_binding"]["scheduled_window"]["start_monotonic_ns"]):
        raise ValidationError("prearm attestation does not bind the scheduled E0 baseline")
    _validate_prearm_e0_common_commit(
        streams,
        scheduled_start_ns=receipt["launch_binding"]["scheduled_window"]["start_monotonic_ns"],
        e0_digest=receipt["launch_binding"]["e0_digest"],
        designated_observer=designated_observer,
    )
    cleanup = _canonical_object(_read_descriptor(root, artifacts["cleanup"], "cleanup receipt", _MAX_SMALL), "cleanup receipt")
    processes = cleanup.get("processes")
    expected_sources = {"adaptive-manager", *(f"replica-{i}" for i in range(7))}
    if (set(cleanup) != {"schema_version", "run_id", "complete", "processes"} or cleanup["schema_version"] != 1 or cleanup["run_id"] != run_id or cleanup["complete"] is not True or
            not isinstance(processes, list) or len(processes) != 8 or
            {row.get("source_id") for row in processes if isinstance(row, Mapping)} != expected_sources or
            any(not isinstance(row, Mapping) or set(row) != {"source_id", "pid", "pgid", "returncode", "termination"} or row["returncode"] != 0 or row["termination"] != "clean-exit" for row in processes)):
        raise ValidationError("cleanup receipt does not prove eight clean exits")
    if receipt["arm"] == "fixed_e0":
        finalization = _canonical_object(_read_descriptor(root, artifacts["finalization_receipt"], "fixed-E0 finalization", _MAX_SMALL), "fixed-E0 finalization")
        expected_final = {"schema_version", "kind", "state", "plan_sha256", "authorization_sha256", "e0_identity_sha256", "no_retry"}
        if (set(finalization) != expected_final or finalization["schema_version"] != 1 or
                finalization["kind"] != "kauri-n7-sustained-role-fixed-e0-launch-finalization-v1" or
                finalization["state"] != "FIXED_E0_HORIZON_COMPLETED_NO_SUCCESSOR" or
                finalization["plan_sha256"] != receipt["plan_sha256"] or finalization["no_retry"] is not True or
                finalization["authorization_sha256"] != hashlib.sha256(approval_raw).hexdigest() or
                finalization["e0_identity_sha256"] != hashlib.sha256(_canonical(e0)).hexdigest()):
            raise ValidationError("fixed-E0 finalization is not an exact no-successor receipt")
    anchor = receipt["anchor"]
    event = next((item for item in streams["replica-1"] if item["source_sequence"] == anchor["source_sequence"]), None)
    if event is None or event["_raw_line_sha256"] != anchor["line_sha256"]:
        raise ValidationError("receipt anchor does not bind replica-1 JSONL bytes")
    if event["source_monotonic_ns"] != anchor["monotonic_ns"] or event["event_type"] != "fault.contribution_opportunity":
        raise ValidationError("receipt anchor is not a physical fault opportunity")
    payload = event["payload"]
    proposal = payload.get("proposal") if isinstance(payload, Mapping) else None
    if (not isinstance(payload, Mapping) or payload.get("actor") != 1 or
            payload.get("fault_mode") != "role_scoped_persistent_selected_omission_v1" or
            payload.get("physical_role") != "internal" or
            payload.get("scheduled_action") != "omit_aggregate" or
            payload.get("expected_message_type") != "aggregate_relay" or
            not isinstance(proposal, Mapping) or proposal.get("epoch_number") != 0 or
            proposal.get("epoch_digest") != receipt["launch_binding"]["e0_digest"]):
        raise ValidationError("receipt anchor is not the scheduled actor-1 role-scoped fault")
    first_anchor = next((item for item in streams["replica-1"]
                         if item["event_type"] == "fault.contribution_opportunity" and
                         isinstance(item.get("payload"), Mapping) and
                         item["payload"].get("actor") == 1 and
                         item["payload"].get("physical_role") == "internal" and
                         item["payload"].get("scheduled_action") == "omit_aggregate"), None)
    if first_anchor is None or first_anchor["source_sequence"] != anchor["source_sequence"]:
        raise ValidationError("receipt anchor is not the first E0 internal aggregate omission")
    if any(not stream or stream[-1]["source_monotonic_ns"] < anchor["monotonic_ns"] + _HORIZON_NS for stream in streams.values()):
        raise ValidationError("replica streams do not cover the common horizon")
    opportunities = [_opportunity_identity(item) for item in streams["replica-1"] if item["event_type"] == "fault.contribution_opportunity"]
    markers = [_marker_identity(marker) for marker in _marker_fields(logs[1]) if marker.get("fault") == "role_scoped_persistent_selected_omission_v1"]
    if not opportunities or len(opportunities) != len(set(opportunities)) or sorted(opportunities) != sorted(markers):
        raise ValidationError("structured fault opportunities do not biject native KAURI_FAULT markers")
    e1_digest = None
    if receipt["arm"] == "adaptive_e1":
        bundle = _validate_adaptive_bundle_and_activation(root, artifacts, receipt=receipt, streams=streams,
                                                          anchor_ns=anchor["monotonic_ns"])
        e1_digest = bundle.epoch_digest
        _validate_adaptive_causality(root, artifacts, receipt=receipt,
                                     manager_events=manager_events,
                                     actor_events=streams["replica-1"], bundle=bundle)
        finalization = _canonical_object(_read_descriptor(root, artifacts["finalization_receipt"], "adaptive-E1 finalization", _MAX_SMALL), "adaptive-E1 finalization")
        if (set(finalization) != {"schema_version", "kind", "state", "plan_sha256", "authorization_sha256", "no_retry"} or
                finalization["schema_version"] != 1 or finalization["kind"] != "kauri-n7-sustained-role-adaptive-e1-launch-finalization-v1" or
                finalization["state"] != "ADAPTIVE_E1_RAW_HORIZON_COMPLETED" or finalization["plan_sha256"] != receipt["plan_sha256"] or
                finalization["authorization_sha256"] != hashlib.sha256(approval_raw).hexdigest() or finalization["no_retry"] is not True):
            raise ValidationError("adaptive-E1 finalization does not bind the raw no-retry horizon")
    else:
        _validate_fixed_e0_manager(manager_events, receipt=receipt, anchor_ns=anchor["monotonic_ns"])
    metric = _validate_commit_metrics(streams, anchor_ns=anchor["monotonic_ns"],
                                      e0_digest=receipt["launch_binding"]["e0_digest"], arm=receipt["arm"], e1_digest=e1_digest,
                                      designated_observer=designated_observer)
    return {
        "verdict": "PASS_COMPONENT_ONLY_NO_CLAIM",
        "arm": receipt["arm"], "run_id": receipt["run_id"], "plan_sha256": receipt["plan_sha256"],
        "e0_digest": receipt["launch_binding"]["e0_digest"], "anchor_monotonic_ns": anchor["monotonic_ns"],
        "common_horizon_ns": _HORIZON_NS, "commit_metric": {"definition": "replica2_designated_commit_joined_to_all7_commit_observed", "counts": metric},
        "claim_boundary": "Independent raw replay passed only; no throughput, recovery, safety, or thesis claim follows.",
    }
