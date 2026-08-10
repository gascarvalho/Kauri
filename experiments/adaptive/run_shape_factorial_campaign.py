#!/usr/bin/env python3
"""Plan, execute once, or independently validate the frozen SHAPE31 campaign."""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time
from typing import Any, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.adaptive.kauri_experiment.factorial_execution import (  # noqa: E402
    CAMPAIGN_AUTHORIZATION_FILENAME,
    CAMPAIGN_CONTRACT_FILENAME,
    CAMPAIGN_LEDGER_FILENAME,
    CAMPAIGN_SUMMARY_FILENAME,
    COVERAGE_SMOKE_AUTHORIZATION_FILENAME,
    COVERAGE_SMOKE_CONTRACT_FILENAME,
    COVERAGE_SMOKE_LEDGER_FILENAME,
    COVERAGE_SMOKE_LEDGER_PREFIX_FILENAME,
    COVERAGE_SMOKE_PREDECESSOR_RECEIPT_FILENAME,
    ExecutionPreflight,
    FactorialExecutionError,
    N31CoverageSmokeRuntime,
    N31CoverageSmokeSlot,
    SlotExecutionResult,
    append_campaign_ledger_record,
    append_coverage_smoke_ledger_record,
    build_campaign_execution_contract,
    build_coverage_smoke_execution_contract,
    build_coverage_smoke_started_record,
    build_coverage_smoke_terminal_record,
    build_execution_authorization_receipt,
    build_n31_coverage_smoke_slot,
    build_n7_ps_smoke_slot,
    execute_slot_once,
    coverage_smoke_previous_record_sha256,
    preserve_build_evidence,
    publish_campaign_summary,
    verify_completed_coverage_smoke_sequence,
    verify_evidence_preflight,
)
from experiments.adaptive.kauri_experiment.factorial_manifest import (  # noqa: E402
    FROZEN_MANIFEST_ID,
    FROZEN_MANIFEST_SHA256,
    FROZEN_PLAN_SHA256,
    V25_MANIFEST_ID,
    V26_MANIFEST_ID,
    V27_MANIFEST_ID,
    V28_MANIFEST_ID,
    V29_MANIFEST_ID,
    V30_MANIFEST_ID,
    V31_MANIFEST_ID,
    V32_MANIFEST_ID,
    FactorialManifestError,
    FactorialPlan,
    FactorialSlot,
    FrozenFactorialManifest,
    build_factorial_plan,
    load_frozen_manifest,
)
from experiments.adaptive.kauri_experiment.factorial_runtime import (  # noqa: E402
    FROZEN_COVERAGE_SMOKE_RUNTIME_SHA256,
    FROZEN_RUNTIME_SHA256,
    FROZEN_SMOKE_RUNTIME_SHA256,
    FactorialRuntimePlan,
    SlotRuntimeSpec,
    build_factorial_runtime,
    canonical_runtime_bytes,
    runtime_preflight,
)
from experiments.adaptive.kauri_experiment.factorial_validation import (  # noqa: E402
    SlotValidationResult,
    validate_campaign,
    validate_slot,
)
from experiments.adaptive.kauri_experiment.profiled_fault_runtime import (  # noqa: E402
    BUILD_PROVENANCE_FILENAME,
)


DEFAULT_MANIFEST = (
    Path(__file__).resolve().parent / "profiles/shape-placement-factorial-v33.json"
)
REPOSITORY = Path(__file__).resolve().parents[2]
SMOKE_AUTHORIZATION_FILENAME = "smoke-execution-authorization.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "plan",
            "preflight",
            "smoke",
            "coverage-smoke",
            "run",
            "validate-smoke",
            "validate-coverage-smoke",
            "validate-campaign",
        ),
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--repository", type=Path, default=REPOSITORY)
    parser.add_argument("--build-directory", type=Path)
    parser.add_argument("--build-provenance", type=Path)
    parser.add_argument("--campaign-results-root", type=Path)
    parser.add_argument("--smoke-results-root", type=Path)
    parser.add_argument("--coverage-smoke-results-root", type=Path)
    parser.add_argument(
        "--preflight-target",
        choices=("campaign", "smoke", "coverage-smoke"),
        default="campaign",
    )
    authorization = parser.add_mutually_exclusive_group()
    authorization.add_argument(
        "--approval-reference",
        help="explicit thesis-author approval reference used to create a receipt",
    )
    authorization.add_argument(
        "--authorization-receipt",
        type=Path,
        help="path to an existing exact canonical authorization receipt",
    )
    parser.add_argument(
        "--approved-utc",
        help="UTC timestamp for --approval-reference (defaults to current UTC)",
    )
    return parser


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as error:
        raise FactorialExecutionError("campaign artifact is not canonical JSON") from error


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _emit(value: object, *, stream) -> None:
    print(_canonical_json_bytes(value).decode("ascii"), end="", file=stream)


def _append_canonical_jsonl(path: Path, value: object) -> None:
    append_campaign_ledger_record(path, value)


def _direct_runtime_bytes(
    spec: SlotRuntimeSpec | N31CoverageSmokeRuntime,
) -> bytes:
    return _canonical_json_bytes(spec.as_document())


def _n31_coverage_smoke(plan: FactorialPlan) -> N31CoverageSmokeSlot:
    primary = next(
        (slot for slot in plan.slots if slot.execution_ordinal == 1),
        None,
    )
    if primary is None:
        raise FactorialExecutionError(
            "coverage smoke requires the exact first campaign slot"
        )
    repair = (
        next(
            (slot for slot in plan.slots if slot.execution_ordinal == 5),
            None,
        )
        if plan.manifest_id
        in {
            V25_MANIFEST_ID,
            V26_MANIFEST_ID,
            V27_MANIFEST_ID,
            V28_MANIFEST_ID,
            V29_MANIFEST_ID,
            V30_MANIFEST_ID,
            V31_MANIFEST_ID,
            V32_MANIFEST_ID,
            FROZEN_MANIFEST_ID,
        }
        else None
    )
    return build_n31_coverage_smoke_slot(
        primary,
        repair_template=repair,
    )


def _coverage_smoke_slot_ids(manifest_id: str) -> tuple[str, ...]:
    primary = "slot-066-n31-f5-b05-P"
    if manifest_id in {
        V25_MANIFEST_ID,
        V26_MANIFEST_ID,
        V27_MANIFEST_ID,
        V28_MANIFEST_ID,
        V29_MANIFEST_ID,
        V30_MANIFEST_ID,
        V31_MANIFEST_ID,
        V32_MANIFEST_ID,
        FROZEN_MANIFEST_ID,
    }:
        return (primary, "slot-037-n31-f2-b04-00")
    return (primary,)


def _static_artifacts(
    manifest_path: Path,
    plan: FactorialPlan,
    runtime_payload: bytes,
) -> dict[str, bytes]:
    return {
        "manifest.json": manifest_path.read_bytes(),
        "plan.json": plan.canonical_bytes,
        "runtime.json": runtime_payload,
    }


def _resolve_result_roots(
    arguments: argparse.Namespace,
    *,
    manifest_id: str,
    results_root: str,
) -> tuple[Path, Path, Path, Path, Path, Path, Path]:
    repository = arguments.repository.resolve()
    campaign_root = (
        arguments.campaign_results_root.absolute()
        if arguments.campaign_results_root is not None
        else repository / results_root
    )
    manifest_suffix = manifest_id.removeprefix("shape-placement-factorial-")
    smoke_results_relative = Path(
        f"results/shape-placement-factorial-{manifest_suffix}-smoke"
    )
    smoke_root = (
        arguments.smoke_results_root.absolute()
        if arguments.smoke_results_root is not None
        else repository / smoke_results_relative
    )
    coverage_smoke_results_relative = Path(
        f"results/shape-placement-factorial-{manifest_suffix}-coverage-smoke"
    )
    coverage_smoke_root = (
        arguments.coverage_smoke_results_root.absolute()
        if arguments.coverage_smoke_results_root is not None
        else repository / coverage_smoke_results_relative
    )
    expected_campaign_root = (repository / results_root).absolute()
    expected_smoke_root = (repository / smoke_results_relative).absolute()
    expected_coverage_smoke_root = (
        repository / coverage_smoke_results_relative
    ).absolute()
    if campaign_root.is_symlink():
        raise FactorialExecutionError("campaign results root must not be a symlink")
    if smoke_root.is_symlink():
        raise FactorialExecutionError("smoke results root must not be a symlink")
    if coverage_smoke_root.is_symlink():
        raise FactorialExecutionError(
            "coverage-smoke results root must not be a symlink"
        )
    return (
        repository,
        campaign_root,
        smoke_root,
        coverage_smoke_root,
        expected_campaign_root,
        expected_smoke_root,
        expected_coverage_smoke_root,
    )


def _paths(
    arguments: argparse.Namespace,
    runtime: FactorialRuntimePlan,
) -> tuple[Path, Path, Path, Path, Path, Path]:
    (
        repository,
        campaign_root,
        smoke_root,
        coverage_smoke_root,
        expected_campaign_root,
        expected_smoke_root,
        expected_coverage_smoke_root,
    ) = _resolve_result_roots(
        arguments,
        manifest_id=runtime.manifest_id,
        results_root=runtime.results_root,
    )
    build_directory = (
        arguments.build_directory.resolve()
        if arguments.build_directory is not None
        else repository / "build-adaptive"
    )
    build_provenance = (
        arguments.build_provenance.resolve()
        if arguments.build_provenance is not None
        else build_directory / BUILD_PROVENANCE_FILENAME
    )
    expected_build_directory = (repository / "build-adaptive").resolve()
    expected_build_provenance = expected_build_directory / BUILD_PROVENANCE_FILENAME
    if build_directory != expected_build_directory:
        raise FactorialExecutionError(
            f"build directory must be exact: {expected_build_directory}"
        )
    if build_provenance != expected_build_provenance:
        raise FactorialExecutionError(
            f"build provenance path must be exact: {expected_build_provenance}"
        )
    if campaign_root != expected_campaign_root:
        raise FactorialExecutionError(
            f"campaign results root must be exact: {expected_campaign_root}"
        )
    if smoke_root != expected_smoke_root:
        raise FactorialExecutionError(
            f"smoke results root must be exact: {expected_smoke_root}"
        )
    if coverage_smoke_root != expected_coverage_smoke_root:
        raise FactorialExecutionError(
            "coverage-smoke results root must be exact: "
            f"{expected_coverage_smoke_root}"
        )
    return (
        repository,
        build_directory,
        build_provenance,
        campaign_root,
        smoke_root,
        coverage_smoke_root,
    )


def _validation_roots(
    arguments: argparse.Namespace,
    manifest: FrozenFactorialManifest,
) -> tuple[Path, Path, Path]:
    """Resolve preserved roots without deriving a new plan or runtime."""

    (
        _,
        campaign_root,
        smoke_root,
        coverage_smoke_root,
        expected_campaign_root,
        expected_smoke_root,
        expected_coverage_smoke_root,
    ) = _resolve_result_roots(
        arguments,
        manifest_id=manifest.manifest_id,
        results_root=manifest.results_root,
    )
    if (
        arguments.command != "validate-campaign"
        and campaign_root != expected_campaign_root
    ):
        raise FactorialExecutionError(
            f"campaign results root must be exact: {expected_campaign_root}"
        )
    if arguments.command != "validate-smoke" and smoke_root != expected_smoke_root:
        raise FactorialExecutionError(
            f"smoke results root must be exact: {expected_smoke_root}"
        )
    if (
        arguments.command != "validate-coverage-smoke"
        and coverage_smoke_root != expected_coverage_smoke_root
    ):
        raise FactorialExecutionError(
            "coverage-smoke results root must be exact: "
            f"{expected_coverage_smoke_root}"
        )
    return campaign_root, smoke_root, coverage_smoke_root


def _require_frozen_artifacts(
    manifest: FrozenFactorialManifest,
    plan: FactorialPlan,
    runtime: FactorialRuntimePlan,
) -> bytes:
    """Fail before any result claim if producer bytes drift from v33."""

    if (
        manifest.manifest_id != FROZEN_MANIFEST_ID
        or manifest.manifest_sha256 != FROZEN_MANIFEST_SHA256
    ):
        raise FactorialExecutionError("campaign production requires exact frozen v33")
    if plan.plan_sha256 != FROZEN_PLAN_SHA256:
        raise FactorialExecutionError(
            "campaign plan bytes differ from the exact frozen v33 identity"
        )
    payload = canonical_runtime_bytes(runtime)
    if _sha256(payload) != FROZEN_RUNTIME_SHA256:
        raise FactorialExecutionError(
            "campaign runtime bytes differ from the exact frozen v33 identity"
        )
    return payload


def _slot_by_runtime_order(
    plan: FactorialPlan,
    runtime: FactorialRuntimePlan,
) -> tuple[tuple[FactorialSlot, SlotRuntimeSpec], ...]:
    planned = {slot.slot_id: slot for slot in plan.slots}
    ordered: list[tuple[FactorialSlot, SlotRuntimeSpec]] = []
    for expected_ordinal, spec in enumerate(runtime.slots, start=1):
        slot = planned.get(spec.slot_id)
        if slot is None or slot.execution_ordinal != expected_ordinal:
            raise FactorialExecutionError("campaign execution order drifted")
        ordered.append((slot, spec))
    return tuple(ordered)


def _preflight(
    slot: FactorialSlot,
    *,
    repository: Path,
    build_directory: Path,
    build_provenance: Path,
    result_root: Path,
    minimum_free_bytes: int,
) -> ExecutionPreflight:
    return verify_evidence_preflight(
        slot,
        repository=repository,
        build_directory=build_directory,
        build_provenance_path=build_provenance,
        result_root=result_root,
        minimum_free_bytes=minimum_free_bytes,
    )


def _authorization_document(payload: bytes) -> Mapping[str, Any]:
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FactorialExecutionError(
            "execution authorization receipt is invalid JSON"
        ) from error
    if not isinstance(document, dict) or _canonical_json_bytes(document) != payload:
        raise FactorialExecutionError(
            "execution authorization receipt is not exact canonical JSON"
        )
    return document


def _authorization_receipt(
    arguments: argparse.Namespace,
    *,
    scope: str,
    preflight: ExecutionPreflight,
    slot_ids: Sequence[str],
    static_artifacts: Mapping[str, bytes],
) -> tuple[bytes, Mapping[str, Any]]:
    if arguments.approval_reference is None and arguments.authorization_receipt is None:
        raise FactorialExecutionError(
            "launch requires --approval-reference or --authorization-receipt"
        )
    if arguments.authorization_receipt is not None:
        supplied_path = arguments.authorization_receipt
        if supplied_path.is_symlink():
            raise FactorialExecutionError(
                "execution authorization receipt must be a regular non-symlink file"
            )
        path = supplied_path.resolve()
        if not path.is_file():
            raise FactorialExecutionError(
                "execution authorization receipt must be a regular non-symlink file"
            )
        payload = path.read_bytes()
        document = _authorization_document(payload)
        try:
            expected = build_execution_authorization_receipt(
                scope=scope,
                approval_reference=document["approval_reference"],
                approved_utc=document["approved_utc"],
                kauri_revision=preflight.revision,
                slot_ids=slot_ids,
                result_root=preflight.result_root.relative_to(
                    preflight.repository
                ).as_posix(),
                static_artifacts=static_artifacts,
                build_provenance_sha256=_sha256(
                    _canonical_json_bytes(preflight.build_provenance)
                ),
            )
        except (KeyError, TypeError) as error:
            raise FactorialExecutionError(
                "execution authorization receipt schema drifted"
            ) from error
        if expected != payload:
            raise FactorialExecutionError(
                "execution authorization receipt is not bound to this exact revision, "
                "scope, slot set, and static contract"
            )
        return payload, document
    approved_utc = arguments.approved_utc or _utc_now()
    payload = build_execution_authorization_receipt(
        scope=scope,
        approval_reference=arguments.approval_reference,
        approved_utc=approved_utc,
        kauri_revision=preflight.revision,
        slot_ids=slot_ids,
        result_root=preflight.result_root.relative_to(
            preflight.repository
        ).as_posix(),
        static_artifacts=static_artifacts,
        build_provenance_sha256=_sha256(
            _canonical_json_bytes(preflight.build_provenance)
        ),
    )
    return payload, _authorization_document(payload)


def _require_authorization_input(arguments: argparse.Namespace) -> None:
    if arguments.approval_reference is None and arguments.authorization_receipt is None:
        raise FactorialExecutionError(
            "launch is not authorized; --approval-reference or "
            "--authorization-receipt is required"
        )
    if arguments.authorization_receipt is not None and arguments.approved_utc is not None:
        raise FactorialExecutionError(
            "--approved-utc is valid only with --approval-reference"
        )


def _require_fresh_result_root(path: Path, label: str) -> None:
    if path.exists() or path.is_symlink():
        raise FactorialExecutionError(
            f"{label} result root already exists; retries and replacement are forbidden: {path}"
        )


def _require_validated_smoke(
    smoke_root: Path,
    *,
    expected_revision: str,
    expected_build_provenance: Mapping[str, object],
    expected_static_artifacts_sha256: Mapping[str, str],
) -> SlotValidationResult:
    slot_root = smoke_root / "smoke-n7-f2-PS"
    result = validate_slot(slot_root)
    if not (
        result.outcome == "PASS"
        and result.integrity_valid
        and not result.campaign_member
        and not result.figure_eligible
    ):
        raise FactorialExecutionError(
            "campaign launch requires the canonical excluded N=7 smoke to "
            "independently validate PASS"
        )
    authorization, _ = _read_canonical_json(
        slot_root / "execution-authorization.json", "smoke execution authorization"
    )
    build_provenance, _ = _read_canonical_json(
        slot_root / "runtime/exact-build-provenance.json", "smoke build provenance"
    )
    if (
        authorization.get("kauri_revision") != expected_revision
        or build_provenance != dict(expected_build_provenance)
    ):
        raise FactorialExecutionError(
            "campaign launch requires the passing N=7 smoke from this exact "
            "revision and build provenance"
        )
    if authorization.get("static_artifacts_sha256") != dict(
        expected_static_artifacts_sha256
    ):
        raise FactorialExecutionError(
            "campaign launch requires the passing N=7 smoke from the exact "
            "frozen static artifacts"
        )
    return result


def _require_validated_coverage_smoke(
    coverage_smoke_root: Path,
    *,
    expected_revision: str,
    expected_build_provenance: Mapping[str, object],
    expected_static_artifacts_sha256: Mapping[str, str],
    expected_slot_ids: Sequence[str] = ("slot-066-n31-f5-b05-P",),
    expected_runtime: N31CoverageSmokeRuntime | None = None,
    expected_static_artifacts: Mapping[str, bytes] | None = None,
) -> SlotValidationResult:
    slot_ids = tuple(expected_slot_ids)
    if not slot_ids or len(set(slot_ids)) != len(slot_ids):
        raise FactorialExecutionError(
            "coverage-smoke validation slot sequence is not exact"
        )
    common_authorization_payload: bytes | None = None
    result: SlotValidationResult | None = None
    validations: list[SlotValidationResult] = []
    for slot_id in slot_ids:
        slot_root = coverage_smoke_root / slot_id
        result = validate_slot(slot_root)
        if not (
            result.outcome == "PASS"
            and result.integrity_valid
            and not result.campaign_member
            and not result.figure_eligible
        ):
            raise FactorialExecutionError(
                "campaign launch requires every canonical excluded N=31 "
                "coverage-smoke slot to independently validate PASS"
            )
        authorization, authorization_payload = _read_canonical_json(
            slot_root / "execution-authorization.json",
            "coverage-smoke execution authorization",
        )
        build_provenance, _ = _read_canonical_json(
            slot_root / "runtime/exact-build-provenance.json",
            "coverage-smoke build provenance",
        )
        if (
            authorization.get("kauri_revision") != expected_revision
            or authorization.get("slot_ids") != list(slot_ids)
            or build_provenance != dict(expected_build_provenance)
        ):
            raise FactorialExecutionError(
                "campaign launch requires every passing N=31 coverage-smoke "
                "slot from this exact revision and build provenance under one "
                "authorization"
            )
        if authorization.get("static_artifacts_sha256") != dict(
            expected_static_artifacts_sha256
        ):
            raise FactorialExecutionError(
                "campaign launch requires every passing N=31 coverage-smoke "
                "slot from the exact frozen static artifacts"
            )
        if (
            common_authorization_payload is not None
            and authorization_payload != common_authorization_payload
        ):
            raise FactorialExecutionError(
                "coverage-smoke slots do not share one exact authorization"
            )
        common_authorization_payload = authorization_payload
        validations.append(result)
    assert result is not None
    if len(slot_ids) == 2:
        if (
            not isinstance(expected_runtime, N31CoverageSmokeRuntime)
            or expected_static_artifacts is None
            or common_authorization_payload is None
        ):
            raise FactorialExecutionError(
                "v33 coverage-smoke gate lacks its ordered runtime/static contract"
            )
        _require_completed_coverage_smoke_sequence(
            coverage_smoke_root,
            runtime=expected_runtime,
            static_artifacts=expected_static_artifacts,
            authorization_payload=common_authorization_payload,
            build_provenance=expected_build_provenance,
            validations=validations,
        )
    return result


def _require_completed_coverage_smoke_sequence(
    root: Path,
    *,
    runtime: N31CoverageSmokeRuntime,
    static_artifacts: Mapping[str, bytes],
    authorization_payload: bytes,
    build_provenance: Mapping[str, object],
    validations: Sequence[SlotValidationResult],
) -> None:
    """Replay the exact completed two-slot ledger and both sealed prefixes."""

    replayed = verify_completed_coverage_smoke_sequence(
        root,
        runtime=runtime,
        static_artifacts=static_artifacts,
        authorization_payload=authorization_payload,
        build_provenance=build_provenance,
        require_canonical_root=True,
    )
    if len(replayed) != len(validations) or any(
        _validation_document(actual) != _validation_document(expected)
        for actual, expected in zip(replayed, validations, strict=True)
    ):
        raise FactorialExecutionError(
            "coverage-smoke completed replay differs from the gate validations"
        )


def _validation_document(result: SlotValidationResult) -> dict[str, object]:
    return {
        "outcome": result.outcome,
        "reason": result.reason,
        "integrity_valid": result.integrity_valid,
        "campaign_member": result.campaign_member,
        "figure_eligible": result.figure_eligible,
    }


def _validate_attempt(path: Path, *, campaign_member: bool) -> SlotValidationResult:
    try:
        return validate_slot(path)
    except (Exception, KeyboardInterrupt) as error:
        return SlotValidationResult(
            slot_id=path.name,
            outcome="INCOMPLETE",
            reason=f"independent validator raised {type(error).__name__}: {error}",
            integrity_valid=False,
            campaign_member=campaign_member,
        )


def _campaign_contract(
    runtime: FactorialRuntimePlan,
    static_artifacts: Mapping[str, bytes],
    authorization: Mapping[str, Any],
    authorization_payload: bytes,
    build_provenance: Mapping[str, object],
) -> dict[str, object]:
    return build_campaign_execution_contract(
        runtime=runtime,
        static_artifacts=static_artifacts,
        authorization=authorization,
        authorization_payload=authorization_payload,
        build_provenance=build_provenance,
    )


def _ledger_common(
    *,
    runtime: FactorialRuntimePlan,
    spec: SlotRuntimeSpec,
    authorization: Mapping[str, Any],
    authorization_payload: bytes,
    contract_sha256: str,
) -> dict[str, object]:
    static_hashes = authorization["static_artifacts_sha256"]
    if not isinstance(static_hashes, Mapping):
        raise FactorialExecutionError("authorization static hashes are malformed")
    return {
        "schema_version": 1,
        "campaign_id": runtime.runtime_id,
        "manifest_sha256": runtime.manifest_sha256,
        "source_plan_sha256": runtime.source_plan_sha256,
        "runtime_sha256": static_hashes["runtime.json"],
        "contract_sha256": contract_sha256,
        "authorization_id": authorization["authorization_id"],
        "authorization_sha256": _sha256(authorization_payload),
        "kauri_revision": authorization["kauri_revision"],
        "execution_ordinal": spec.execution_ordinal,
        "slot_id": spec.slot_id,
        "block_id": spec.block_id,
        "arm_code": spec.arm_code,
        "attempt_ordinal": 1,
        "automatic_retries": 0,
        "replacement_policy": "none",
    }


def _started_record(
    *,
    runtime: FactorialRuntimePlan,
    spec: SlotRuntimeSpec,
    preflight: ExecutionPreflight,
    authorization: Mapping[str, Any],
    authorization_payload: bytes,
    contract_sha256: str,
) -> dict[str, object]:
    return {
        **_ledger_common(
            runtime=runtime,
            spec=spec,
            authorization=authorization,
            authorization_payload=authorization_payload,
            contract_sha256=contract_sha256,
        ),
        "state": "STARTED",
        "recorded_utc": _utc_now(),
        "recorded_monotonic_ns": time.monotonic_ns(),
        "preflight_revision": preflight.revision,
        "preflight_free_bytes": preflight.free_bytes,
        "slot_directory": str(preflight.slot_directory),
        "build_provenance_sha256": _sha256(
            _canonical_json_bytes(preflight.build_provenance)
        ),
    }


def _terminal_record(
    *,
    runtime: FactorialRuntimePlan,
    spec: SlotRuntimeSpec,
    execution: SlotExecutionResult,
    validation: SlotValidationResult,
    authorization: Mapping[str, Any],
    authorization_payload: bytes,
    contract_sha256: str,
) -> dict[str, object]:
    return {
        **_ledger_common(
            runtime=runtime,
            spec=spec,
            authorization=authorization,
            authorization_payload=authorization_payload,
            contract_sha256=contract_sha256,
        ),
        "state": "TERMINAL",
        "recorded_utc": _utc_now(),
        "recorded_monotonic_ns": time.monotonic_ns(),
        "slot_directory": str(execution.slot_directory),
        "execution_outcome": execution.outcome,
        "execution_reason": execution.reason,
        "launch_count": execution.launch_count,
        "validation": _validation_document(validation),
    }


def _campaign_summary(
    *,
    runtime: FactorialRuntimePlan,
    root: Path,
    authorization: Mapping[str, Any],
    authorization_payload: bytes,
    contract_payload: bytes,
    attempted_count: int,
    stopped_reason: str | None,
) -> dict[str, object]:
    complete = (
        attempted_count == len(runtime.slots)
        and stopped_reason is None
    )
    return {
        "schema_version": 1,
        "campaign_id": runtime.runtime_id,
        "authorization_id": authorization["authorization_id"],
        "authorization_sha256": _sha256(authorization_payload),
        "contract_sha256": _sha256(contract_payload),
        "ledger_sha256": _sha256((root / CAMPAIGN_LEDGER_FILENAME).read_bytes()),
        "expected_slot_count": len(runtime.slots),
        "attempted_slot_count": attempted_count,
        "next_execution_ordinal": (
            None if attempted_count == len(runtime.slots) else attempted_count + 1
        ),
        "execution_complete": complete,
        "stopped_reason": stopped_reason,
        "completed_utc": _utc_now(),
    }


def _run_smoke(
    arguments: argparse.Namespace,
    *,
    plan: FactorialPlan,
    runtime: FactorialRuntimePlan,
    repository: Path,
    build_directory: Path,
    build_provenance: Path,
    smoke_root: Path,
) -> tuple[int, dict[str, object]]:
    _require_authorization_input(arguments)
    smoke = build_n7_ps_smoke_slot(plan.slots[0])
    smoke_runtime_payload = _direct_runtime_bytes(smoke.runtime)
    if _sha256(smoke_runtime_payload) != FROZEN_SMOKE_RUNTIME_SHA256:
        raise FactorialExecutionError(
            "smoke runtime bytes differ from the exact frozen v33 identity"
        )
    _require_fresh_result_root(smoke_root, "smoke")
    artifacts = _static_artifacts(
        arguments.manifest.resolve(),
        plan,
        smoke_runtime_payload,
    )
    preflight = _preflight(
        smoke.slot,
        repository=repository,
        build_directory=build_directory,
        build_provenance=build_provenance,
        result_root=smoke_root,
        minimum_free_bytes=runtime.minimum_free_bytes,
    )
    authorization_payload, authorization = _authorization_receipt(
        arguments,
        scope="excluded_n7_smoke",
        preflight=preflight,
        slot_ids=(smoke.slot.slot_id,),
        static_artifacts=artifacts,
    )
    preserve_build_evidence(
        smoke_root,
        preflight.build_provenance,
        initial_files={SMOKE_AUTHORIZATION_FILENAME: authorization_payload},
    )
    execution = execute_slot_once(
        smoke.slot,
        smoke.runtime,
        preflight=preflight,
        static_artifacts=artifacts,
        authorization_receipt=authorization_payload,
        campaign_member=False,
    )
    validation = validate_slot(execution.slot_directory)
    accepted = (
        execution.outcome == "PASS"
        and validation.outcome == "PASS"
        and validation.integrity_valid
        and not validation.campaign_member
        and not validation.figure_eligible
    )
    return (
        0 if accepted else 1,
        {
            "command": "smoke",
            "slot_directory": str(execution.slot_directory),
            "execution_outcome": execution.outcome,
            "execution_reason": execution.reason,
            "validation": _validation_document(validation),
            "authorization_id": authorization["authorization_id"],
            "authorization_sha256": _sha256(authorization_payload),
            "campaign_member": False,
            "figure_eligible": False,
        },
    )


def _run_coverage_smoke(
    arguments: argparse.Namespace,
    *,
    plan: FactorialPlan,
    runtime: FactorialRuntimePlan,
    repository: Path,
    build_directory: Path,
    build_provenance: Path,
    smoke_root: Path,
    coverage_smoke_root: Path,
) -> tuple[int, dict[str, object]]:
    _require_authorization_input(arguments)
    coverage = _n31_coverage_smoke(plan)
    coverage_runtime_payload = _direct_runtime_bytes(coverage.runtime)
    if _sha256(coverage_runtime_payload) != FROZEN_COVERAGE_SMOKE_RUNTIME_SHA256:
        raise FactorialExecutionError(
            "coverage-smoke runtime bytes differ from the exact frozen v33 identity"
        )
    _require_fresh_result_root(coverage_smoke_root, "coverage-smoke")
    artifacts = _static_artifacts(
        arguments.manifest.resolve(),
        plan,
        coverage_runtime_payload,
    )
    preflight = _preflight(
        coverage.slot,
        repository=repository,
        build_directory=build_directory,
        build_provenance=build_provenance,
        result_root=coverage_smoke_root,
        minimum_free_bytes=runtime.minimum_free_bytes,
    )
    _require_validated_smoke(
        smoke_root,
        expected_revision=preflight.revision,
        expected_build_provenance=preflight.build_provenance,
        expected_static_artifacts_sha256={
            "manifest.json": FROZEN_MANIFEST_SHA256,
            "plan.json": FROZEN_PLAN_SHA256,
            "runtime.json": FROZEN_SMOKE_RUNTIME_SHA256,
        },
    )
    authorization_payload, authorization = _authorization_receipt(
        arguments,
        scope="excluded_n31_coverage_smoke",
        preflight=preflight,
        slot_ids=tuple(slot.slot_id for slot in coverage.slots),
        static_artifacts=artifacts,
    )
    if not isinstance(coverage.runtime, N31CoverageSmokeRuntime):
        raise FactorialExecutionError(
            "v33 coverage-smoke launch lacks the ordered runtime"
        )
    contract = build_coverage_smoke_execution_contract(
        runtime=coverage.runtime,
        static_artifacts=artifacts,
        authorization=authorization,
        authorization_payload=authorization_payload,
        build_provenance=preflight.build_provenance,
    )
    contract_payload = _canonical_json_bytes(contract)
    preserve_build_evidence(
        coverage_smoke_root,
        preflight.build_provenance,
        initial_files={
            COVERAGE_SMOKE_AUTHORIZATION_FILENAME: authorization_payload,
            COVERAGE_SMOKE_CONTRACT_FILENAME: contract_payload,
        },
    )
    ledger_path = coverage_smoke_root / COVERAGE_SMOKE_LEDGER_FILENAME
    attempts: list[dict[str, object]] = []
    accepted = True
    stopped_reason: str | None = None
    for index, (slot, spec) in enumerate(
        zip(coverage.slots, coverage.runtimes, strict=True)
    ):
        current_preflight = preflight
        if index:
            current_preflight = _preflight(
                slot,
                repository=repository,
                build_directory=build_directory,
                build_provenance=build_provenance,
                result_root=coverage_smoke_root,
                minimum_free_bytes=runtime.minimum_free_bytes,
            )
            if (
                current_preflight.revision != preflight.revision
                or current_preflight.build_provenance
                != preflight.build_provenance
            ):
                raise FactorialExecutionError(
                    "coverage-smoke revision or build provenance drifted "
                    "between ordered slots"
                )
        started = build_coverage_smoke_started_record(
            runtime=coverage.runtime,
            spec=spec,
            coverage_execution_ordinal=index + 1,
            preflight=current_preflight,
            static_artifacts=artifacts,
            authorization=authorization,
            authorization_payload=authorization_payload,
            contract_payload=contract_payload,
            previous_record_sha256=coverage_smoke_previous_record_sha256(
                ledger_path
            ),
            recorded_utc=_utc_now(),
            recorded_monotonic_ns=time.monotonic_ns(),
        )
        append_coverage_smoke_ledger_record(ledger_path, started)
        execution = execute_slot_once(
            slot,
            spec,
            preflight=current_preflight,
            static_artifacts=artifacts,
            authorization_receipt=authorization_payload,
            campaign_member=False,
        )
        validation = _validate_attempt(
            execution.slot_directory,
            campaign_member=False,
        )
        slot_accepted = (
            execution.outcome == "PASS"
            and validation.outcome == "PASS"
            and validation.integrity_valid
            and not validation.campaign_member
            and not validation.figure_eligible
        )
        terminal = build_coverage_smoke_terminal_record(
            runtime=coverage.runtime,
            spec=spec,
            coverage_execution_ordinal=index + 1,
            execution=execution,
            validation=_validation_document(validation),
            static_artifacts=artifacts,
            authorization=authorization,
            authorization_payload=authorization_payload,
            contract_payload=contract_payload,
            build_provenance=current_preflight.build_provenance,
            previous_record_sha256=coverage_smoke_previous_record_sha256(
                ledger_path
            ),
            recorded_utc=_utc_now(),
            recorded_monotonic_ns=time.monotonic_ns(),
        )
        append_coverage_smoke_ledger_record(ledger_path, terminal)
        attempts.append(
            {
                "slot_directory": str(execution.slot_directory),
                "execution_outcome": execution.outcome,
                "execution_reason": execution.reason,
                "validation": _validation_document(validation),
            }
        )
        if not slot_accepted:
            accepted = False
            stopped_reason = (
                f"coverage-smoke slot {slot.slot_id} did not independently "
                "validate PASS"
            )
            break
    return (
        0 if accepted else 1,
        {
            "command": "coverage-smoke",
            "attempted_slot_count": len(attempts),
            "expected_slot_count": len(coverage.slots),
            "attempts": attempts,
            "stopped_reason": stopped_reason,
            "authorization_id": authorization["authorization_id"],
            "authorization_sha256": _sha256(authorization_payload),
            "campaign_member": False,
            "figure_eligible": False,
        },
    )


def _run_campaign(
    arguments: argparse.Namespace,
    *,
    plan: FactorialPlan,
    runtime: FactorialRuntimePlan,
    repository: Path,
    build_directory: Path,
    build_provenance: Path,
    campaign_root: Path,
    smoke_root: Path,
    coverage_smoke_root: Path,
    runtime_payload: bytes,
) -> tuple[int, dict[str, object]]:
    _require_authorization_input(arguments)
    _require_fresh_result_root(campaign_root, "campaign")
    ordered = _slot_by_runtime_order(plan, runtime)
    artifacts = _static_artifacts(
        arguments.manifest.resolve(), plan, runtime_payload
    )

    first_preflight = _preflight(
        ordered[0][0],
        repository=repository,
        build_directory=build_directory,
        build_provenance=build_provenance,
        result_root=campaign_root,
        minimum_free_bytes=runtime.minimum_free_bytes,
    )
    _require_validated_smoke(
        smoke_root,
        expected_revision=first_preflight.revision,
        expected_build_provenance=first_preflight.build_provenance,
        expected_static_artifacts_sha256={
            "manifest.json": FROZEN_MANIFEST_SHA256,
            "plan.json": FROZEN_PLAN_SHA256,
            "runtime.json": FROZEN_SMOKE_RUNTIME_SHA256,
        },
    )
    coverage = _n31_coverage_smoke(plan)
    if not isinstance(coverage.runtime, N31CoverageSmokeRuntime):
        raise FactorialExecutionError(
            "v33 campaign gate lacks the ordered coverage-smoke runtime"
        )
    coverage_artifacts = _static_artifacts(
        arguments.manifest.resolve(),
        plan,
        _direct_runtime_bytes(coverage.runtime),
    )
    _require_validated_coverage_smoke(
        coverage_smoke_root,
        expected_revision=first_preflight.revision,
        expected_build_provenance=first_preflight.build_provenance,
        expected_static_artifacts_sha256={
            "manifest.json": FROZEN_MANIFEST_SHA256,
            "plan.json": FROZEN_PLAN_SHA256,
            "runtime.json": FROZEN_COVERAGE_SMOKE_RUNTIME_SHA256,
        },
        expected_slot_ids=tuple(slot.slot_id for slot in coverage.slots),
        expected_runtime=coverage.runtime,
        expected_static_artifacts=coverage_artifacts,
    )
    authorization_payload, authorization = _authorization_receipt(
        arguments,
        scope="shape25_campaign",
        preflight=first_preflight,
        slot_ids=tuple(slot.slot_id for slot in plan.slots),
        static_artifacts=artifacts,
    )
    contract = _campaign_contract(
        runtime,
        artifacts,
        authorization,
        authorization_payload,
        first_preflight.build_provenance,
    )
    contract_payload = _canonical_json_bytes(contract)
    contract_sha256 = _sha256(contract_payload)
    preserve_build_evidence(
        campaign_root,
        first_preflight.build_provenance,
        initial_files={
            CAMPAIGN_AUTHORIZATION_FILENAME: authorization_payload,
            CAMPAIGN_CONTRACT_FILENAME: contract_payload,
        },
    )

    attempted_count = 0
    stopped_reason: str | None = None
    for slot, spec in ordered:
        try:
            preflight = (
                first_preflight
                if spec.execution_ordinal == 1
                else _preflight(
                    slot,
                    repository=repository,
                    build_directory=build_directory,
                    build_provenance=build_provenance,
                    result_root=campaign_root,
                    minimum_free_bytes=runtime.minimum_free_bytes,
                )
            )
        except (Exception, KeyboardInterrupt) as error:
            stopped_reason = (
                f"slot {spec.execution_ordinal} preflight rejected without launch: "
                f"{type(error).__name__}: {error}"
            )
            break
        if preflight.revision != authorization["kauri_revision"]:
            stopped_reason = (
                f"slot {spec.execution_ordinal} preflight revision drifted without launch"
            )
            break
        if preflight.build_provenance != first_preflight.build_provenance:
            stopped_reason = (
                f"slot {spec.execution_ordinal} build provenance drifted without launch"
            )
            break
        _append_canonical_jsonl(
            campaign_root / CAMPAIGN_LEDGER_FILENAME,
            _started_record(
                runtime=runtime,
                spec=spec,
                preflight=preflight,
                authorization=authorization,
                authorization_payload=authorization_payload,
                contract_sha256=contract_sha256,
            ),
        )
        try:
            execution = execute_slot_once(
                slot,
                spec,
                preflight=preflight,
                static_artifacts=artifacts,
                authorization_receipt=authorization_payload,
                campaign_member=True,
            )
        except (Exception, KeyboardInterrupt) as error:
            execution = SlotExecutionResult(
                slot_directory=preflight.slot_directory,
                outcome="INCOMPLETE",
                reason=f"{type(error).__name__}: {error}",
                launch_count=0,
                phase_cutoffs=None,
                cleanup_ledger=(),
            )
        validation = _validate_attempt(
            preflight.slot_directory, campaign_member=True
        )
        _append_canonical_jsonl(
            campaign_root / CAMPAIGN_LEDGER_FILENAME,
            _terminal_record(
                runtime=runtime,
                spec=spec,
                execution=execution,
                validation=validation,
                authorization=authorization,
                authorization_payload=authorization_payload,
                contract_sha256=contract_sha256,
            ),
        )
        attempted_count += 1
        if not (
            execution.outcome == "PASS"
            and validation.outcome == "PASS"
            and validation.integrity_valid
            and validation.campaign_member
            and validation.figure_eligible
        ):
            stopped_reason = (
                f"slot {spec.execution_ordinal} did not independently validate PASS; "
                "preserved without retry or replacement"
            )
            break

    summary = _campaign_summary(
        runtime=runtime,
        root=campaign_root,
        authorization=authorization,
        authorization_payload=authorization_payload,
        contract_payload=contract_payload,
        attempted_count=attempted_count,
        stopped_reason=stopped_reason,
    )
    publish_campaign_summary(
        campaign_root / CAMPAIGN_SUMMARY_FILENAME,
        summary,
    )
    campaign_validation = validate_campaign(campaign_root)
    accepted = (
        summary["execution_complete"] is True
        and campaign_validation.outcome == "PASS"
        and campaign_validation.figure_eligible
    )
    return (
        0 if accepted else 1,
        {
            "command": "run",
            "campaign_directory": str(campaign_root),
            "attempted_slot_count": attempted_count,
            "expected_slot_count": len(runtime.slots),
            "stopped_reason": summary["stopped_reason"],
            "campaign_validation": asdict(campaign_validation),
            "ledger_sha256": summary["ledger_sha256"],
            "authorization_id": authorization["authorization_id"],
            "figure_eligible": accepted,
        },
    )


def _read_canonical_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise FactorialExecutionError(f"{label} is absent or is not a regular file")
    payload = path.read_bytes()
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FactorialExecutionError(f"{label} is invalid JSON") from error
    if not isinstance(document, dict) or _canonical_json_bytes(document) != payload:
        raise FactorialExecutionError(f"{label} is not exact canonical JSON")
    return document, payload


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        manifest_path = arguments.manifest.resolve()
        manifest = load_frozen_manifest(manifest_path)
        if arguments.command in {
            "validate-smoke",
            "validate-coverage-smoke",
            "validate-campaign",
        }:
            campaign_root, smoke_root, coverage_smoke_root = _validation_roots(
                arguments, manifest
            )
            if arguments.command == "validate-smoke":
                result = validate_slot(smoke_root / "smoke-n7-f2-PS")
                _emit(
                    {
                        "command": "validate-smoke",
                        "slot_directory": str(smoke_root / "smoke-n7-f2-PS"),
                        "validation": asdict(result),
                        "figure_eligible": False,
                    },
                    stream=sys.stdout,
                )
                return 0 if (
                    result.outcome == "PASS"
                    and result.integrity_valid
                    and not result.campaign_member
                    and not result.figure_eligible
                ) else 1
            if arguments.command == "validate-coverage-smoke":
                slot_ids = _coverage_smoke_slot_ids(manifest.manifest_id)
                if manifest.manifest_id in {
                    V25_MANIFEST_ID,
                    V26_MANIFEST_ID,
                    V27_MANIFEST_ID,
                    V28_MANIFEST_ID,
                    V29_MANIFEST_ID,
                    V30_MANIFEST_ID,
                    V31_MANIFEST_ID,
                    V32_MANIFEST_ID,
                    FROZEN_MANIFEST_ID,
                }:
                    plan = build_factorial_plan(manifest)
                    coverage = _n31_coverage_smoke(plan)
                    if not isinstance(
                        coverage.runtime,
                        N31CoverageSmokeRuntime,
                    ):
                        raise FactorialExecutionError(
                            "v33 coverage validation lacks the ordered runtime"
                        )
                    static_artifacts = _static_artifacts(
                        manifest_path,
                        plan,
                        _direct_runtime_bytes(coverage.runtime),
                    )
                    authorization_path = (
                        coverage_smoke_root
                        / COVERAGE_SMOKE_AUTHORIZATION_FILENAME
                    )
                    if (
                        authorization_path.is_symlink()
                        or not authorization_path.is_file()
                    ):
                        raise FactorialExecutionError(
                            "coverage-smoke root authorization is absent or unsafe"
                        )
                    authorization_payload = authorization_path.read_bytes()
                    build_document, _ = _read_canonical_json(
                        coverage_smoke_root
                        / slot_ids[0]
                        / "runtime/exact-build-provenance.json",
                        "coverage-smoke build provenance",
                    )
                    replayed = verify_completed_coverage_smoke_sequence(
                        coverage_smoke_root,
                        runtime=coverage.runtime,
                        static_artifacts=static_artifacts,
                        authorization_payload=authorization_payload,
                        build_provenance=build_document,
                    )
                    validations = [
                        {
                            "slot_directory": str(coverage_smoke_root / slot_id),
                            "validation": asdict(result),
                        }
                        for slot_id, result in zip(
                            slot_ids,
                            replayed,
                            strict=True,
                        )
                    ]
                    _emit(
                        {
                            "command": "validate-coverage-smoke",
                            "expected_slot_count": len(slot_ids),
                            "validated_slot_count": len(validations),
                            "validations": validations,
                            "sequence_integrity_valid": True,
                            "figure_eligible": False,
                        },
                        stream=sys.stdout,
                    )
                    return 0
                validations: list[dict[str, object]] = []
                accepted = True
                for slot_id in slot_ids:
                    slot_directory = coverage_smoke_root / slot_id
                    result = _validate_attempt(
                        slot_directory,
                        campaign_member=False,
                    )
                    slot_accepted = (
                        result.outcome == "PASS"
                        and result.integrity_valid
                        and not result.campaign_member
                        and not result.figure_eligible
                    )
                    validations.append(
                        {
                            "slot_directory": str(slot_directory),
                            "validation": asdict(result),
                        }
                    )
                    if not slot_accepted:
                        accepted = False
                        break
                if len(slot_ids) == 1:
                    output = {
                        "command": "validate-coverage-smoke",
                        **validations[0],
                        "figure_eligible": False,
                    }
                else:
                    output = {
                        "command": "validate-coverage-smoke",
                        "expected_slot_count": len(slot_ids),
                        "validated_slot_count": len(validations),
                        "validations": validations,
                        "figure_eligible": False,
                    }
                _emit(output, stream=sys.stdout)
                return 0 if accepted else 1
            result = validate_campaign(campaign_root)
            ledger_path = campaign_root / CAMPAIGN_LEDGER_FILENAME
            ledger_sha256 = (
                _sha256(ledger_path.read_bytes())
                if ledger_path.is_file() and not ledger_path.is_symlink()
                else None
            )
            _emit(
                {
                    "command": "validate-campaign",
                    "campaign_directory": str(campaign_root),
                    "ledger_sha256": ledger_sha256,
                    "validation": asdict(result),
                    "figure_eligible": result.figure_eligible,
                },
                stream=sys.stdout,
            )
            return 0 if (
                result.outcome == "PASS" and result.figure_eligible
            ) else 1
        if manifest.manifest_id != FROZEN_MANIFEST_ID:
            raise FactorialExecutionError(
                "shape-placement-factorial-v1 through v32 are validation-only; "
                "production commands require shape-placement-factorial-v33"
            )
        plan = build_factorial_plan(manifest)
        runtime = build_factorial_runtime(plan)
        runtime_payload = _require_frozen_artifacts(manifest, plan, runtime)
        (
            repository,
            build_directory,
            build_provenance,
            campaign_root,
            smoke_root,
            coverage_smoke_root,
        ) = _paths(arguments, runtime)
        if arguments.command == "plan":
            print(runtime_payload.decode("ascii"), end="")
            return 0
        if arguments.command == "preflight":
            preflight_runtime = runtime
            preflight_root = campaign_root
            target_runtime_sha256 = FROZEN_RUNTIME_SHA256
            target_runtime_id = runtime.runtime_id
            if arguments.preflight_target == "smoke":
                smoke = build_n7_ps_smoke_slot(plan.slots[0])
                smoke_runtime_payload = _direct_runtime_bytes(smoke.runtime)
                target_runtime_sha256 = _sha256(smoke_runtime_payload)
                if target_runtime_sha256 != FROZEN_SMOKE_RUNTIME_SHA256:
                    raise FactorialExecutionError(
                        "smoke runtime bytes differ from the exact frozen v33 identity"
                    )
                preflight_root = smoke_root
                target_runtime_id = smoke.runtime.artifact_id
                preflight_runtime = replace(
                    runtime,
                    runtime_id=f"{runtime.manifest_id}-excluded-n7-smoke-preflight-v1",
                    results_root=Path(smoke.slot.result_path).parent.as_posix(),
                    slots=(smoke.runtime,),
                )
            elif arguments.preflight_target == "coverage-smoke":
                coverage = _n31_coverage_smoke(plan)
                coverage_runtime_payload = _direct_runtime_bytes(coverage.runtime)
                target_runtime_sha256 = _sha256(coverage_runtime_payload)
                if (
                    target_runtime_sha256
                    != FROZEN_COVERAGE_SMOKE_RUNTIME_SHA256
                ):
                    raise FactorialExecutionError(
                        "coverage-smoke runtime bytes differ from the exact "
                        "frozen v32 identity"
                    )
                preflight_root = coverage_smoke_root
                target_runtime_id = (
                    coverage.runtime.runtime_id
                    if isinstance(coverage.runtime, N31CoverageSmokeRuntime)
                    else coverage.runtime.artifact_id
                )
                preflight_runtime = replace(
                    runtime,
                    runtime_id=(
                        f"{runtime.manifest_id}-excluded-n31-coverage-"
                        "smoke-preflight-v1"
                    ),
                    results_root=Path(coverage.slot.result_path).parent.as_posix(),
                    slots=coverage.runtimes,
                )
            pure = runtime_preflight(
                preflight_runtime,
                available_free_bytes=shutil.disk_usage(preflight_root.parent).free,
            )
            pure.update(
                {
                    "runtime_id": target_runtime_id,
                    "runtime_sha256": target_runtime_sha256,
                    "slot_count": len(preflight_runtime.slots),
                }
            )
            _emit(
                {
                    **pure,
                    "target": arguments.preflight_target,
                    "exact_live_preflight": "performed_immediately_before_launch",
                    "build_directory": str(build_directory),
                    "build_provenance": str(build_provenance),
                },
                stream=sys.stdout,
            )
            return 0
        if arguments.command == "smoke":
            code, result = _run_smoke(
                arguments,
                plan=plan,
                runtime=runtime,
                repository=repository,
                build_directory=build_directory,
                build_provenance=build_provenance,
                smoke_root=smoke_root,
            )
        elif arguments.command == "coverage-smoke":
            code, result = _run_coverage_smoke(
                arguments,
                plan=plan,
                runtime=runtime,
                repository=repository,
                build_directory=build_directory,
                build_provenance=build_provenance,
                smoke_root=smoke_root,
                coverage_smoke_root=coverage_smoke_root,
            )
        else:
            code, result = _run_campaign(
                arguments,
                plan=plan,
                runtime=runtime,
                repository=repository,
                build_directory=build_directory,
                build_provenance=build_provenance,
                campaign_root=campaign_root,
                smoke_root=smoke_root,
                coverage_smoke_root=coverage_smoke_root,
                runtime_payload=runtime_payload,
            )
        _emit(result, stream=sys.stdout)
        return code
    except (FactorialExecutionError, FactorialManifestError, OSError, ValueError) as error:
        _emit(
            {"reason": str(error), "status": "REJECT"},
            stream=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
