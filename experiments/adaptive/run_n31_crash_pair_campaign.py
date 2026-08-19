"""Pure campaign contracts for the focused N=31 matched crash experiment.

The module freezes intent and validates preserved children.  It deliberately
contains no live runner, retry mechanism, or execution authorization.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from fractions import Fraction
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
_SCIENTIFIC_SUPPORT_DOMAIN = "kauri-focused-campaign-scientific-support-v1"
_SCIENTIFIC_SUPPORT_CONTRACT = {
    "schema_version": 1,
    "domain": _SCIENTIFIC_SUPPORT_DOMAIN,
}
_SCIENTIFIC_THRESHOLD_KEYS = frozenset(
    {
        "adaptive_ratio_min_ppm",
        "containment_over_baseline_min_ppm",
        "paired_ratio_min_ppm",
    }
)
_PPM_SCALE = 1_000_000
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
    extended_schema: bool | None = None
    for name, raw_phase in zip(expected_names, phases, strict=True):
        phase = _mapping(raw_phase, "source-blind phase row")
        extended = "buckets" in phase or "median_milli_tps" in phase
        if extended_schema is None:
            extended_schema = extended
        elif extended_schema is not extended:
            _error("source-blind phase aggregation schema is mixed")
        expected_keys = {
            "phase",
            "start_ns",
            "end_ns",
            "transactions",
            "mean_milli_tps",
        }
        if extended:
            expected_keys |= {"buckets", "median_milli_tps"}
        if set(phase) != expected_keys or phase.get("phase") != name:
            _error("source-blind phase schema or order drifted")
        start = _integer(phase.get("start_ns"), "phase start")
        end = _integer(phase.get("end_ns"), "phase end", minimum=1)
        transactions = _integer(phase.get("transactions"), "phase transactions")
        milli_tps = _integer(phase.get("mean_milli_tps"), "phase milli TPS")
        if end <= start or milli_tps != transactions * 1_000_000_000_000 // (
            end - start
        ):
            _error("source-blind throughput row does not recompute")
        if extended:
            buckets = [
                _mapping(bucket, "source-blind phase bucket")
                for bucket in _sequence(phase.get("buckets"), "phase buckets")
            ]
            if len(buckets) != 6:
                _error("scientific phase does not contain six complete buckets")
            throughputs: list[int] = []
            bucket_transactions = 0
            prior_end = start
            bucket_width: int | None = None
            for index, bucket in enumerate(buckets):
                if set(bucket) != {
                    "bucket_index",
                    "start_ns",
                    "end_ns",
                    "transactions",
                    "mean_milli_tps",
                }:
                    _error("scientific bucket schema drifted")
                bucket_start = _integer(bucket.get("start_ns"), "bucket start")
                bucket_end = _integer(bucket.get("end_ns"), "bucket end", minimum=1)
                count = _integer(bucket.get("transactions"), "bucket transactions")
                throughput = _integer(bucket.get("mean_milli_tps"), "bucket throughput")
                duration = bucket_end - bucket_start
                if (
                    bucket.get("bucket_index") != index
                    or bucket_start != prior_end
                    or duration <= 0
                    or (bucket_width is not None and duration != bucket_width)
                    or throughput != count * 1_000_000_000_000 // duration
                ):
                    _error("scientific bucket identity or throughput drifted")
                bucket_width = duration
                prior_end = bucket_end
                bucket_transactions += count
                throughputs.append(throughput)
            ordered = sorted(throughputs)
            median_sum = ordered[2] + ordered[3]
            if (
                prior_end != end
                or bucket_transactions != transactions
                or median_sum % 2
                or phase.get("median_milli_tps") != median_sum // 2
            ):
                _error("scientific phase bucket aggregation drifted")
        normalized.append(dict(phase))
    late = _integer(
        measurements.get("late_window_throughput_milli_tps"),
        "late-window throughput",
    )
    expected_late = normalized[-1][
        "median_milli_tps" if extended_schema else "mean_milli_tps"
    ]
    if late != expected_late:
        _error("late-window throughput differs from its raw phase")
    return {"phases": normalized, "late_window_throughput_milli_tps": late}


def _scientific_support_contract(
    sealed_child_directory: Path,
) -> dict[str, object] | None:
    """Read the explicit opt-in and thresholds from one verified child profile."""

    profile_path = sealed_child_directory / "profile.json"
    if not profile_path.exists():
        return None
    if profile_path.is_symlink() or not profile_path.is_file():
        _error("scientific-support child profile is not a regular file")
    try:
        profile = _mapping(
            json.loads(profile_path.read_text(encoding="utf-8")),
            "scientific-support child profile",
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise N31CrashPairCampaignError(
            "scientific-support child profile is unreadable"
        ) from error
    campaign = profile.get("campaign")
    if not isinstance(campaign, Mapping):
        return None
    raw_contract = campaign.get("scientific_support_contract")
    if raw_contract is None:
        return None
    contract = _mapping(raw_contract, "scientific-support contract")
    if dict(contract) != _SCIENTIFIC_SUPPORT_CONTRACT:
        _error("scientific-support contract is not the reviewed exact opt-in")
    thresholds = _mapping(profile.get("thresholds"), "scientific-support thresholds")
    if set(thresholds) != _SCIENTIFIC_THRESHOLD_KEYS:
        _error("scientific-support threshold schema drifted")
    normalized_thresholds = {
        key: _integer(thresholds.get(key), f"scientific-support {key}", minimum=1)
        for key in sorted(_SCIENTIFIC_THRESHOLD_KEYS)
    }
    return {
        **_SCIENTIFIC_SUPPORT_CONTRACT,
        "thresholds_ppm": normalized_thresholds,
    }


def _ratio_ppm(numerator: int, denominator: int) -> int | None:
    if denominator == 0:
        return None
    return numerator * _PPM_SCALE // denominator


def _phase_tps(validation: Mapping[str, Any]) -> dict[str, int]:
    measurements = _mapping(
        validation.get("scientific_measurements"), "scientific measurements"
    )
    phases = _sequence(measurements.get("phases"), "scientific phase rows")
    return {
        str(_mapping(phase, "scientific phase row")["phase"]): _integer(
            _mapping(phase, "scientific phase row").get(
                "median_milli_tps"
                if "median_milli_tps" in _mapping(phase, "scientific phase row")
                else "mean_milli_tps"
            ),
            "scientific phase throughput",
        )
        for phase in phases
    }


def _median_ratio_ppm(ratios: Sequence[tuple[int, int]]) -> int | None:
    if len(ratios) != _PAIR_COUNT or any(denominator == 0 for _, denominator in ratios):
        return None
    ordered = sorted(
        (Fraction(numerator, denominator), numerator, denominator)
        for numerator, denominator in ratios
    )
    _fraction, numerator, denominator = ordered[_PAIR_COUNT // 2]
    return _ratio_ppm(numerator, denominator)


def _median_meets_threshold(
    ratios: Sequence[tuple[int, int]], threshold_ppm: int
) -> bool:
    if len(ratios) != _PAIR_COUNT or any(denominator == 0 for _, denominator in ratios):
        return False
    ordered = sorted(
        Fraction(numerator, denominator) for numerator, denominator in ratios
    )
    median = ordered[_PAIR_COUNT // 2]
    return median.numerator * _PPM_SCALE >= threshold_ppm * median.denominator


def _evaluate_scientific_support(
    by_pair: Mapping[str, Mapping[str, Mapping[str, Any]]],
    pair_verdicts: list[dict[str, object]],
    thresholds: Mapping[str, Any],
) -> dict[str, object]:
    """Evaluate the opt-in v9 recovery and paired-improvement claim gate."""

    containment_threshold = _integer(
        thresholds.get("containment_over_baseline_min_ppm"),
        "containment-over-baseline threshold",
        minimum=1,
    )
    adaptive_threshold = _integer(
        thresholds.get("adaptive_ratio_min_ppm"),
        "adaptive-ratio threshold",
        minimum=1,
    )
    paired_threshold = _integer(
        thresholds.get("paired_ratio_min_ppm"),
        "paired-ratio threshold",
        minimum=1,
    )
    adaptive_ratios: list[tuple[int, int]] = []
    paired_ratios: list[tuple[int, int]] = []
    containment_pass_count = 0
    adaptive_positive_count = 0
    paired_positive_count = 0
    verdict_by_pair = {str(verdict["pair_id"]): verdict for verdict in pair_verdicts}
    for pair_ordinal in range(1, _PAIR_COUNT + 1):
        pair_id = f"pair-{pair_ordinal:02d}"
        arms = by_pair[pair_id]
        control = _phase_tps(arms["control"])
        adaptive = _phase_tps(arms["adaptive"])
        control_containment = (control["epoch1"], control["baseline"])
        adaptive_containment = (adaptive["epoch1"], adaptive["baseline"])
        adaptive_ratio = (adaptive["late"], adaptive["epoch1"])
        paired_ratio = (
            adaptive["late"] * control["epoch1"],
            adaptive["epoch1"] * control["late"],
        )
        adaptive_ratios.append(adaptive_ratio)
        paired_ratios.append(paired_ratio)
        control_containment_pass = (
            control_containment[1] > 0
            and control_containment[0] * _PPM_SCALE
            >= containment_threshold * control_containment[1]
        )
        adaptive_containment_pass = (
            adaptive_containment[1] > 0
            and adaptive_containment[0] * _PPM_SCALE
            >= containment_threshold * adaptive_containment[1]
        )
        containment_pass_count += int(control_containment_pass)
        containment_pass_count += int(adaptive_containment_pass)
        adaptive_positive = (
            adaptive_ratio[1] > 0 and adaptive_ratio[0] > adaptive_ratio[1]
        )
        paired_positive = paired_ratio[1] > 0 and paired_ratio[0] > paired_ratio[1]
        adaptive_positive_count += int(adaptive_positive)
        paired_positive_count += int(paired_positive)
        verdict_by_pair[pair_id].update(
            {
                "control_containment_over_baseline_ppm": _ratio_ppm(
                    *control_containment
                ),
                "adaptive_containment_over_baseline_ppm": _ratio_ppm(
                    *adaptive_containment
                ),
                "adaptive_ratio_ppm": _ratio_ppm(*adaptive_ratio),
                "paired_ratio_ppm": _ratio_ppm(*paired_ratio),
                "control_containment_support": control_containment_pass,
                "adaptive_containment_support": adaptive_containment_pass,
                "adaptive_ratio_positive": adaptive_positive,
                "paired_ratio_positive": paired_positive,
            }
        )
    median_adaptive = _median_ratio_ppm(adaptive_ratios)
    median_paired = _median_ratio_ppm(paired_ratios)
    requirements = {
        "all_arms_containment_over_baseline": containment_pass_count
        == _CLAIM_SLOT_COUNT,
        "adaptive_positive_at_least_four_of_five": adaptive_positive_count >= 4,
        "adaptive_median_meets_threshold": _median_meets_threshold(
            adaptive_ratios, adaptive_threshold
        ),
        "paired_positive_at_least_four_of_five": paired_positive_count >= 4,
        "paired_median_meets_threshold": _median_meets_threshold(
            paired_ratios, paired_threshold
        ),
    }
    return {
        "schema_version": 1,
        "domain": _SCIENTIFIC_SUPPORT_DOMAIN,
        "thresholds_ppm": {
            key: int(thresholds[key]) for key in sorted(_SCIENTIFIC_THRESHOLD_KEYS)
        },
        "containment_pass_count": containment_pass_count,
        "adaptive_positive_pair_count": adaptive_positive_count,
        "paired_positive_pair_count": paired_positive_count,
        "median_adaptive_ratio_ppm": median_adaptive,
        "median_paired_ratio_ppm": median_paired,
        "requirements": requirements,
        "supported": all(requirements.values()),
    }


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
        tuple[
            Mapping[str, Any],
            Mapping[str, Any],
            Mapping[str, Any],
            dict[str, object] | None,
        ]
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
            try:
                post_validation_seal = verify_evidence_seal(isolated)
            except (EvidenceSealError, OSError) as error:
                raise N31CrashPairCampaignError(
                    "source-blind child mutated during validation"
                ) from error
            if (
                post_validation_seal.tree_sha256 != child["child_tree_sha256"]
                or post_validation_seal.seal_sha256 != child["child_seal_sha256"]
            ):
                _error("source-blind child mutated during validation")
            scientific_support_contract = _scientific_support_contract(isolated)
            observations.append(
                (
                    child,
                    slot,
                    {**dict(validation), "scientific_measurements": measurements},
                    scientific_support_contract,
                )
            )

    support_contracts = [observation[3] for observation in observations]
    support_enabled = any(contract is not None for contract in support_contracts)
    support_thresholds: Mapping[str, Any] | None = None
    if support_enabled:
        if any(contract is None for contract in support_contracts):
            _error("scientific-support opt-in is missing from one or more children")
        first_contract = support_contracts[0]
        if first_contract is None:
            _error("scientific-support contract collection is inconsistent")
        if any(contract != first_contract for contract in support_contracts[1:]):
            _error("scientific-support contracts or thresholds differ across children")
        support_thresholds = _mapping(
            first_contract.get("thresholds_ppm"),
            "scientific-support campaign thresholds",
        )
        if any(
            any(
                "median_milli_tps"
                not in _mapping(phase, "scientific-support phase row")
                for phase in _sequence(
                    _mapping(
                        observation[2].get("scientific_measurements"),
                        "scientific-support measurements",
                    ).get("phases"),
                    "scientific-support phases",
                )
            )
            for observation in observations
        ):
            _error("scientific-support measurements lack stable-phase medians")

    child_verdicts: list[dict[str, object]] = []
    by_pair: dict[str, dict[str, dict[str, object]]] = {}
    for child, slot, validation, _support_contract in observations:
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
    scientific_support = (
        None
        if support_thresholds is None
        else _evaluate_scientific_support(
            by_pair,
            pair_verdicts,
            support_thresholds,
        )
    )
    child_verdicts.sort(key=lambda row: int(str(row["slot_id"]).removeprefix("slot-")))
    all_pass = all(verdict.get("outcome") == "PASS" for verdict in child_verdicts)
    result: dict[str, object] = {
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
    if scientific_support is not None:
        result["scientific_support"] = scientific_support
        result["claim_eligible"] = bool(
            all_pass and scientific_support["supported"] is True
        )
    return result


__all__ = [
    "N31CrashPairCampaignError",
    "derive_focused_campaign_plan",
    "validate_campaign_ledger",
    "validate_campaign_plan",
    "validate_campaign_source_blind",
]
