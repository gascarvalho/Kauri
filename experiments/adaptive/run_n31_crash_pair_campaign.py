"""Pure campaign contracts for the focused N=31 matched crash experiment.

The module freezes intent and validates preserved children.  It deliberately
contains no live runner, retry mechanism, or execution authorization.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import hashlib
import json
from pathlib import Path
import re
import secrets
import shutil
import tempfile
from typing import Any

from experiments.adaptive.kauri_experiment.factorial_manifest import (
    FactorialManifestError,
    derive_execution_schedule,
)
from experiments.adaptive.kauri_experiment.profiled_fault_archive import (
    EvidenceSealError,
    verify_evidence_seal,
)

_SCHEMA_VERSION = 1
_EXPERIMENT_ID = "n31-f5-q21-three-crash-pair-v1"
_PAIR_COUNT = 5
_CLAIM_SLOT_COUNT = 10
_ARMS = ("control", "adaptive")
_SCHEDULE_ALGORITHM = "factorial_manifest.derive_execution_schedule"
_LEDGER_GENESIS_DOMAIN = "kauri-n31-crash-pair-ledger-genesis-v1"
_SOURCE_BLIND_ORDER_DOMAIN = "kauri-n31-crash-pair-source-blind-order-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_PLAN_KEYS = frozenset(
    {
        "schema_version",
        "experiment_id",
        "revision",
        "build_sha256",
        "profile_sha256",
        "topology_proof_sha256",
        "campaign_seed",
        "pair_count",
        "pair_seeds",
        "claim_slot_count",
        "schedule_algorithm",
        "schedule_sha256",
        "execution_mode",
        "automatic_retries",
        "replacement_policy",
        "outcome_dependent_order",
        "slots",
        "plan_sha256",
        "ledger_genesis_sha256",
    }
)
_LEDGER_KEYS = frozenset(
    {
        "schema_version",
        "plan_sha256",
        "execution_ordinal",
        "slot_id",
        "pair_id",
        "arm",
        "pair_seed",
        "attempt_ordinal",
        "state",
        "execution_outcome",
        "validation",
        "child_tree_sha256",
        "child_seal_sha256",
        "previous_record_sha256",
        "record_sha256",
    }
)
_CHILD_KEYS = frozenset(
    {
        "slot_id",
        "pair_id",
        "arm",
        "child_tree_sha256",
        "child_seal_sha256",
        "sealed_child_directory",
        "source_inventory_sha256",
        "authoritative_commit_identity_sha256",
        "epoch_identity_sha256",
        "ranking_identity_sha256",
    }
)
_BOUND_IDENTITY_FIELDS = (
    "source_inventory_sha256",
    "authoritative_commit_identity_sha256",
    "epoch_identity_sha256",
    "ranking_identity_sha256",
)


class N31CrashPairCampaignError(ValueError):
    """The focused campaign intent, ledger, or evidence join is invalid."""


def _error(message: str) -> None:
    raise N31CrashPairCampaignError(message)


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _error(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _error(f"{label} must be a sequence")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _error(f"{label} must be an integer >= {minimum}")
    return value


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _error(f"{label} must be a lowercase SHA-256 digest")
    return value


def _canonical_json_bytes(value: object) -> bytes:
    try:
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
    except (TypeError, ValueError) as error:
        raise N31CrashPairCampaignError(
            "campaign evidence is not canonical JSON"
        ) from error


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _ledger_genesis_sha256(plan_sha256: str) -> str:
    return _canonical_sha256(
        {
            "schema_version": 1,
            "domain": _LEDGER_GENESIS_DOMAIN,
            "plan_sha256": plan_sha256,
        }
    )


def derive_focused_campaign_plan(
    *,
    pair_count: int,
    campaign_seed: int,
    revision: str,
    build_sha256: str,
    profile_sha256: str,
    topology_proof_sha256: str,
    pair_seeds: Sequence[int],
) -> dict[str, object]:
    """Precommit five pair seeds and the audited factorial launch schedule."""

    if _integer(pair_count, "pair count", minimum=1) != _PAIR_COUNT:
        _error("focused campaign requires exactly five pairs")
    seed = _integer(campaign_seed, "campaign seed")
    if not isinstance(revision, str) or _REVISION.fullmatch(revision) is None:
        _error("campaign revision must be a lowercase Git object ID")
    build = _sha256(build_sha256, "campaign build digest")
    profile = _sha256(profile_sha256, "campaign profile digest")
    topology = _sha256(topology_proof_sha256, "campaign topology proof digest")
    seeds = tuple(
        _integer(value, "pair seed") for value in _sequence(pair_seeds, "pair seeds")
    )
    if len(seeds) != _PAIR_COUNT or len(set(seeds)) != _PAIR_COUNT:
        _error("campaign requires five distinct precommitted pair seeds")

    block_ids = tuple(f"pair-{ordinal:02d}" for ordinal in range(1, _PAIR_COUNT + 1))
    try:
        schedule = derive_execution_schedule(block_ids, _ARMS, seed)
    except FactorialManifestError as error:
        raise N31CrashPairCampaignError("factorial campaign schedule failed") from error
    slots: list[dict[str, object]] = []
    for block in schedule:
        pair_ordinal = int(block.block_id.removeprefix("pair-"))
        for within_pair_ordinal, arm in enumerate(block.arm_order, start=1):
            execution_ordinal = len(slots) + 1
            slots.append(
                {
                    "execution_ordinal": execution_ordinal,
                    "slot_id": f"slot-{execution_ordinal:02d}",
                    "pair_id": block.block_id,
                    "pair_ordinal": pair_ordinal,
                    "pair_execution_ordinal": block.block_execution_ordinal,
                    "within_pair_ordinal": within_pair_ordinal,
                    "arm": arm,
                    "pair_seed": seeds[pair_ordinal - 1],
                    "attempt_ordinal": 1,
                    "claim_slot": True,
                }
            )
    schedule_sha256 = _canonical_sha256(slots)
    unhashed: dict[str, object] = {
        "schema_version": 1,
        "experiment_id": _EXPERIMENT_ID,
        "revision": revision,
        "build_sha256": build,
        "profile_sha256": profile,
        "topology_proof_sha256": topology,
        "campaign_seed": seed,
        "pair_count": _PAIR_COUNT,
        "pair_seeds": list(seeds),
        "claim_slot_count": len(slots),
        "schedule_algorithm": _SCHEDULE_ALGORITHM,
        "schedule_sha256": schedule_sha256,
        "execution_mode": "fixed_sequential",
        "automatic_retries": 0,
        "replacement_policy": "none",
        "outcome_dependent_order": False,
        "slots": slots,
    }
    plan_sha256 = _canonical_sha256(unhashed)
    return {
        **unhashed,
        "plan_sha256": plan_sha256,
        "ledger_genesis_sha256": _ledger_genesis_sha256(plan_sha256),
    }


def validate_campaign_plan(plan: Mapping[str, Any]) -> dict[str, object]:
    """Re-derive every campaign byte and reject mutable schedule drift."""

    document = _mapping(plan, "campaign plan")
    if set(document) != _PLAN_KEYS or document.get("schema_version") != 1:
        _error("campaign plan schema drifted")
    expected = derive_focused_campaign_plan(
        pair_count=_integer(document.get("pair_count"), "pair count", minimum=1),
        campaign_seed=_integer(document.get("campaign_seed"), "campaign seed"),
        revision=str(document.get("revision")),
        build_sha256=str(document.get("build_sha256")),
        profile_sha256=str(document.get("profile_sha256")),
        topology_proof_sha256=str(document.get("topology_proof_sha256")),
        pair_seeds=_sequence(document.get("pair_seeds"), "pair seeds"),
    )
    if dict(document) != expected:
        _error("campaign plan differs from its deterministic schedule or hashes")
    return expected


def validate_campaign_ledger(
    plan: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> dict[str, object]:
    """Consume one exact terminal hash-chain record per precommitted slot."""

    validated_plan = validate_campaign_plan(plan)
    slots = tuple(_mapping(slot, "campaign slot") for slot in validated_plan["slots"])
    ledger = tuple(
        _mapping(record, "campaign ledger record")
        for record in _sequence(records, "campaign ledger")
    )
    if len(ledger) != len(slots):
        _error("campaign ledger is incomplete or contains an appended retry")
    previous = str(validated_plan["ledger_genesis_sha256"])
    consumed_ordinals: list[int] = []
    consumed_slots: list[str] = []
    validated_claim_slots = 0
    for slot, record in zip(slots, ledger, strict=True):
        if set(record) != _LEDGER_KEYS or record.get("schema_version") != 1:
            _error("campaign ledger record schema drifted")
        expected_bindings = {
            "plan_sha256": validated_plan["plan_sha256"],
            "execution_ordinal": slot["execution_ordinal"],
            "slot_id": slot["slot_id"],
            "pair_id": slot["pair_id"],
            "arm": slot["arm"],
            "pair_seed": slot["pair_seed"],
            "attempt_ordinal": 1,
            "state": "TERMINAL",
            "previous_record_sha256": previous,
        }
        if any(record.get(key) != value for key, value in expected_bindings.items()):
            _error("campaign ledger record differs from its immutable slot or chain")
        if record.get("execution_outcome") not in {"PASS", "FAIL", "INCOMPLETE"}:
            _error("campaign ledger execution outcome is unsupported")
        _sha256(record.get("child_tree_sha256"), "ledger child tree digest")
        _sha256(record.get("child_seal_sha256"), "ledger child seal digest")
        validation = _mapping(record.get("validation"), "campaign validation")
        if set(validation) != {"outcome", "integrity_valid", "claim_slot"}:
            _error("campaign validation schema drifted")
        unhashed = {
            key: value for key, value in record.items() if key != "record_sha256"
        }
        expected_record_sha256 = _canonical_sha256(unhashed)
        if record.get("record_sha256") != expected_record_sha256:
            _error("campaign ledger record hash does not recompute")
        previous = expected_record_sha256
        consumed_ordinals.append(int(slot["execution_ordinal"]))
        consumed_slots.append(str(slot["slot_id"]))
        if (
            validation.get("outcome") == "PASS"
            and validation.get("integrity_valid") is True
            and validation.get("claim_slot") is True
        ):
            validated_claim_slots += 1
    return {
        "schema_version": 1,
        "execution_complete": True,
        "attempted_slot_count": len(ledger),
        "validated_claim_slot_count": validated_claim_slots,
        "automatic_retries": 0,
        "replacement_count": 0,
        "ledger_head_sha256": previous,
        "consumed_execution_ordinals": consumed_ordinals,
        "consumed_slot_ids": consumed_slots,
        "unconsumed_slot_ids": [],
        "extra_record_count": 0,
    }


def _source_blind_order_key(tree_sha256: str, seal_sha256: str) -> str:
    payload = f"{_SOURCE_BLIND_ORDER_DOMAIN}\0{tree_sha256}\0{seal_sha256}".encode(
        "ascii"
    )
    return hashlib.sha256(payload).hexdigest()


def _validated_measurements(value: object) -> dict[str, object]:
    measurements = _mapping(value, "source-blind scientific measurements")
    phases = _sequence(measurements.get("phases"), "source-blind phase rows")
    if len(phases) != 4:
        _error("source-blind measurements do not contain four phases")
    expected_names = ("baseline", "fault", "epoch1", "late")
    normalized: list[dict[str, object]] = []
    for name, raw_phase in zip(expected_names, phases, strict=True):
        phase = _mapping(raw_phase, "source-blind phase row")
        if (
            set(phase)
            != {
                "phase",
                "start_ns",
                "end_ns",
                "transactions",
                "mean_milli_tps",
            }
            or phase.get("phase") != name
        ):
            _error("source-blind phase schema or order drifted")
        start = _integer(phase.get("start_ns"), "phase start")
        end = _integer(phase.get("end_ns"), "phase end", minimum=1)
        transactions = _integer(phase.get("transactions"), "phase transactions")
        milli_tps = _integer(phase.get("mean_milli_tps"), "phase milli TPS")
        if end <= start or milli_tps != transactions * 1_000_000_000_000 // (
            end - start
        ):
            _error("source-blind throughput row does not recompute")
        normalized.append(dict(phase))
    late = _integer(
        measurements.get("late_window_throughput_milli_tps"),
        "late-window throughput",
    )
    if late != normalized[-1]["mean_milli_tps"]:
        _error("late-window throughput differs from its raw phase")
    return {"phases": normalized, "late_window_throughput_milli_tps": late}


def validate_campaign_source_blind(
    plan: Mapping[str, Any],
    children: Sequence[Mapping[str, Any]],
    *,
    ledger_records: Sequence[Mapping[str, Any]] | None = None,
    validate_child: Callable[..., Mapping[str, Any]],
    trusted_provenance: object,
) -> dict[str, object]:
    """Classify isolated sealed children, then join truth and paired effects."""

    validated_plan = validate_campaign_plan(plan)
    if not callable(validate_child):
        _error("source-blind child validator must be callable")
    if ledger_records is None:
        _error("source-blind campaign requires its exact terminal ledger")
    slots = tuple(_mapping(slot, "campaign slot") for slot in validated_plan["slots"])
    slot_by_id = {str(slot["slot_id"]): slot for slot in slots}
    ledger_by_slot: dict[str, Mapping[str, Any]] = {}
    ledger_summary: dict[str, object] | None = None
    ledger_summary = validate_campaign_ledger(validated_plan, ledger_records)
    records = tuple(
        _mapping(record, "campaign ledger record")
        for record in _sequence(ledger_records, "campaign ledger")
    )
    ledger_by_slot = {str(record["slot_id"]): record for record in records}
    if set(ledger_by_slot) != set(slot_by_id):
        _error("source-blind ledger does not consume every plan slot exactly once")
    raw_children = tuple(
        _mapping(child, "campaign child")
        for child in _sequence(children, "campaign children")
    )
    if len(raw_children) != len(slots):
        _error("source-blind campaign does not contain all ten children")

    prepared: list[tuple[str, str, Mapping[str, Any], Mapping[str, Any], Path]] = []
    observed_slots: set[str] = set()
    observed_seals: set[tuple[str, str]] = set()
    for child in raw_children:
        if set(child) != _CHILD_KEYS:
            _error("source-blind child metadata schema drifted")
        slot_id = child.get("slot_id")
        if (
            not isinstance(slot_id, str)
            or slot_id not in slot_by_id
            or slot_id in observed_slots
        ):
            _error("source-blind child has an unknown or duplicate slot identity")
        slot = slot_by_id[slot_id]
        if child.get("pair_id") != slot["pair_id"] or child.get("arm") != slot["arm"]:
            _error("source-blind child was relabelled against the frozen plan")
        tree_sha256 = _sha256(child.get("child_tree_sha256"), "child tree digest")
        seal_sha256 = _sha256(child.get("child_seal_sha256"), "child seal digest")
        seal_identity = (tree_sha256, seal_sha256)
        if seal_identity in observed_seals:
            _error("source-blind campaign reuses one child tree/seal identity")
        if ledger_by_slot:
            ledger_record = ledger_by_slot[slot_id]
            if (
                ledger_record.get("child_tree_sha256") != tree_sha256
                or ledger_record.get("child_seal_sha256") != seal_sha256
            ):
                _error("source-blind child seal differs from its terminal ledger")
        for field in _BOUND_IDENTITY_FIELDS:
            _sha256(child.get(field), f"child {field}")
        directory = child.get("sealed_child_directory")
        if (
            not isinstance(directory, Path)
            or directory.is_symlink()
            or not directory.is_dir()
        ):
            _error("source-blind child directory is absent")
        try:
            actual_seal = verify_evidence_seal(directory)
        except (EvidenceSealError, OSError) as error:
            raise N31CrashPairCampaignError(
                "source-blind child evidence seal rejected"
            ) from error
        if (
            actual_seal.tree_sha256 != tree_sha256
            or actual_seal.seal_sha256 != seal_sha256
        ):
            _error("source-blind child directory differs from its sealed identity")
        observed_slots.add(slot_id)
        observed_seals.add(seal_identity)
        prepared.append(
            (
                _source_blind_order_key(tree_sha256, seal_sha256),
                secrets.token_hex(16),
                child,
                slot,
                directory,
            )
        )

    observations: list[
        tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]
    ] = []
    for _, _, child, slot, source in sorted(
        prepared, key=lambda item: (item[0], item[1])
    ):
        with tempfile.TemporaryDirectory(prefix="kauri-n31-source-blind-") as temporary:
            isolated = Path(temporary) / f"opaque-{secrets.token_hex(16)}"
            try:
                shutil.copytree(source, isolated, symlinks=False)
                copied_seal = verify_evidence_seal(isolated)
            except (EvidenceSealError, OSError) as error:
                raise N31CrashPairCampaignError(
                    "cannot create an isolated sealed source-blind child"
                ) from error
            if (
                copied_seal.tree_sha256 != child["child_tree_sha256"]
                or copied_seal.seal_sha256 != child["child_seal_sha256"]
            ):
                _error("isolated source-blind child seal drifted")
            try:
                raw_validation = validate_child(
                    isolated,
                    trusted_provenance=trusted_provenance,
                )
            except Exception as error:
                raise N31CrashPairCampaignError(
                    "source-blind child validator failed"
                ) from error
            validation = _mapping(raw_validation, "source-blind child validation")
            if validation.get("reconstructed_from_raw_evidence") is not True:
                _error("source-blind result was not reconstructed from raw evidence")
            child_binding = _mapping(
                validation.get("child"), "source-blind child binding"
            )
            if (
                child_binding.get("path") != str(isolated)
                or child_binding.get("run_id") != isolated.name
                or child_binding.get("evidence_tree_sha256")
                != child["child_tree_sha256"]
                or child_binding.get("evidence_seal_sha256")
                != child["child_seal_sha256"]
            ):
                _error("source-blind result is not bound to the isolated child seal")
            for field in _BOUND_IDENTITY_FIELDS:
                if validation.get(field) != child[field]:
                    _error(f"source-blind result {field} binding drifted")
            _sha256(validation.get("fault_receipt_sha256"), "fault receipt digest")
            if (
                validation.get("fault_receipt_joined") is not True
                or validation.get("native_bundles_decoded") is not True
                or validation.get("runtime_graph_validated") is not True
                or validation.get("integrity_valid") is not True
                or validation.get("claim_slot") is not True
            ):
                _error("source-blind result lacks independent reconstruction gates")
            measurements = _validated_measurements(
                validation.get("scientific_measurements")
            )
            observations.append(
                (
                    child,
                    slot,
                    {**dict(validation), "scientific_measurements": measurements},
                )
            )

    child_verdicts: list[dict[str, object]] = []
    by_pair: dict[str, dict[str, dict[str, object]]] = {}
    for child, slot, validation in observations:
        epoch2_present = validation.get("epoch2_present")
        if type(epoch2_present) is not bool or epoch2_present is not (
            slot["arm"] == "adaptive"
        ):
            _error("source-blind native epoch presence does not match the frozen arm")
        verdict = {
            "slot_id": slot["slot_id"],
            "pair_id": slot["pair_id"],
            "arm": slot["arm"],
            "child_tree_sha256": child["child_tree_sha256"],
            "child_seal_sha256": child["child_seal_sha256"],
            **{field: validation[field] for field in _BOUND_IDENTITY_FIELDS},
            "fault_receipt_sha256": validation["fault_receipt_sha256"],
            "fault_receipt_joined": True,
            "native_bundles_decoded": True,
            "runtime_graph_validated": True,
            "epoch2_present": epoch2_present,
            "outcome": validation.get("outcome"),
            "scientific_measurements": validation["scientific_measurements"],
        }
        child_verdicts.append(verdict)
        by_pair.setdefault(str(slot["pair_id"]), {})[str(slot["arm"])] = verdict

    pair_verdicts: list[dict[str, object]] = []
    for pair_ordinal in range(1, _PAIR_COUNT + 1):
        pair_id = f"pair-{pair_ordinal:02d}"
        arms = by_pair.get(pair_id, {})
        if set(arms) != set(_ARMS):
            _error("source-blind pair join does not contain both frozen arms")
        control_tps = int(
            arms["control"]["scientific_measurements"][  # type: ignore[index]
                "late_window_throughput_milli_tps"
            ]
        )
        adaptive_tps = int(
            arms["adaptive"]["scientific_measurements"][  # type: ignore[index]
                "late_window_throughput_milli_tps"
            ]
        )
        effect = adaptive_tps - control_tps
        pair_verdicts.append(
            {
                "pair_id": pair_id,
                "control_late_window_throughput_milli_tps": control_tps,
                "adaptive_late_window_throughput_milli_tps": adaptive_tps,
                "effect_milli_tps": effect,
                "scientific_outcome": (
                    "FAVORABLE"
                    if effect > 0
                    else "UNFAVORABLE" if effect < 0 else "NEUTRAL"
                ),
            }
        )
    child_verdicts.sort(key=lambda row: int(str(row["slot_id"]).removeprefix("slot-")))
    all_pass = all(verdict.get("outcome") == "PASS" for verdict in child_verdicts)
    return {
        "schema_version": 1,
        "source_blind": True,
        "expected_claim_slot_count": _CLAIM_SLOT_COUNT,
        "validated_claim_slot_count": len(child_verdicts),
        "campaign_acceptance": "ACCEPTED" if all_pass else "REJECTED",
        "figure_eligible": all_pass,
        "child_verdicts": child_verdicts,
        "pair_verdicts": pair_verdicts,
        "ledger_head_sha256": (
            None if ledger_summary is None else ledger_summary["ledger_head_sha256"]
        ),
    }


__all__ = [
    "N31CrashPairCampaignError",
    "derive_focused_campaign_plan",
    "validate_campaign_ledger",
    "validate_campaign_plan",
    "validate_campaign_source_blind",
]
