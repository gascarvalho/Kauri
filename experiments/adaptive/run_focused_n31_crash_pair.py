#!/usr/bin/env python3
"""CLI gate for the focused N7 smoke and N31 matched crash campaign."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.adaptive.kauri_experiment.focused_crash_pair_runtime import (
    FocusedCrashPairRuntimeError,
    FocusedLivePreflightChecks,
    FocusedLaunchBackend,
    FocusedProfile,
    build_focused_authorization_request,
    load_focused_profile,
    prepare_focused_preflight,
    reload_pair_issuer_allocations,
    verify_focused_authorization_receipt,
)
from experiments.adaptive.kauri_experiment.focused_crash_pair_validation import (
    FocusedCrashPairValidationError,
    validate_sealed_campaign,
    validate_sealed_pair,
)
from experiments.adaptive.kauri_experiment.profiled_fault_archive import (
    create_evidence_seal,
    verify_evidence_seal,
)
from experiments.adaptive import run_n31_crash_pair_campaign as campaign_contracts


_PAIR_ABORT_SLOT_IDENTITIES = {
    "control": ("slot-01", 1),
    "adaptive": ("slot-02", 2),
}


class FocusedCrashPairCliError(RuntimeError):
    """The CLI invocation is not exactly authorized for one result root."""


FOCUSED_LAUNCH_BACKEND = FocusedLaunchBackend()
FOCUSED_PREFLIGHT_CHECKS: object = FocusedLivePreflightChecks
_PROFILE_DIRECTORY = Path(__file__).resolve().parent / "profiles"
_V12_PROFILES = {
    "smoke": _PROFILE_DIRECTORY / "n7-f2-q5-two-crash-pair-smoke-v12.json",
    "pair": _PROFILE_DIRECTORY / "n31-f5-q21-three-crash-pair-v12.json",
    "campaign": _PROFILE_DIRECTORY / "n31-f5-q21-three-crash-pair-v12.json",
}


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise FocusedCrashPairCliError(f"{label} must be a regular non-symlink file")
    try:
        document = json.loads(candidate.read_bytes())
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise FocusedCrashPairCliError(f"{label} is invalid JSON") from exc
    if not isinstance(document, Mapping):
        raise FocusedCrashPairCliError(f"{label} must contain an object")
    return document


def _write_cli_result(value: object) -> None:
    sys.stdout.write(json.dumps(value, allow_nan=False, sort_keys=True))
    sys.stdout.write("\n")


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        + b"\n"
    )


def _write_exclusive_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = path.open("xb")
    with descriptor:
        descriptor.write(_canonical(value))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _pending_external_provenance(
    directory: Path,
    *,
    seal: object,
    children: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Describe the sealed aggregate that an independent validator must bind.

    Execution cannot know the seal digests before the child evidence exists.
    It must therefore never fabricate trusted provenance for its own output.
    """

    return {
        "verdict": "PROVISIONAL",
        "trusted_provenance_required": True,
        "trusted_provenance_supplied": False,
        "pending_external_provenance": {
            "directory": str(directory),
            "evidence_tree_sha256": str(getattr(seal, "tree_sha256")),
            "evidence_seal_sha256": str(getattr(seal, "seal_sha256")),
            "children": [dict(child) for child in children],
        },
    }


def _bounded_failure_reason(error: BaseException) -> str:
    """Project one exception into a bounded, non-control diagnostic string."""

    raw = str(error)
    printable = "".join(
        character if " " <= character <= "~" else "?" for character in raw
    )
    return (printable or type(error).__name__)[:256]


def _sha256_field(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise FocusedCrashPairCliError(f"{label} is not a canonical SHA-256 digest")
    return value


def _pair_abort_diagnostic_path(output_root: Path, pair_id: str) -> Path:
    """Choose a diagnostic location without following a hostile pair symlink."""

    lexical_root = output_root
    lexical_pair = lexical_root / pair_id
    if not lexical_pair.is_symlink():
        root = lexical_root.resolve()
        pair = lexical_pair.resolve()
        if pair.parent == root:
            return lexical_pair / "abort-finalization-failure.json"
    return lexical_root / "pair-abort-finalization-failure.json"


def _finalize_campaign_abort(
    *,
    output_root: Path,
    plan: Mapping[str, object],
    slot: Mapping[str, object],
    slot_directory: Path,
    completed_records: Sequence[Mapping[str, object]],
    runtime_error: FocusedCrashPairRuntimeError,
    cleanup: Mapping[str, object] | None,
    cleanup_error: BaseException | None,
) -> None:
    """Persist a non-claimable campaign prefix without normal aggregation."""

    slot_abort = {
        "schema_version": 1,
        "kind": "kauri-focused-slot-abort-v1",
        "state": "INCOMPLETE",
        "claim_eligible": False,
        "plan_sha256": _sha256_field(plan.get("plan_sha256"), "campaign plan"),
        "slot_id": str(slot["slot_id"]),
        "pair_id": str(slot["pair_id"]),
        "arm": str(slot["arm"]),
        "pair_seed": int(slot["pair_seed"]),
        "execution_ordinal": int(slot["execution_ordinal"]),
        "attempt_ordinal": 1,
        "automatic_retries": 0,
        "replacement_policy": "none",
        "failure": {
            "category": "runtime_error",
            "reason": _bounded_failure_reason(runtime_error),
        },
    }
    _write_exclusive_json(slot_directory / "slot-abort.json", slot_abort)

    if cleanup is not None:
        _write_exclusive_json(slot_directory / "cleanup.json", dict(cleanup))
    if (
        cleanup_error is not None
        or cleanup is None
        or cleanup.get("complete") is not True
    ):
        reason = cleanup_error or FocusedCrashPairRuntimeError(
            "cleanup did not establish a quiescent process set"
        )
        _write_exclusive_json(
            slot_directory / "cleanup-failure.json",
            {
                "schema_version": 1,
                "kind": "kauri-focused-cleanup-failure-v1",
                "state": "INCOMPLETE",
                "claim_eligible": False,
                "complete": False,
                "category": "cleanup_error",
                "reason": _bounded_failure_reason(reason),
            },
        )
        return

    planned_slots = {
        str(candidate["slot_id"]): candidate for candidate in plan["slots"]
    }
    failed_ordinal = int(slot["execution_ordinal"])
    completed_prefix: list[dict[str, object]] = []
    for record in completed_records:
        completed_slot = planned_slots[str(record["slot_id"])]
        seal = record["seal"]
        if not isinstance(seal, Mapping):
            raise FocusedCrashPairCliError("completed child seal is malformed")
        verified = verify_evidence_seal(
            output_root / "children" / str(completed_slot["slot_id"])
        )
        verified_tree_sha = _sha256_field(
            getattr(verified, "tree_sha256", None), "verified completed child tree"
        )
        verified_seal_sha = _sha256_field(
            getattr(verified, "seal_sha256", None), "verified completed child seal"
        )
        if (
            _sha256_field(seal.get("tree_sha256"), "completed child tree")
            != verified_tree_sha
            or _sha256_field(seal.get("seal_sha256"), "completed child seal")
            != verified_seal_sha
        ):
            raise FocusedCrashPairCliError(
                "completed child seal differs from its persisted evidence"
            )
        completed_prefix.append(
            {
                "execution_ordinal": int(completed_slot["execution_ordinal"]),
                "slot_id": str(completed_slot["slot_id"]),
                "pair_id": str(completed_slot["pair_id"]),
                "arm": str(completed_slot["arm"]),
                "child_tree_sha256": verified_tree_sha,
                "child_seal_sha256": verified_seal_sha,
            }
        )
    completed_prefix.sort(key=lambda entry: int(entry["execution_ordinal"]))
    if [int(entry["execution_ordinal"]) for entry in completed_prefix] != list(
        range(1, failed_ordinal)
    ):
        raise FocusedCrashPairCliError(
            "completed campaign prefix is not contiguous before the failed slot"
        )
    child_seal = create_evidence_seal(slot_directory)
    child_tree_sha = _sha256_field(
        getattr(child_seal, "tree_sha256", None), "incomplete child tree"
    )
    child_seal_sha = _sha256_field(
        getattr(child_seal, "seal_sha256", None), "incomplete child seal"
    )
    not_started = [
        {
            "execution_ordinal": int(candidate["execution_ordinal"]),
            "slot_id": str(candidate["slot_id"]),
        }
        for candidate in plan["slots"]
        if int(candidate["execution_ordinal"]) > failed_ordinal
    ]
    _write_exclusive_json(
        output_root / "campaign-abort-prefix.json",
        {
            "schema_version": 1,
            "kind": "kauri-focused-campaign-abort-prefix-v1",
            "state": "ABORTED_INCOMPLETE",
            "claim_eligible": False,
            "plan_sha256": _sha256_field(plan.get("plan_sha256"), "campaign plan"),
            "failed_slot": {
                "execution_ordinal": failed_ordinal,
                "slot_id": str(slot["slot_id"]),
                "pair_id": str(slot["pair_id"]),
                "arm": str(slot["arm"]),
                "child_tree_sha256": child_tree_sha,
                "child_seal_sha256": child_seal_sha,
            },
            "completed_prefix": completed_prefix,
            "not_started": not_started,
            "automatic_retries": 0,
            "replacement_policy": "none",
            "continuation": "prohibited",
        },
    )
    create_evidence_seal(output_root)


def _finalize_pair_abort(
    *,
    output_root: Path,
    slot: Mapping[str, object],
    configuration: Mapping[str, object],
    completed_records: Sequence[Mapping[str, object]],
    mode: str,
    runtime_error: FocusedCrashPairRuntimeError,
    cleanup: Mapping[str, object] | None,
    cleanup_error: BaseException | None,
) -> Path:
    """Seal one quiescent smoke/pair abort without normal aggregation."""

    if mode not in {"smoke", "pair"}:
        raise FocusedCrashPairCliError("pair abort mode is invalid")
    pair_id = str(slot["pair_id"])
    arm = str(slot["arm"])
    if arm not in {"control", "adaptive"}:
        raise FocusedCrashPairCliError("failed focused arm is invalid")
    expected_slot_id, expected_ordinal = _PAIR_ABORT_SLOT_IDENTITIES[arm]
    if (
        slot.get("slot_id") != expected_slot_id
        or slot.get("execution_ordinal") != expected_ordinal
        or not isinstance(slot.get("pair_seed"), int)
        or not pair_id
    ):
        raise FocusedCrashPairCliError("failed pair slot identity is not canonical")
    lexical_root = output_root
    lexical_pair_root = lexical_root / pair_id
    lexical_arm_root = lexical_pair_root / arm
    if lexical_pair_root.is_symlink() or lexical_arm_root.is_symlink():
        raise FocusedCrashPairCliError("pair abort path is a symlink")
    root = lexical_root.resolve()
    pair_root = lexical_pair_root.resolve()
    arm_root = lexical_arm_root.resolve()
    if pair_root.parent != root or arm_root.parent != pair_root:
        raise FocusedCrashPairCliError("failed focused arm path escapes its pair root")
    configured_root = configuration.get("run_directory")
    if (
        not isinstance(configured_root, (str, Path))
        or Path(configured_root).is_symlink()
        or Path(configured_root).resolve() != arm_root
        or any(
            configuration.get(key) != slot[key]
            for key in ("slot_id", "pair_id", "arm", "pair_seed", "execution_ordinal")
        )
    ):
        raise FocusedCrashPairCliError("failed arm configuration run directory drifted")
    if not arm_root.is_dir():
        raise FocusedCrashPairCliError("failed arm directory is absent")
    if (
        cleanup_error is not None
        or cleanup is None
        or cleanup.get("complete") is not True
    ):
        reason = cleanup_error or FocusedCrashPairRuntimeError(
            "cleanup did not establish a quiescent process set"
        )
        _write_exclusive_json(
            arm_root / "cleanup-failure.json",
            {
                "schema_version": 1,
                "kind": "kauri-focused-cleanup-failure-v1",
                "state": "INCOMPLETE",
                "claim_eligible": False,
                "complete": False,
                "category": "cleanup_error",
                "reason": _bounded_failure_reason(reason),
            },
        )
        return arm_root

    expected_completed = [] if arm == "control" else ["control"]
    if [str(record.get("arm")) for record in completed_records] != expected_completed:
        raise FocusedCrashPairCliError("completed pair prefix is not exact")
    completed: list[dict[str, object]] = []
    for record in completed_records:
        completed_arm = str(record["arm"])
        expected_completed_slot, expected_completed_ordinal = (
            _PAIR_ABORT_SLOT_IDENTITIES[completed_arm]
        )
        lexical_completed_root = lexical_pair_root / completed_arm
        if lexical_completed_root.is_symlink():
            raise FocusedCrashPairCliError("completed pair arm path is a symlink")
        completed_root = lexical_completed_root.resolve()
        recorded_configuration = record.get("configuration")
        seal = record.get("seal")
        if (
            not isinstance(recorded_configuration, Mapping)
            or not isinstance(seal, Mapping)
            or recorded_configuration.get("pair_id") != pair_id
            or recorded_configuration.get("arm") != completed_arm
            or record.get("slot_id") != expected_completed_slot
            or record.get("execution_ordinal") != expected_completed_ordinal
            or record.get("pair_id") != pair_id
            or record.get("pair_seed") != slot["pair_seed"]
            or any(
                recorded_configuration.get(key) != record.get(key)
                for key in ("slot_id", "pair_id", "arm", "pair_seed", "execution_ordinal")
            )
            or not isinstance(recorded_configuration.get("run_directory"), (str, Path))
            or Path(recorded_configuration["run_directory"]).is_symlink()
            or Path(recorded_configuration.get("run_directory", "")).resolve()
            != completed_root
        ):
            raise FocusedCrashPairCliError("completed pair arm binding drifted")
        verified = verify_evidence_seal(completed_root)
        tree_sha = _sha256_field(
            getattr(verified, "tree_sha256", None), "verified completed arm tree"
        )
        seal_sha = _sha256_field(
            getattr(verified, "seal_sha256", None), "verified completed arm seal"
        )
        if (
            _sha256_field(seal.get("tree_sha256"), "completed arm tree") != tree_sha
            or _sha256_field(seal.get("seal_sha256"), "completed arm seal") != seal_sha
        ):
            raise FocusedCrashPairCliError(
                "completed arm seal differs from its persisted evidence"
            )
        completed.append(
            {
                "arm": completed_arm,
                "slot_id": str(record["slot_id"]),
                "pair_id": pair_id,
                "pair_seed": int(record["pair_seed"]),
                "execution_ordinal": int(record["execution_ordinal"]),
                "tree_sha256": tree_sha,
                "seal_sha256": seal_sha,
            }
        )
    _write_exclusive_json(arm_root / "cleanup.json", dict(cleanup))
    _write_exclusive_json(
        arm_root / "arm-abort.json",
        {
            "schema_version": 1,
            "kind": "kauri-focused-arm-abort-v1",
            "state": "INCOMPLETE",
            "claim_eligible": False,
            "mode": mode,
            "slot_id": str(slot["slot_id"]),
            "pair_id": pair_id,
            "arm": arm,
            "pair_seed": int(slot["pair_seed"]),
            "execution_ordinal": int(slot["execution_ordinal"]),
            "automatic_retries": 0,
            "replacement_policy": "none",
            "failure": {
                "category": "runtime_error",
                "reason": _bounded_failure_reason(runtime_error),
            },
        },
    )
    failed_seal = create_evidence_seal(arm_root)
    failed = {
        "arm": arm,
        "slot_id": str(slot["slot_id"]),
        "pair_id": pair_id,
        "pair_seed": int(slot["pair_seed"]),
        "execution_ordinal": int(slot["execution_ordinal"]),
        "tree_sha256": _sha256_field(
            getattr(failed_seal, "tree_sha256", None), "failed arm tree"
        ),
        "seal_sha256": _sha256_field(
            getattr(failed_seal, "seal_sha256", None), "failed arm seal"
        ),
    }
    if not pair_root.is_dir():
        raise FocusedCrashPairCliError("pair root is absent")
    _write_exclusive_json(
        pair_root / "pair-abort.json",
        {
            "schema_version": 1,
            "kind": "kauri-focused-pair-abort-v1",
            "state": "ABORTED_INCOMPLETE",
            "claim_eligible": False,
            "mode": mode,
            "pair_id": pair_id,
            "pair_seed": int(slot["pair_seed"]),
            "failed_arm": failed,
            "completed_prefix": completed,
            "not_started_arms": [
                candidate
                for candidate in ("control", "adaptive")
                if candidate not in {entry["arm"] for entry in completed} | {arm}
            ],
            "automatic_retries": 0,
            "replacement_policy": "none",
            "continuation": "prohibited",
        },
    )
    create_evidence_seal(pair_root)
    return arm_root


def _execute_focused(
    invocation: Mapping[str, object], *, backend: object
) -> Mapping[str, object]:
    context = backend.bind_execution_context(invocation)
    pair_count = int(invocation["pair_count"])
    output_root = Path(invocation["output_root"])
    output_root.mkdir(parents=True, exist_ok=False)
    plan: Mapping[str, object] | None = None
    if invocation["mode"] == "campaign":
        profile = invocation["profile"]
        preflight = invocation["preflight_receipt"]
        execution = preflight.get("execution_context", {})
        revision = str(execution.get("repository", {}).get("revision", "b" * 40))
        build_sha = str(execution.get("build", {}).get("build_sha256", "c" * 64))
        topology_sha = str(execution.get("topology_proof_sha256", "d" * 64))
        plan = campaign_contracts.derive_focused_campaign_plan(
            pair_count=pair_count,
            campaign_seed=41_719,
            revision=revision,
            build_sha256=build_sha,
            profile_sha256=str(profile.profile_sha256),
            topology_proof_sha256=topology_sha,
            pair_seeds=tuple(41_719 + ordinal for ordinal in range(1, 6)),
        )
        _write_exclusive_json(output_root / "plan.json", plan)
        slots = list(plan["slots"])
    else:
        slots = [
            {
                "slot_id": f"slot-{ordinal:02d}",
                "pair_id": "pair-01",
                "pair_ordinal": 1,
                "arm": arm,
                "pair_seed": 41_720,
                "execution_ordinal": ordinal,
            }
            for ordinal, arm in enumerate(("control", "adaptive"), start=1)
        ]
    records: list[Mapping[str, object]] = []
    arm_records: dict[str, list[Mapping[str, object]]] = {}
    for slot in slots:
        pair_id = str(slot["pair_id"])
        arm = str(slot["arm"])
        slot_id = str(slot["slot_id"])
        slot_context = context
        if plan is not None:
            slot_context = {
                **context,
                "slot_id": slot_id,
                "pair_seed": slot["pair_seed"],
                "slot_directory": output_root / "children" / slot_id,
            }
        configuration = backend.materialize_arm_configuration(
            slot_context,
            pair_ordinal=int(slot["pair_ordinal"]),
            arm=arm,
        )
        if configuration.get("pair_id") != pair_id or configuration.get("arm") != arm:
            raise FocusedCrashPairCliError(
                "backend relabelled the prederived pair or arm identity"
            )
        identity = {
            "slot_id": slot_id,
            "pair_id": pair_id,
            "arm": arm,
            "pair_seed": slot["pair_seed"],
            "execution_ordinal": slot["execution_ordinal"],
        }
        if any(
            configuration.get(key) not in {None, value}
            for key, value in identity.items()
        ):
            raise FocusedCrashPairCliError("backend relabelled focused slot identity")
        configuration = {**configuration, **identity}
        slot_directory = output_root / "children" / slot_id
        if plan is not None and not slot_directory.exists():
            slot_directory.mkdir(parents=True)
        processes = backend.spawn_processes(configuration)
        outcome: Mapping[str, object] | None = None
        execution_error: BaseException | None = None
        execution_traceback = None
        try:
            if callable(getattr(backend, "run_arm", None)):
                outcome = backend.run_arm(configuration, processes)
            else:
                fault_receipt = backend.execute_atomic_fault_batch(
                    configuration, processes
                )
                outcome = backend.drive_event_hooks(
                    configuration, processes, fault_receipt
                )
        except BaseException as exc:
            execution_error = exc
            execution_traceback = exc.__traceback__

        cleanup: Mapping[str, object] | None = None
        cleanup_error: BaseException | None = None
        try:
            cleanup = backend.cleanup(configuration, processes)
        except BaseException as exc:
            cleanup_error = exc

        if execution_error is not None:
            if (
                isinstance(execution_error, FocusedCrashPairRuntimeError)
                and plan is not None
            ):
                try:
                    _finalize_campaign_abort(
                        output_root=output_root,
                        plan=plan,
                        slot=slot,
                        slot_directory=slot_directory,
                        completed_records=records,
                        runtime_error=execution_error,
                        cleanup=cleanup,
                        cleanup_error=cleanup_error,
                    )
                except BaseException as finalization_error:
                    try:
                        _write_exclusive_json(
                            slot_directory / "abort-finalization-failure.json",
                            {
                                "schema_version": 1,
                                "kind": "kauri-focused-abort-finalization-failure-v1",
                                "state": "INCOMPLETE",
                                "claim_eligible": False,
                                "category": "finalization_error",
                                "reason": _bounded_failure_reason(finalization_error),
                            },
                        )
                    except BaseException:
                        pass
                    try:
                        sys.stderr.write(
                            "campaign abort finalization failed: "
                            f"{_bounded_failure_reason(finalization_error)}\n"
                        )
                    except BaseException:
                        pass
            elif (
                isinstance(execution_error, FocusedCrashPairRuntimeError)
                and plan is None
                and invocation.get("mode") in {"smoke", "pair"}
            ):
                try:
                    _finalize_pair_abort(
                        output_root=output_root,
                        slot=slot,
                        configuration=configuration,
                        completed_records=arm_records.get(pair_id, ()),
                        mode=str(invocation["mode"]),
                        runtime_error=execution_error,
                        cleanup=cleanup,
                        cleanup_error=cleanup_error,
                    )
                except BaseException as finalization_error:
                    try:
                        _write_exclusive_json(
                            _pair_abort_diagnostic_path(output_root, pair_id),
                            {
                                "schema_version": 1,
                                "kind": "kauri-focused-abort-finalization-failure-v1",
                                "state": "INCOMPLETE",
                                "claim_eligible": False,
                                "category": "finalization_error",
                                "reason": _bounded_failure_reason(finalization_error),
                            },
                        )
                    except BaseException:
                        pass
                    try:
                        sys.stderr.write(
                            "pair abort finalization failed: "
                            f"{_bounded_failure_reason(finalization_error)}\n"
                        )
                    except BaseException:
                        pass
            raise execution_error.with_traceback(execution_traceback)
        if cleanup_error is not None:
            raise cleanup_error
        if outcome is None or cleanup is None:
            raise FocusedCrashPairCliError(
                "arm execution did not produce terminal state"
            )
        materialize = getattr(backend, "materialize_artifacts", None)
        if callable(materialize):
            materialize(configuration, outcome, cleanup)
        seal = backend.seal(configuration, outcome, cleanup)
        validation = backend.validate(configuration, seal)
        if (
            validation.get("pair_id", pair_id) != pair_id
            or validation.get("arm", arm) != arm
        ):
            raise FocusedCrashPairCliError(
                "backend validation relabelled the pair or arm identity"
            )
        ledger = backend.append_ledger(slot_context, configuration, validation)
        record = {
            "slot_id": slot_id,
            "pair_id": pair_id,
            "arm": arm,
            "pair_seed": slot["pair_seed"],
            "execution_ordinal": slot["execution_ordinal"],
            "validation": dict(validation),
            "ledger": dict(ledger),
            "seal": dict(seal),
            "configuration": dict(configuration),
        }
        records.append(record)
        arm_records.setdefault(pair_id, []).append(record)

    pair_validations: list[dict[str, object]] = []
    for pair_id, children in sorted(arm_records.items()):
        pair_root = output_root / pair_id
        pair_root.mkdir(parents=True, exist_ok=True)
        entries: list[dict[str, object]] = []
        for child in children:
            seal = child["seal"]
            configuration = child["configuration"]
            child_root = configuration.get("run_directory")
            relative = str(child["arm"])
            if plan is not None:
                relative = f"children/{child['arm']}"
                destination = pair_root / relative
                if isinstance(child_root, (str, Path)) and Path(child_root).is_dir():
                    shutil.copytree(Path(child_root), destination)
                else:
                    destination.mkdir(parents=True, exist_ok=False)
            elif isinstance(child_root, (str, Path)):
                candidate = Path(child_root)
                try:
                    relative = str(candidate.relative_to(pair_root))
                except ValueError:
                    relative = str(child["arm"])
            tree_sha = seal.get("tree_sha256") or _digest(
                {"pair_id": pair_id, "arm": child["arm"], "kind": "tree"}
            )
            seal_sha = seal.get("seal_sha256") or _digest(
                {"pair_id": pair_id, "arm": child["arm"], "kind": "seal"}
            )
            entries.append(
                {
                    "arm": child["arm"],
                    "path": relative,
                    "tree_sha256": tree_sha,
                    "seal_sha256": seal_sha,
                }
            )
        _write_exclusive_json(
            pair_root / "pair-receipt.json",
            {
                "schema_version": 1,
                "pair_id": pair_id,
                "automatic_retries": 0,
                "replacement_policy": "none",
                "children": entries,
            },
        )
        pair_seal = create_evidence_seal(pair_root)
        pair_validations.append(
            {
                "pair_id": pair_id,
                **_pending_external_provenance(
                    pair_root,
                    seal=pair_seal,
                    children=entries,
                ),
            }
        )

    campaign_validation: Mapping[str, object] | None = None
    if plan is not None:
        by_slot = {str(record["slot_id"]): record for record in records}
        previous = str(plan["ledger_genesis_sha256"])
        ledger_records: list[dict[str, object]] = []
        for slot in plan["slots"]:
            record = by_slot[str(slot["slot_id"])]
            seal = record["seal"]
            validation = record["validation"]
            verdict = validation.get("verdict")
            child_outcome = validation.get("outcome")
            if child_outcome not in {"PASS", "FAIL", "INCOMPLETE"}:
                child_outcome = (
                    "PASS"
                    if verdict == "PASS"
                    else "FAIL" if verdict == "FAIL" else "INCOMPLETE"
                )
            ledger_record: dict[str, object] = {
                "schema_version": 1,
                "plan_sha256": plan["plan_sha256"],
                "execution_ordinal": slot["execution_ordinal"],
                "slot_id": slot["slot_id"],
                "pair_id": slot["pair_id"],
                "arm": slot["arm"],
                "pair_seed": slot["pair_seed"],
                "attempt_ordinal": 1,
                "state": "TERMINAL",
                "execution_outcome": child_outcome,
                "validation": {
                    "outcome": child_outcome,
                    "integrity_valid": validation.get("integrity_valid") is True,
                    "claim_slot": validation.get("claim_slot") is True,
                },
                "child_tree_sha256": seal.get("tree_sha256")
                or _digest({"slot_id": slot["slot_id"], "kind": "tree"}),
                "child_seal_sha256": seal.get("seal_sha256")
                or _digest({"slot_id": slot["slot_id"], "kind": "seal"}),
                "previous_record_sha256": previous,
            }
            ledger_record["record_sha256"] = _digest(ledger_record)
            previous = str(ledger_record["record_sha256"])
            ledger_records.append(ledger_record)
        ledger_path = output_root / "campaign-ledger.jsonl"
        with ledger_path.open("xb") as output:
            for record in ledger_records:
                output.write(_canonical(record))
        campaign_summary = campaign_contracts.validate_campaign_ledger(
            plan, ledger_records
        )
        _write_exclusive_json(output_root / "campaign-summary.json", campaign_summary)
        campaign_seal = create_evidence_seal(output_root)
        campaign_validation = _pending_external_provenance(
            output_root,
            seal=campaign_seal,
            children=[
                {
                    "pair_id": validation["pair_id"],
                    "verdict": validation["verdict"],
                    "pending_external_provenance": validation[
                        "pending_external_provenance"
                    ],
                }
                for validation in pair_validations
            ],
        )

    validation_status = "PASS"
    child_verdicts = [record["validation"].get("verdict") for record in records]
    aggregate_verdicts = [record.get("verdict") for record in pair_validations]
    if campaign_validation is not None:
        aggregate_verdicts.append(campaign_validation.get("verdict"))
    if "FAIL" in child_verdicts or "FAIL" in aggregate_verdicts:
        validation_status = "FAIL"
    elif any(verdict != "PASS" for verdict in child_verdicts):
        validation_status = "PROVISIONAL"
    elif any(verdict != "PASS" for verdict in aggregate_verdicts):
        validation_status = "INCOMPLETE"

    result = {
        "schema_version": 1,
        "mode": invocation["mode"],
        "pair_count": pair_count,
        "automatic_retries": 0,
        "replacement_policy": "none",
        "validation_status": validation_status,
        "trusted_provenance_required": True,
        "children": [
            {
                key: value
                for key, value in record.items()
                if key not in {"seal", "configuration"}
            }
            for record in records
        ],
        "pair_validations": pair_validations,
    }
    if campaign_validation is not None:
        result["campaign_validation"] = dict(campaign_validation)
    return result


def run_focused_pair(**kwargs: object) -> Mapping[str, object]:
    """Execute one authorized matched pair through the injectable backend."""

    return _execute_focused(kwargs, backend=FOCUSED_LAUNCH_BACKEND)


def run_focused_campaign(**kwargs: object) -> Mapping[str, object]:
    """Execute five authorized matched pairs through the injectable backend."""

    return _execute_focused(kwargs, backend=FOCUSED_LAUNCH_BACKEND)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument(
        "--mode", choices=("smoke", "pair", "campaign"), required=True
    )
    preflight.add_argument("--profile", type=Path)
    preflight.add_argument("--pairs", type=int, required=True)
    preflight.add_argument("--output", type=Path, required=True)

    for command in ("smoke", "pair", "campaign"):
        execute = subparsers.add_parser(command)
        execute.add_argument("--profile", type=Path)
        execute.add_argument("--pairs", type=int, required=True)
        execute.add_argument("--preflight-receipt", type=Path, required=True)
        execute.add_argument("--authorization-receipt", type=Path, required=True)
        execute.add_argument("--output", type=Path, required=True)
        execute.add_argument("--retries", type=int, default=0)

    pair = subparsers.add_parser("validate-pair")
    pair.add_argument("--pair-root", type=Path, required=True)
    pair.add_argument("--trusted-provenance", type=Path, required=True)
    campaign = subparsers.add_parser("validate-campaign")
    campaign.add_argument("--campaign-root", type=Path, required=True)
    campaign.add_argument("--trusted-provenance", type=Path, required=True)
    return parser


def _require_mode_profile(profile: FocusedProfile, mode: str) -> None:
    canonical_path = _V12_PROFILES.get(mode)
    if canonical_path is None:
        raise FocusedCrashPairCliError("execution mode has no frozen v12 profile")
    canonical = load_focused_profile(canonical_path)
    if (
        profile.profile_id,
        profile.profile_sha256,
        profile.topology_proof_sha256,
    ) != (
        canonical.profile_id,
        canonical.profile_sha256,
        canonical.topology_proof_sha256,
    ):
        raise FocusedCrashPairCliError(
            "execution mode is not bound to the required frozen profile"
        )


def _profile_path(profile: Path | None, mode: str) -> Path:
    """Use the immutable v12 profile; archived paths fail exact binding."""

    if mode not in _V12_PROFILES:
        raise FocusedCrashPairCliError("execution mode has no frozen v12 profile")
    return _V12_PROFILES[mode] if profile is None else profile


def _authorized_execution(
    arguments: argparse.Namespace,
) -> tuple[FocusedProfile, Mapping[str, Any], Mapping[str, Any]]:
    expected_pairs = 5 if arguments.command == "campaign" else 1
    if arguments.pairs != expected_pairs or arguments.retries != 0:
        raise FocusedCrashPairCliError(
            "execution requires exact pair cardinality and zero retries"
        )
    output = arguments.output.resolve()
    if output.exists():
        raise FocusedCrashPairCliError("allocated result root already exists")
    profile = load_focused_profile(_profile_path(arguments.profile, arguments.command))
    _require_mode_profile(profile, arguments.command)
    preflight = _read_json(arguments.preflight_receipt, "preflight receipt")
    authorization = _read_json(arguments.authorization_receipt, "authorization receipt")
    request = build_focused_authorization_request(preflight)
    verify_focused_authorization_receipt(request, authorization)
    request_document = json.loads(request)
    expected = {
        "mode": arguments.command,
        "pair_count": expected_pairs,
        "profile_sha256": profile.profile_sha256,
        "topology_proof_sha256": profile.topology_proof_sha256,
        "output_root": str(output),
        "automatic_retries": 0,
        "replacement_policy": "none",
    }
    if any(request_document.get(key) != value for key, value in expected.items()):
        raise FocusedCrashPairCliError(
            "receipts are replayed or do not authorize this exact invocation"
        )
    return profile, preflight, authorization


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "preflight":
            expected_pairs = 5 if arguments.mode == "campaign" else 1
            if arguments.pairs != expected_pairs or arguments.output.resolve().exists():
                raise FocusedCrashPairCliError(
                    "preflight pair cardinality or output allocation is invalid"
                )
            profile = load_focused_profile(
                _profile_path(arguments.profile, arguments.mode)
            )
            _require_mode_profile(profile, arguments.mode)
            result = prepare_focused_preflight(
                profile=profile,
                mode=arguments.mode,
                pair_count=arguments.pairs,
                output_root=arguments.output,
                checks=FOCUSED_PREFLIGHT_CHECKS,
            )
        elif arguments.command in {"smoke", "pair", "campaign"}:
            profile, preflight, authorization = _authorized_execution(arguments)
            pair_issuers = (
                reload_pair_issuer_allocations(
                    preflight=preflight,
                    authorization=authorization,
                )
                if isinstance(preflight.get("execution_context"), Mapping)
                and "pair_issuers" in preflight["execution_context"]
                else None
            )
            invocation = {
                "profile": profile,
                "preflight_receipt": preflight,
                "authorization_receipt": authorization,
                "pair_count": arguments.pairs,
                "output_root": arguments.output.resolve(),
                "mode": arguments.command,
                "automatic_retries": 0,
                "replacement_policy": "none",
            }
            if pair_issuers is not None:
                invocation["pair_issuer_allocations"] = pair_issuers
            result = (
                run_focused_campaign(**invocation)
                if arguments.command == "campaign"
                else run_focused_pair(**invocation)
            )
        elif arguments.command == "validate-pair":
            result = validate_sealed_pair(
                pair_directory=arguments.pair_root,
                trusted_provenance=_read_json(
                    arguments.trusted_provenance, "trusted provenance"
                ),
            )
        else:
            result = validate_sealed_campaign(
                campaign_directory=arguments.campaign_root,
                trusted_provenance=_read_json(
                    arguments.trusted_provenance, "trusted provenance"
                ),
            )
    except (
        FocusedCrashPairCliError,
        FocusedCrashPairRuntimeError,
        FocusedCrashPairValidationError,
    ) as exc:
        parser.error(str(exc))
    _write_cli_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
