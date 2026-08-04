#!/usr/bin/env python3
"""Run or validate the frozen 90-attempt N=31 PQAR campaign.

The live command is intentionally one shot.  It consumes one canonical
campaign root, seals the complete intent before slot one, invokes each planned
arm in a fresh sequential process set, and never retries or replaces a result.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from copy import deepcopy
import datetime as dt
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable

REPOSITORY = Path(__file__).resolve().parents[2]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from experiments.adaptive.kauri_experiment import (  # noqa: E402
    n31_post_qc_audit_runtime as audit_runtime,
)
from experiments.adaptive.kauri_experiment import (  # noqa: E402
    profiled_fault_runtime as runtime,
)
from experiments.adaptive.kauri_experiment.n31_post_qc_audit import (  # noqa: E402
    load_frozen_profile as load_audit_profile,
)
from experiments.adaptive.kauri_experiment.n31_post_qc_audit_campaign import (  # noqa: E402
    CAMPAIGN_ORDER_SEED,
    CAMPAIGN_SCENARIO,
    SCHEDULED_ATTEMPTS,
    FrozenCampaignProfile,
    N31PostQcAuditCampaignError,
    build_campaign_plan,
    canonical_json_bytes,
    derive_frozen_campaign_schedule,
    isolated_source_blind_observation,
    load_frozen_campaign_profile,
    semantic_document_sha256,
    source_blind_order_key,
    summarize_n31_pqar_campaign,
    validate_n31_pqar_campaign,
)
from experiments.adaptive.kauri_experiment.n31_static_diagnosis_runtime import (  # noqa: E402
    TrustedProvenance,
    derive_trusted_provenance,
    load_trusted_provenance,
    write_trusted_provenance,
)
from experiments.adaptive.kauri_experiment.profiled_fault_archive import (  # noqa: E402
    EvidenceSealError,
    create_evidence_seal,
    verify_evidence_seal,
)
from experiments.adaptive.kauri_experiment.profiled_fault_evaluation import (  # noqa: E402
    load_frozen_profile as load_runtime_profile,
)

DEFAULT_CAMPAIGN_PROFILE = (
    REPOSITORY / "experiments/adaptive/profiles/n31-f5-post-qc-audit-campaign-v2.json"
)
DEFAULT_AUDIT_PROFILE = (
    REPOSITORY / "experiments/adaptive/profiles/n31-f5-post-qc-audit-v4.json"
)
DEFAULT_RESULTS_PARENT = REPOSITORY / "results/n31-post-qc-audit-campaign-v2"


class N31PostQcAuditCampaignRunError(RuntimeError):
    """The campaign could not be allocated, run, or sealed safely."""


class N31PostQcAuditCampaignInterrupted(N31PostQcAuditCampaignRunError):
    """A spent campaign stopped early and its available evidence was sealed."""

    def __init__(self, message: str, campaign_directory: Path) -> None:
        super().__init__(message)
        self.campaign_directory = campaign_directory


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_exclusive(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, mode)
    try:
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    _fsync_directory(path.parent)


def _write_json_exclusive(path: Path, value: object) -> None:
    _write_exclusive(path, canonical_json_bytes(value))


def _replace_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    _write_exclusive(temporary, canonical_json_bytes(value))
    try:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _write_json_with_recovery(
    path: Path,
    value: object,
    *,
    label: str,
    failures: list[str],
) -> bool:
    try:
        _write_json_exclusive(path, value)
        return True
    except (Exception, KeyboardInterrupt) as error:
        failures.append(f"{label}:{type(error).__name__}:{error}")
    if path.exists():
        return False
    try:
        _write_json_exclusive(path, value)
        return True
    except (Exception, KeyboardInterrupt) as error:
        failures.append(f"{label}_recovery:{type(error).__name__}:{error}")
        return False


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _preserve_aborted_campaign(
    root: Path,
    *,
    phase: str,
    error: BaseException,
) -> None:
    """Seal a spent one-shot root that failed before normal finalization."""

    abort_document = {
        "schema_version": 1,
        "scenario": f"{CAMPAIGN_SCENARIO}-controller-abort",
        "phase": phase,
        "exception": f"{type(error).__name__}: {error}",
        "figure_eligible": False,
    }
    try:
        _write_exclusive(
            root / "controller-abort.json",
            canonical_json_bytes(abort_document),
        )
        created = create_evidence_seal(root)
        verified = verify_evidence_seal(root)
    except (Exception, KeyboardInterrupt) as seal_error:
        raise N31PostQcAuditCampaignRunError(
            f"spent campaign root could not preserve {phase} abort: {seal_error}"
        ) from seal_error
    if created != verified:
        raise N31PostQcAuditCampaignRunError(
            f"spent campaign root did not durably preserve {phase} abort"
        )


def _allocate_campaign_root(path: Path) -> Path:
    root = path.resolve()
    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.mkdir(mode=0o700)
    except FileExistsError as error:
        raise N31PostQcAuditCampaignRunError(
            f"canonical one-shot campaign root is already spent: {root}"
        ) from error
    try:
        for name in ("children", "executions", "intent", "starts"):
            (root / name).mkdir(mode=0o700)
        _fsync_directory(root)
        _fsync_directory(root.parent)
    except (Exception, KeyboardInterrupt) as error:
        _preserve_aborted_campaign(root, phase="allocation", error=error)
        raise N31PostQcAuditCampaignInterrupted(
            f"campaign root allocation aborted and was preserved at {root}",
            root,
        ) from error
    return root


def _resolve_bound_path(repository: Path, relative: str, label: str) -> Path:
    path = (repository / relative).resolve()
    try:
        path.relative_to(repository)
    except ValueError as error:
        raise N31PostQcAuditCampaignRunError(
            f"{label} escapes the repository"
        ) from error
    return path


def _validate_bound_profiles(
    profile: FrozenCampaignProfile,
    *,
    repository: Path,
    audit_profile_path: Path,
) -> Path:
    audit = load_audit_profile(audit_profile_path.resolve())
    runtime_path = _resolve_bound_path(
        repository, profile.runtime_profile_path, "runtime profile"
    )
    runtime_profile = load_runtime_profile(runtime_path)
    if (
        audit.profile_id != profile.audit_profile_id
        or audit.profile_sha256 != profile.audit_profile_sha256
        or runtime_profile.profile_id != profile.runtime_profile_id
        or runtime_profile.profile_sha256 != profile.runtime_profile_sha256
    ):
        raise N31PostQcAuditCampaignRunError(
            "live profile inputs differ from frozen campaign bindings"
        )
    return runtime_path


def _pilot_gate(
    profile: FrozenCampaignProfile,
    *,
    pilot_sequence_directory: Path,
    pilot_result: Mapping[str, Any],
    trusted_provenance: TrustedProvenance,
) -> dict[str, object]:
    if (
        pilot_result.get("verdict") != "PASS"
        or pilot_result.get("kind") != "fresh-three-arm-pilot"
        or pilot_result.get("preflight_revision") != trusted_provenance.revision
        or pilot_result.get("preflight_profile_sha256") != profile.audit_profile_sha256
        or pilot_result.get("figure_eligible") is not False
        or not isinstance(pilot_result.get("evidence_tree_sha256"), str)
        or not isinstance(pilot_result.get("evidence_seal_sha256"), str)
    ):
        raise N31PostQcAuditCampaignRunError(
            "campaign requires one sealed aggregate-PASS v4 pilot"
        )
    return {
        "schema_version": 1,
        "scenario": f"{CAMPAIGN_SCENARIO}-pilot-gate",
        "required_verdict": "PASS",
        "pilot_sequence_directory": str(pilot_sequence_directory.resolve()),
        "pilot_tree_sha256": pilot_result["evidence_tree_sha256"],
        "pilot_seal_sha256": pilot_result["evidence_seal_sha256"],
        "kauri_revision": trusted_provenance.revision,
        "trusted_provenance_sha256": trusted_provenance.sha256,
        "audit_profile_sha256": profile.audit_profile_sha256,
        "runtime_profile_sha256": profile.runtime_profile_sha256,
        "figure_eligible": False,
    }


def _slot_record_base(slot: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "scenario": f"{CAMPAIGN_SCENARIO}-slot-execution",
        **deepcopy(dict(slot)),
    }


def _start_record(
    slot: Mapping[str, object],
    *,
    intent_tree_sha256: str,
    intent_seal_sha256: str,
    started_utc: str,
    started_monotonic_ns: int,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "scenario": f"{CAMPAIGN_SCENARIO}-slot-start",
        **deepcopy(dict(slot)),
        "intent_tree_sha256": intent_tree_sha256,
        "intent_seal_sha256": intent_seal_sha256,
        "started_utc": started_utc,
        "started_monotonic_ns": started_monotonic_ns,
    }


def _relative_child(root: Path, run_directory: Path, results_root: Path) -> str:
    resolved = run_directory.resolve()
    if resolved.parent != results_root.resolve():
        raise N31PostQcAuditCampaignRunError(
            "run_once returned a directory outside its exact scheduled slot root"
        )
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as error:
        raise N31PostQcAuditCampaignRunError(
            "run_once returned a directory outside the campaign"
        ) from error


def _direct_child_names(path: Path) -> set[str]:
    names: set[str] = set()
    for child in path.iterdir():
        if child.is_symlink() or not child.is_dir():
            raise N31PostQcAuditCampaignRunError(
                f"campaign child root contains a non-directory: {child.name}"
            )
        if child.name in names:
            raise N31PostQcAuditCampaignRunError(
                "campaign child root contains a duplicate canonical name"
            )
        names.add(child.name)
    return names


def _blind_observation(
    record: Mapping[str, Any],
    *,
    root: Path,
    trusted_provenance: TrustedProvenance,
    classifier: Callable[..., Mapping[str, Any]],
) -> dict[str, object]:
    relative = record.get("run_directory")
    if not isinstance(relative, str):
        raise N31PostQcAuditCampaignRunError(
            "returned execution record lacks its exact child path"
        )
    child = (root / relative).resolve()
    try:
        child.relative_to(root)
    except ValueError as error:
        raise N31PostQcAuditCampaignRunError(
            "returned execution record child path escapes the campaign"
        ) from error
    observation = isolated_source_blind_observation(
        child,
        trusted_provenance=trusted_provenance,
        classifier=classifier,
    )
    return {
        "schema_version": 1,
        "scenario": f"{CAMPAIGN_SCENARIO}-blind-observation",
        "ordinal": record["ordinal"],
        "run_directory": relative,
        "observation": deepcopy(dict(observation)),
        "observation_error": None,
    }


def run_campaign(
    *,
    campaign_profile_path: Path,
    audit_profile_path: Path,
    pilot_sequence_directory: Path,
    trusted_provenance: TrustedProvenance,
    repository: Path,
    campaign_root: Path,
    app_binary: Path,
    manager_binary: Path,
    keygen_binary: Path,
    tls_keygen_binary: Path,
    epoch_profile_digest_binary: Path,
    build_directory: Path,
    build_provenance_path: Path,
    prepare_build: Callable[..., Any] | None = None,
    derive_provenance: Callable[..., TrustedProvenance] | None = None,
    preflight: Callable[..., Mapping[str, Any]] | None = None,
    validate_pilot: Callable[..., Mapping[str, Any]] | None = None,
    run_once: Callable[..., tuple[Path, str]] | None = None,
    classify_source_blind: Callable[..., Mapping[str, Any]] | None = None,
    validate_after_seal: bool = True,
) -> Path:
    """Execute the frozen order and return the canonical summary path.

    A returned PASS, FAIL, or INCOMPLETE never affects later launches.  A
    raised runtime/infrastructure exception spends that slot, records every
    untouched suffix as INCOMPLETE/not-started, seals the campaign, and raises
    :class:`N31PostQcAuditCampaignInterrupted` with the preserved root.
    """

    repository = repository.resolve()
    campaign_profile_path = campaign_profile_path.resolve()
    audit_profile_path = audit_profile_path.resolve()
    pilot_sequence_directory = pilot_sequence_directory.resolve()
    build_directory = build_directory.resolve()
    profile = load_frozen_campaign_profile(campaign_profile_path)
    runtime_profile_path = _validate_bound_profiles(
        profile,
        repository=repository,
        audit_profile_path=audit_profile_path,
    )
    prepare_build = prepare_build or runtime.prepare_exact_revision_build
    derive_provenance = derive_provenance or derive_trusted_provenance
    preflight = preflight or audit_runtime.preflight
    validate_pilot = validate_pilot or audit_runtime.validate_pilot_sequence
    run_once = run_once or audit_runtime.run_once
    if classify_source_blind is None:
        classify_source_blind = getattr(
            audit_runtime, "classify_preserved_run_source_blind", None
        )
    if not callable(classify_source_blind):
        raise N31PostQcAuditCampaignRunError(
            "v4 runtime lacks classify_preserved_run_source_blind; refusing "
            "to launch a campaign without an arm-free preserved-run extractor"
        )
    if campaign_root.resolve().exists():
        raise N31PostQcAuditCampaignRunError(
            "canonical one-shot campaign root is already spent: "
            f"{campaign_root.resolve()}"
        )

    # Validate the excluded pilot first; then make exactly one clean build,
    # provenance derivation, and preflight for the campaign itself.
    pilot_result = validate_pilot(
        pilot_sequence_directory,
        trusted_provenance=trusted_provenance,
    )
    gate = _pilot_gate(
        profile,
        pilot_sequence_directory=pilot_sequence_directory,
        pilot_result=pilot_result,
        trusted_provenance=trusted_provenance,
    )
    prepare_build(repository=repository, build_directory=build_directory)
    current_provenance = derive_provenance(
        repository=repository,
        app_binary=app_binary.resolve(),
        manager_binary=manager_binary.resolve(),
        keygen_binary=keygen_binary.resolve(),
        tls_keygen_binary=tls_keygen_binary.resolve(),
        epoch_profile_digest_binary=epoch_profile_digest_binary.resolve(),
        build_directory=build_directory,
        build_provenance_path=build_provenance_path.resolve(),
    )
    if current_provenance != trusted_provenance:
        raise N31PostQcAuditCampaignRunError(
            "campaign build/provenance differs from the qualifying pilot receipt"
        )
    frozen_preflight = dict(
        preflight(
            audit_profile_path=audit_profile_path,
            repository=repository,
            app_binary=app_binary.resolve(),
            manager_binary=manager_binary.resolve(),
            keygen_binary=keygen_binary.resolve(),
            tls_keygen_binary=tls_keygen_binary.resolve(),
            epoch_profile_digest_binary=epoch_profile_digest_binary.resolve(),
            build_directory=build_directory,
            build_provenance_path=build_provenance_path.resolve(),
        )
    )
    if (
        frozen_preflight.get("verdict") != "PASS"
        or frozen_preflight.get("revision") != trusted_provenance.revision
        or frozen_preflight.get("audit_profile")
        != {
            "profile_id": profile.audit_profile_id,
            "sha256": profile.audit_profile_sha256,
        }
        or not isinstance(frozen_preflight.get("runtime_profile"), Mapping)
        or frozen_preflight["runtime_profile"].get("profile_id")
        != profile.runtime_profile_id
        or frozen_preflight["runtime_profile"].get("sha256")
        != profile.runtime_profile_sha256
    ):
        raise N31PostQcAuditCampaignRunError(
            "one campaign preflight differs from the frozen profile/provenance"
        )

    root = _allocate_campaign_root(campaign_root)
    intent = root / "intent"
    try:
        _write_exclusive(
            intent / "campaign-profile.json", campaign_profile_path.read_bytes()
        )
        _write_exclusive(intent / "audit-profile.json", audit_profile_path.read_bytes())
        _write_exclusive(
            intent / "runtime-profile.json", runtime_profile_path.read_bytes()
        )
        write_trusted_provenance(intent / "trusted-provenance.json", trusted_provenance)
        _fsync_directory(intent)
        _write_json_exclusive(intent / "pilot-gate.json", gate)
        _write_json_exclusive(intent / "frozen-preflight.json", frozen_preflight)
        plan = build_campaign_plan(
            profile,
            kauri_revision=trusted_provenance.revision,
            trusted_provenance_sha256=trusted_provenance.sha256,
            pilot_gate=gate,
            frozen_preflight_sha256=semantic_document_sha256(frozen_preflight),
        )
        _write_json_exclusive(intent / "campaign-plan.json", plan)
        intent_seal = create_evidence_seal(intent)
        verified_intent = verify_evidence_seal(intent)
        if intent_seal != verified_intent or not intent_seal.seal_path.is_file():
            raise N31PostQcAuditCampaignRunError(
                "campaign intent seal was not durable before slot one"
            )
    except (Exception, KeyboardInterrupt) as error:
        _preserve_aborted_campaign(root, phase="intent", error=error)
        raise N31PostQcAuditCampaignInterrupted(
            f"campaign intent aborted and was preserved at {root}",
            root,
        ) from error

    schedule = derive_frozen_campaign_schedule(profile)
    records: list[dict[str, object]] = []
    controller_failures: list[str] = []
    interruption: BaseException | None = None
    interrupted_ordinal: int | None = None
    run_arguments = {
        "audit_profile_path": audit_profile_path,
        "trusted_provenance": trusted_provenance,
        "frozen_preflight": frozen_preflight,
        "repository": repository,
        "app_binary": app_binary.resolve(),
        "manager_binary": manager_binary.resolve(),
        "keygen_binary": keygen_binary.resolve(),
        "tls_keygen_binary": tls_keygen_binary.resolve(),
        "epoch_profile_digest_binary": epoch_profile_digest_binary.resolve(),
        "build_directory": build_directory,
        "build_provenance_path": build_provenance_path.resolve(),
    }
    results_root = (root / "children").resolve()
    for slot in schedule:
        ordinal = int(slot["ordinal"])
        started_utc = _utc_now()
        started_ns = time.monotonic_ns()
        children_before: set[str] = set()
        execution_path = root / "executions" / f"slot-{ordinal:03d}.json"
        try:
            children_before = _direct_child_names(results_root)
            _write_json_exclusive(
                root / "starts" / f"slot-{ordinal:03d}.json",
                _start_record(
                    slot,
                    intent_tree_sha256=intent_seal.tree_sha256,
                    intent_seal_sha256=intent_seal.seal_sha256,
                    started_utc=started_utc,
                    started_monotonic_ns=started_ns,
                ),
            )
            if not (intent / "evidence-seal.json").is_file():
                raise N31PostQcAuditCampaignRunError(
                    "intent seal disappeared before the live invocation"
                )
            returned_directory, verdict = run_once(
                arm=str(slot["arm"]),
                results_root=results_root,
                **run_arguments,
            )
            if verdict not in {"PASS", "FAIL", "INCOMPLETE"}:
                raise N31PostQcAuditCampaignRunError(
                    f"run_once returned unsupported verdict: {verdict}"
                )
            relative_child = _relative_child(
                root, Path(returned_directory), results_root
            )
            child_seal = verify_evidence_seal(Path(returned_directory).resolve())
            finished_utc = _utc_now()
            finished_ns = time.monotonic_ns()
            observed_children = sorted(
                _direct_child_names(results_root) - children_before
            )
            if observed_children != [Path(returned_directory).resolve().name]:
                raise N31PostQcAuditCampaignRunError(
                    "one invocation did not create exactly its returned opaque child"
                )
            record = {
                **_slot_record_base(slot),
                "launch_status": "returned",
                "started_utc": started_utc,
                "finished_utc": finished_utc,
                "started_monotonic_ns": started_ns,
                "finished_monotonic_ns": finished_ns,
                "elapsed_ns": finished_ns - started_ns,
                "run_directory": relative_child,
                "original_verdict": verdict,
                "child_tree_sha256": child_seal.tree_sha256,
                "child_seal_sha256": child_seal.seal_sha256,
                "intent_tree_sha256": intent_seal.tree_sha256,
                "intent_seal_sha256": intent_seal.seal_sha256,
                "observed_children": observed_children,
                "exception": None,
            }
            try:
                _write_json_exclusive(execution_path, record)
            except (Exception, KeyboardInterrupt) as write_error:
                controller_failures.append(
                    "returned_execution_record_write_failed_"
                    f"{ordinal:03d}:{type(write_error).__name__}:{write_error}"
                )
                interruption = write_error
                interrupted_ordinal = ordinal
                if not execution_path.exists():
                    try:
                        _write_json_exclusive(execution_path, record)
                    except (Exception, KeyboardInterrupt) as retry_error:
                        controller_failures.append(
                            "returned_execution_record_recovery_failed_"
                            f"{ordinal:03d}:{type(retry_error).__name__}:"
                            f"{retry_error}"
                        )
        except (Exception, KeyboardInterrupt) as error:
            finished_utc = _utc_now()
            finished_ns = time.monotonic_ns()
            try:
                observed_children = sorted(
                    _direct_child_names(results_root) - children_before
                )
            except (OSError, N31PostQcAuditCampaignRunError):
                observed_children = []
            record = {
                **_slot_record_base(slot),
                "launch_status": "raised",
                "started_utc": started_utc,
                "finished_utc": finished_utc,
                "started_monotonic_ns": started_ns,
                "finished_monotonic_ns": finished_ns,
                "elapsed_ns": finished_ns - started_ns,
                "run_directory": None,
                "original_verdict": None,
                "child_tree_sha256": None,
                "child_seal_sha256": None,
                "intent_tree_sha256": intent_seal.tree_sha256,
                "intent_seal_sha256": intent_seal.seal_sha256,
                "observed_children": observed_children,
                "exception": f"{type(error).__name__}: {error}",
            }
            interruption = error
            interrupted_ordinal = ordinal
            try:
                _write_json_exclusive(execution_path, record)
            except (Exception, KeyboardInterrupt) as write_error:
                controller_failures.append(
                    "execution_record_write_failed_"
                    f"{ordinal:03d}:{type(write_error).__name__}:{write_error}"
                )
                if not execution_path.exists():
                    try:
                        _write_json_exclusive(execution_path, record)
                    except (Exception, KeyboardInterrupt) as retry_error:
                        controller_failures.append(
                            "execution_record_recovery_failed_"
                            f"{ordinal:03d}:{type(retry_error).__name__}:"
                            f"{retry_error}"
                        )
        records.append(record)
        if interruption is not None:
            break

    if interruption is not None:
        assert interrupted_ordinal is not None
        for slot in schedule[interrupted_ordinal:]:
            ordinal = int(slot["ordinal"])
            record = {
                **_slot_record_base(slot),
                "launch_status": "not_started",
                "started_utc": None,
                "finished_utc": None,
                "started_monotonic_ns": None,
                "finished_monotonic_ns": None,
                "elapsed_ns": None,
                "run_directory": None,
                "original_verdict": None,
                "child_tree_sha256": None,
                "child_seal_sha256": None,
                "intent_tree_sha256": intent_seal.tree_sha256,
                "intent_seal_sha256": intent_seal.seal_sha256,
                "observed_children": [],
                "exception": (
                    f"untouched_after_interrupted_slot_{interrupted_ordinal:03d}"
                ),
            }
            records.append(record)
            suffix_path = root / "executions" / f"slot-{ordinal:03d}.json"
            try:
                _write_json_exclusive(suffix_path, record)
            except (Exception, KeyboardInterrupt) as write_error:
                controller_failures.append(
                    "suffix_record_write_failed_"
                    f"{ordinal:03d}:{type(write_error).__name__}:{write_error}"
                )
                if not suffix_path.exists():
                    try:
                        _write_json_exclusive(suffix_path, record)
                    except (Exception, KeyboardInterrupt) as retry_error:
                        controller_failures.append(
                            "suffix_record_recovery_failed_"
                            f"{ordinal:03d}:{type(retry_error).__name__}:"
                            f"{retry_error}"
                        )

    # Blind pass over every exact returned child path.  The classifier receives
    # only the child path and external provenance, never the plan or an arm.
    observations: list[dict[str, object]] = []
    returned_records = [
        record for record in records if record.get("launch_status") == "returned"
    ]
    blind_records = sorted(returned_records, key=source_blind_order_key)
    blind_rank_keys = [source_blind_order_key(record) for record in blind_records]
    if len(blind_rank_keys) != len(set(blind_rank_keys)):
        controller_failures.append("source_blind_child_identity_collision")
        blind_records = []
    for record in blind_records:
        try:
            observations.append(
                _blind_observation(
                    record,
                    root=root,
                    trusted_provenance=trusted_provenance,
                    classifier=classify_source_blind,
                )
            )
        except (Exception, KeyboardInterrupt) as error:
            observations.append(
                {
                    "schema_version": 1,
                    "scenario": f"{CAMPAIGN_SCENARIO}-blind-observation",
                    "ordinal": record["ordinal"],
                    "run_directory": record["run_directory"],
                    "observation": None,
                    "observation_error": f"{type(error).__name__}: {error}",
                }
            )
    observation_document = {
        "schema_version": 1,
        "scenario": f"{CAMPAIGN_SCENARIO}-blind-observations",
        "observations": observations,
    }
    _write_json_with_recovery(
        root / "blind-observations.json",
        observation_document,
        label="blind_observation_write_failed",
        failures=controller_failures,
    )

    controller_path = root / "controller-failures.json"

    def controller_document() -> dict[str, object]:
        return {
            "schema_version": 1,
            "scenario": f"{CAMPAIGN_SCENARIO}-controller-failures",
            "failures": list(controller_failures),
        }

    _write_json_with_recovery(
        controller_path,
        controller_document(),
        label="controller_failure_record_write_failed",
        failures=controller_failures,
    )
    if controller_path.exists():
        _replace_json(controller_path, controller_document())

    # Truth join and statistics occur only after the full blind pass exists.
    summary = summarize_n31_pqar_campaign(
        profile,
        plan,
        records,
        observations,
        controller_failures=controller_failures,
    )
    summary_path = root / "campaign-summary.json"
    try:
        _write_exclusive(summary_path, canonical_json_bytes(summary))
    except (Exception, KeyboardInterrupt) as error:
        controller_failures.append(
            f"campaign_summary_write_failed:{type(error).__name__}:{error}"
        )
        if controller_path.exists():
            _replace_json(controller_path, controller_document())
        summary = summarize_n31_pqar_campaign(
            profile,
            plan,
            records,
            observations,
            controller_failures=controller_failures,
        )
        if not summary_path.exists():
            _write_exclusive(summary_path, canonical_json_bytes(summary))
    try:
        create_evidence_seal(root)
        verify_evidence_seal(root)
    except (EvidenceSealError, OSError, KeyboardInterrupt) as error:
        controller_failures.append(
            f"campaign_seal_failed:{type(error).__name__}:{error}"
        )
        seal_path = root / "evidence-seal.json"
        seal_path.unlink(missing_ok=True)
        if controller_path.exists():
            _replace_json(controller_path, controller_document())
        summary = summarize_n31_pqar_campaign(
            profile,
            plan,
            records,
            observations,
            controller_failures=controller_failures,
        )
        _replace_json(summary_path, summary)
        try:
            create_evidence_seal(root)
            verify_evidence_seal(root)
        except (EvidenceSealError, OSError, KeyboardInterrupt) as retry_error:
            raise N31PostQcAuditCampaignRunError(
                f"preserved campaign could not be sealed: {retry_error}"
            ) from retry_error

    if interruption is not None:
        raise N31PostQcAuditCampaignInterrupted(
            "campaign stopped after interrupted slot "
            f"{interrupted_ordinal:03d}; untouched suffix was recorded "
            f"INCOMPLETE and preserved at {root}",
            root,
        ) from interruption
    if validate_after_seal:
        validate_n31_pqar_campaign(
            root,
            trusted_provenance=trusted_provenance,
            classify_preserved_run_source_blind=classify_source_blind,
            validate_preserved_run=audit_runtime.validate_preserved_run,
            validate_pilot_sequence=validate_pilot,
        )
    return summary_path


def _default_campaign_root(repository: Path, revision: str) -> Path:
    return (
        repository
        / DEFAULT_RESULTS_PARENT.relative_to(REPOSITORY)
        / f"{revision[:8]}-seed{CAMPAIGN_ORDER_SEED}-campaign-v2"
    )


def _arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "validate"))
    parser.add_argument(
        "--campaign-profile", type=Path, default=DEFAULT_CAMPAIGN_PROFILE
    )
    parser.add_argument("--audit-profile", type=Path, default=DEFAULT_AUDIT_PROFILE)
    parser.add_argument("--pilot-sequence", type=Path)
    parser.add_argument("--trusted-provenance", type=Path, required=True)
    parser.add_argument("--repository", type=Path, default=REPOSITORY)
    parser.add_argument("--campaign-root", type=Path)
    parser.add_argument(
        "--build-directory", type=Path, default=REPOSITORY / "build-adaptive"
    )
    parser.add_argument(
        "--app-binary",
        type=Path,
        default=REPOSITORY / "build-adaptive/examples/hotstuff-app",
    )
    parser.add_argument(
        "--manager-binary",
        type=Path,
        default=REPOSITORY / "build-adaptive/examples/adaptation-manager",
    )
    parser.add_argument(
        "--keygen-binary",
        type=Path,
        default=REPOSITORY / "build-adaptive/hotstuff-keygen",
    )
    parser.add_argument(
        "--tls-keygen-binary",
        type=Path,
        default=REPOSITORY / "build-adaptive/hotstuff-tls-keygen",
    )
    parser.add_argument(
        "--epoch-profile-digest-binary",
        type=Path,
        default=REPOSITORY / "build-adaptive/examples/epoch-profile-digest",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _arguments(argv)
    try:
        trusted = load_trusted_provenance(arguments.trusted_provenance.resolve())
        repository = arguments.repository.resolve()
        campaign_root = arguments.campaign_root
        if campaign_root is None:
            campaign_root = _default_campaign_root(repository, trusted.revision)
        if arguments.command == "validate":
            summary = validate_n31_pqar_campaign(
                campaign_root.resolve(), trusted_provenance=trusted
            )
            print(canonical_json_bytes(summary).decode("utf-8"), end="")
            return 0 if summary["campaign_acceptance"] == "ACCEPTED" else 1
        canonical_root = _default_campaign_root(repository, trusted.revision)
        if campaign_root.resolve() != canonical_root.resolve():
            raise N31PostQcAuditCampaignRunError(
                "run requires the one canonical frozen campaign root: "
                f"{canonical_root}"
            )
        if arguments.pilot_sequence is None:
            raise N31PostQcAuditCampaignRunError(
                "run requires --pilot-sequence for the excluded sealed v4 gate"
            )
        build_directory = arguments.build_directory.resolve()
        summary_path = run_campaign(
            campaign_profile_path=arguments.campaign_profile,
            audit_profile_path=arguments.audit_profile,
            pilot_sequence_directory=arguments.pilot_sequence,
            trusted_provenance=trusted,
            repository=repository,
            campaign_root=campaign_root,
            app_binary=arguments.app_binary,
            manager_binary=arguments.manager_binary,
            keygen_binary=arguments.keygen_binary,
            tls_keygen_binary=arguments.tls_keygen_binary,
            epoch_profile_digest_binary=arguments.epoch_profile_digest_binary,
            build_directory=build_directory,
            build_provenance_path=(build_directory / runtime.BUILD_PROVENANCE_FILENAME),
        )
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        print(summary_path)
        return 0 if summary["campaign_acceptance"] == "ACCEPTED" else 1
    except N31PostQcAuditCampaignInterrupted as error:
        print(f"error: {error}", file=sys.stderr)
        print(error.campaign_directory, file=sys.stderr)
        return 2
    except (
        N31PostQcAuditCampaignError,
        N31PostQcAuditCampaignRunError,
        OSError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
