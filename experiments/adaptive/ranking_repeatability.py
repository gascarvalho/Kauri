#!/usr/bin/env python3
"""Produce the frozen descriptive CERT13 ranking-repeatability audit."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.adaptive.kauri_experiment import factorial_validation  # noqa: E402
from experiments.adaptive.kauri_experiment.profiled_fault_archive import (  # noqa: E402
    EvidenceSealError,
    verify_evidence_seal,
)


DOMAIN = "kauri-cert13-ranking-repeatability-v1"
ANALYSIS_ID = "cert13-ranking-repeatability-v1"
GENERATOR_ID = "experiments/adaptive/ranking_repeatability.py"
CAMPAIGN_ID = "cert13-n31-campaign-v13-7adabc83-r50"
CAMPAIGN_REVISION = "7adabc838129082486e33d830d1d09cd0f2919e5"
EXPECTED_CAMPAIGN_TREE_SHA256 = (
    "a150b65d2938f8912917734dfd575c713519e378191470474604d71fc7196423"
)
EXPECTED_CAMPAIGN_SEAL_SHA256 = (
    "b14c991a5f964434e85ec306c78ed4b0fb02ea96ec0fad28726c5a557b7bd470"
)
EXPECTED_PLAN_SHA256 = (
    "1429971a2697bd111e3e1576c0cb7158d64d88c8bb361cc75f6629414ee8e820"
)
EXPECTED_PLACEMENT_CONTRAST_SHA256 = (
    "10121fbb0f1c59f634049d14e75877d102e7df0815cc318c55081fd427efd07c"
)
EXPECTED_CAMPAIGN_VALIDATION_RAW_SHA256 = (
    "062e140e29b2684c58eb4322cc1b77897b7784a4ce63659f3f228bf2110d5b0a"
)
EXPECTED_CAMPAIGN_VALIDATION_CANONICAL_SHA256 = (
    "6d245dda770c32ad8fb427289003dc478fd765e461ec8aa94d4b4f904034aa18"
)
PAIR_IDS = tuple(f"pair-{index:02d}" for index in range(1, 6))
PAIR_TO_ADAPTIVE_SLOT = {
    "pair-01": "slot-02",
    "pair-02": "slot-10",
    "pair-03": "slot-06",
    "pair-04": "slot-03",
    "pair-05": "slot-07",
}
CLAIM_BOUNDARY = (
    "Descriptive reanalysis of accepted CERT13 evidence only; "
    "C-007 remains rejected and unchanged."
)
LIMITATIONS = [
    "The audit describes five repetitions of one fixed N=31 scenario and does not "
    "estimate population-level ranking stability.",
    "Ranking repeatability is not a causal estimator of throughput.",
    "All repeatability metrics use truncated 21-root slates from 28 survivors; "
    "Kendall tau uses only each pairwise intersection, so omitted-survivor order "
    "and score margins are unrecoverable.",
    "No p-value, correlation, or causal attribution is computed.",
]


class RankingRepeatabilityError(ValueError):
    """The frozen accepted inputs cannot support the W13 audit."""


def canonical_json(value: object) -> str:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _git_bytes(*arguments: str) -> bytes:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout
    except subprocess.CalledProcessError as exc:
        raise RankingRepeatabilityError(
            f"cannot resolve committed producer provenance: git {' '.join(arguments)}"
        ) from exc


def _committed_source_provenance() -> dict[str, str]:
    revision = _git_bytes("rev-parse", "HEAD").decode("ascii").strip()
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise RankingRepeatabilityError("analysis revision is not a full Git SHA-1")
    producer_path = (ROOT / GENERATOR_ID).resolve(strict=True)
    if producer_path != Path(__file__).resolve(strict=True):
        raise RankingRepeatabilityError("producer path differs from the frozen generator ID")
    worktree_bytes = _read_regular_bytes(producer_path, "ranking-repeatability producer")
    committed_bytes = _git_bytes("show", f"{revision}:{GENERATOR_ID}")
    if committed_bytes != worktree_bytes:
        raise RankingRepeatabilityError(
            "ranking-repeatability producer differs from the recorded analysis revision"
        )
    return {
        "analysis_revision": revision,
        "generator_source_sha256": _sha256_bytes(worktree_bytes),
    }


def _read_regular_bytes(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise RankingRepeatabilityError(f"{label} must be a regular file")
    return path.read_bytes()


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    raw = _read_regular_bytes(path, label)
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise RankingRepeatabilityError(f"{label} is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise RankingRepeatabilityError(f"{label} must be a JSON object")
    return value


def _require_exact_hash(path: Path, expected: str, label: str) -> str:
    actual = _sha256_bytes(_read_regular_bytes(path, label))
    if actual != expected:
        raise RankingRepeatabilityError(
            f"{label} SHA-256 differs: expected {expected}, got {actual}"
        )
    return actual


def _integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise RankingRepeatabilityError(f"{label} must be an integer")
    return value


def _root_list(value: object, label: str) -> list[int]:
    if (
        not isinstance(value, list)
        or len(value) != 21
        or any(type(replica) is not int for replica in value)
        or len(set(value)) != 21
        or any(replica < 0 or replica >= 31 for replica in value)
    ):
        raise RankingRepeatabilityError(f"{label} must contain 21 unique N31 IDs")
    return list(value)


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise RankingRepeatabilityError(f"{path} is outside the campaign root") from exc


def _decode_roots(child: Path, epoch: int) -> list[int]:
    issuer_path = child / "raw" / "issuer-public-key.txt"
    issuer = _read_regular_bytes(issuer_path, "issuer public key").decode("ascii").strip()
    bundle_path = child / "raw" / f"epoch{epoch}.bundle"
    bundle = _read_regular_bytes(bundle_path, f"epoch {epoch} bundle")
    try:
        decoded = factorial_validation.decode_adaptive_v3_epoch_change_bundle(
            bundle, issuer_public_key=issuer
        )
    except factorial_validation.FactorialValidationError as exc:
        raise RankingRepeatabilityError(f"epoch {epoch} bundle is invalid") from exc
    if (
        decoded.epoch_number != epoch
        or tuple(tree.tree_id for tree in decoded.trees) != tuple(range(21))
        or any(len(tree.members) != 31 for tree in decoded.trees)
    ):
        raise RankingRepeatabilityError(f"epoch {epoch} bundle shape is invalid")
    return [tree.members[0] for tree in decoded.trees]


def _raw_optimization_event(stream_path: Path) -> Mapping[str, Any]:
    raw = _read_regular_bytes(stream_path, "raw manager event stream")
    events: list[Mapping[str, Any]] = []
    for line_number, line in enumerate(raw.splitlines(), start=1):
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise RankingRepeatabilityError(
                f"raw manager event stream line {line_number} is invalid JSON"
            ) from exc
        if not isinstance(event, Mapping):
            raise RankingRepeatabilityError(
                f"raw manager event stream line {line_number} is not an object"
            )
        payload = event.get("payload")
        if (
            event.get("event_type") == "adaptive_v2_evidence_snapshot"
            and isinstance(payload, Mapping)
            and payload.get("cycle_ordinal") == 1
            and payload.get("policy_intent") == "performance_optimization"
            and payload.get("transition_artifact_id") == "e1-to-e2-optimization"
        ):
            events.append(event)
    if len(events) != 1:
        raise RankingRepeatabilityError(
            "raw manager event stream must contain exactly one E2 optimization snapshot"
        )
    return events[0]


def _pairwise(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, object]:
    left_roots = list(left["raw_eligible_ranking"])
    right_roots = list(right["raw_eligible_ranking"])
    common = set(left_roots) & set(right_roots)
    union = set(left_roots) | set(right_roots)
    left_common = [replica for replica in left_roots if replica in common]
    right_position = {replica: index for index, replica in enumerate(right_roots)}
    concordant = 0
    discordant = 0
    for left_index, first in enumerate(left_common):
        for second in left_common[left_index + 1 :]:
            if right_position[first] < right_position[second]:
                concordant += 1
            else:
                discordant += 1
    denominator = concordant + discordant
    numerator = concordant - discordant
    if denominator == 0:
        raise RankingRepeatabilityError("Kendall comparison has no comparable pair")
    return {
        "left_pair_id": left["pair_id"],
        "right_pair_id": right["pair_id"],
        "common_root_count": len(common),
        "union_root_count": len(union),
        "jaccard": {
            "numerator": len(common),
            "denominator": len(union),
            "decimal": len(common) / len(union),
        },
        "kendall_common": {
            "common_item_count": len(common),
            "comparable_pair_count": denominator,
            "concordant_pair_count": concordant,
            "discordant_pair_count": discordant,
            "numerator": numerator,
            "denominator": denominator,
            "tau": numerator / denominator,
        },
        "exact_position_match_count": sum(
            left_replica == right_replica
            for left_replica, right_replica in zip(
                left_roots, right_roots, strict=True
            )
        ),
    }


def _range_summary(values: Sequence[int | float]) -> dict[str, int | float]:
    return {
        "minimum": min(values),
        "median": statistics.median(values),
        "maximum": max(values),
    }


def build_report(
    campaign_root: Path,
    campaign_validation_path: Path,
    placement_contrast_path: Path,
    output_path: Path,
) -> dict[str, object]:
    root = campaign_root.resolve(strict=True)
    campaign_validation_path = campaign_validation_path.resolve(strict=True)
    placement_contrast_path = placement_contrast_path.resolve(strict=True)
    output_path = output_path.resolve(strict=False)
    if root.name != CAMPAIGN_ID:
        raise RankingRepeatabilityError("campaign root is not the frozen CERT13 campaign")
    source_provenance = _committed_source_provenance()
    try:
        seal = verify_evidence_seal(root)
    except (EvidenceSealError, OSError) as exc:
        raise RankingRepeatabilityError("campaign evidence seal is invalid") from exc
    if (
        seal.tree_sha256 != EXPECTED_CAMPAIGN_TREE_SHA256
        or seal.seal_sha256 != EXPECTED_CAMPAIGN_SEAL_SHA256
    ):
        raise RankingRepeatabilityError("campaign seal differs from accepted CERT13")

    plan_path = root / "plan.json"
    _require_exact_hash(plan_path, EXPECTED_PLAN_SHA256, "campaign plan")
    plan = _read_json(plan_path, "campaign plan")
    if plan.get("revision") != CAMPAIGN_REVISION or plan.get("pair_count") != 5:
        raise RankingRepeatabilityError("campaign plan identity differs from CERT13")

    _require_exact_hash(
        placement_contrast_path,
        EXPECTED_PLACEMENT_CONTRAST_SHA256,
        "placement contrast",
    )
    placement = _read_json(placement_contrast_path, "placement contrast")
    if (
        placement.get("verdict") != "PASS"
        or placement.get("audit_id") != "cert13-placement-contrast-v1"
        or placement.get("claim_boundary")
        != "Descriptive accepted-artifact analysis only; C-007 remains rejected and unchanged."
    ):
        raise RankingRepeatabilityError("placement contrast is outside its accepted boundary")

    validation_raw = _read_regular_bytes(campaign_validation_path, "campaign validation")
    validation_raw_sha256 = _sha256_bytes(validation_raw)
    if validation_raw_sha256 != EXPECTED_CAMPAIGN_VALIDATION_RAW_SHA256:
        raise RankingRepeatabilityError("campaign validation raw bytes differ from accepted input")
    validation = _read_json(campaign_validation_path, "campaign validation")
    validation_canonical_sha256 = _sha256_bytes(canonical_json(validation).encode("ascii"))
    if validation_canonical_sha256 != EXPECTED_CAMPAIGN_VALIDATION_CANONICAL_SHA256:
        raise RankingRepeatabilityError("campaign validation semantics differ from accepted input")
    if (
        validation.get("verdict") != "PASS"
        or validation.get("pair_count") != 5
        or validation.get("figure_eligible") is not True
        or validation.get("claim_eligible") is not False
    ):
        raise RankingRepeatabilityError("campaign validation is not the accepted C-004 boundary")

    placement_arms = placement.get("arms")
    validation_pairs = validation.get("pairs")
    if not isinstance(placement_arms, list) or len(placement_arms) != 5:
        raise RankingRepeatabilityError("placement contrast must contain five arms")
    if not isinstance(validation_pairs, list) or len(validation_pairs) != 5:
        raise RankingRepeatabilityError("campaign validation must contain five pairs")
    arm_by_pair = {
        arm.get("pair_id"): arm
        for arm in placement_arms
        if isinstance(arm, Mapping)
    }
    validation_by_pair = {
        row.get("pair_id"): row
        for row in validation_pairs
        if isinstance(row, Mapping)
    }
    if set(arm_by_pair) != set(PAIR_IDS) or set(validation_by_pair) != set(PAIR_IDS):
        raise RankingRepeatabilityError("accepted inputs do not cover the exact five pairs")

    pair_rows: list[dict[str, object]] = []
    for pair_index, pair_id in enumerate(PAIR_IDS, start=1):
        slot_id = PAIR_TO_ADAPTIVE_SLOT[pair_id]
        arm = arm_by_pair[pair_id]
        outcome = validation_by_pair[pair_id]
        if arm.get("slot_id") != slot_id:
            raise RankingRepeatabilityError(f"{pair_id} adaptive slot differs from frozen plan")
        child = root / "children" / slot_id
        try:
            child_seal = verify_evidence_seal(child)
        except (EvidenceSealError, OSError) as exc:
            raise RankingRepeatabilityError(f"{slot_id} child seal is invalid") from exc

        arm_source = arm.get("source")
        if not isinstance(arm_source, Mapping):
            raise RankingRepeatabilityError(f"{pair_id} W9 source binding is absent")
        if (
            child_seal.tree_sha256 != arm_source.get("child_tree_sha256")
            or child_seal.seal_sha256 != arm_source.get("child_seal_sha256")
        ):
            raise RankingRepeatabilityError(f"{pair_id} child seal differs from W9")

        w9_epoch1_roots = _root_list(arm.get("epoch1_roots"), f"{pair_id} W9 E1 roots")
        w9_epoch2_roots = _root_list(arm.get("epoch2_roots"), f"{pair_id} W9 E2 roots")
        if _decode_roots(child, 1) != w9_epoch1_roots:
            raise RankingRepeatabilityError(f"{pair_id} W9 E1 roots differ from bundle")
        if _decode_roots(child, 2) != w9_epoch2_roots:
            raise RankingRepeatabilityError(f"{pair_id} W9 E2 roots differ from bundle")

        raw_stream = child / "raw" / "adaptive-manager-events.jsonl"
        event = _raw_optimization_event(raw_stream)
        payload = event["payload"]
        if not isinstance(payload, Mapping):
            raise RankingRepeatabilityError(f"{pair_id} raw E2 payload is absent")
        raw_ranking = _root_list(
            payload.get("eligible_ranking"), f"{pair_id} raw eligible ranking"
        )
        if raw_ranking != w9_epoch2_roots:
            raise RankingRepeatabilityError(f"{pair_id} raw ranking differs from W9")

        materialized_path = (
            child
            / "transitions"
            / "e1-to-e2-optimization"
            / "evidence-snapshot.json"
        )
        materialized = _read_json(materialized_path, f"{pair_id} materialized snapshot")
        if (
            materialized.get("eligible_ranking") != raw_ranking
            or materialized.get("evidence_snapshot_id")
            != payload.get("evidence_snapshot_id")
            or materialized.get("current_cutoff") != payload.get("current_cutoff")
        ):
            raise RankingRepeatabilityError(
                f"{pair_id} materialized snapshot differs from raw event"
            )
        bound_paths = {
            "epoch1_bundle_sha256": child / "raw" / "epoch1.bundle",
            "epoch2_bundle_sha256": child / "raw" / "epoch2.bundle",
            "epoch2_snapshot_sha256": materialized_path,
        }
        for field, bound_path in bound_paths.items():
            expected = arm_source.get(field)
            if not isinstance(expected, str) or _sha256_bytes(
                _read_regular_bytes(bound_path, f"{pair_id} {field}")
            ) != expected:
                raise RankingRepeatabilityError(
                    f"{pair_id} {field} differs from W9 source binding"
                )

        adaptive_late = _integer(
            outcome.get("adaptive_late_window_throughput_milli_tps"),
            f"{pair_id} adaptive late throughput",
        )
        control_late = _integer(
            outcome.get("control_late_window_throughput_milli_tps"),
            f"{pair_id} control late throughput",
        )
        direct_effect = _integer(
            outcome.get("effect_milli_tps"), f"{pair_id} direct effect"
        )
        if direct_effect != adaptive_late - control_late:
            raise RankingRepeatabilityError(f"{pair_id} direct effect is inconsistent")
        paired_ratio_ppm = _integer(
            outcome.get("paired_ratio_ppm"), f"{pair_id} paired ratio"
        )
        throughput = {
            "adaptive_late_window_throughput_milli_tps": adaptive_late,
            "control_late_window_throughput_milli_tps": control_late,
            "direct_effect_milli_tps": direct_effect,
            "adaptive_containment_over_baseline_ppm": _integer(
                outcome.get("adaptive_containment_over_baseline_ppm"),
                f"{pair_id} adaptive containment ratio",
            ),
            "control_containment_over_baseline_ppm": _integer(
                outcome.get("control_containment_over_baseline_ppm"),
                f"{pair_id} control containment ratio",
            ),
            "adaptive_ratio_ppm": _integer(
                outcome.get("adaptive_ratio_ppm"), f"{pair_id} adaptive ratio"
            ),
            "paired_ratio_ppm": paired_ratio_ppm,
            "adjusted_effect_ppm": paired_ratio_ppm - 1_000_000,
            "scientific_outcome": outcome.get("scientific_outcome"),
        }
        pair_rows.append(
            {
                "pair_index": pair_index,
                "pair_id": pair_id,
                "adaptive_slot_id": slot_id,
                "adaptive_child_path": _relative(child, root),
                "raw_snapshot": {
                    "path": _relative(raw_stream, root),
                    "raw_sha256": _sha256_bytes(
                        _read_regular_bytes(raw_stream, "raw manager event stream")
                    ),
                    "source_sequence": _integer(
                        event.get("source_sequence"), f"{pair_id} source sequence"
                    ),
                    "evidence_snapshot_id": payload.get("evidence_snapshot_id"),
                    "baseline_cutoff": _integer(
                        payload.get("baseline_cutoff"), f"{pair_id} baseline cutoff"
                    ),
                    "current_cutoff": _integer(
                        payload.get("current_cutoff"), f"{pair_id} current cutoff"
                    ),
                },
                "materialized_snapshot": {
                    "path": _relative(materialized_path, root),
                    "raw_sha256": _sha256_bytes(
                        _read_regular_bytes(materialized_path, "materialized snapshot")
                    ),
                },
                "epoch1_bundle": {
                    "path": _relative(child / "raw" / "epoch1.bundle", root),
                    "raw_sha256": _sha256_bytes(
                        _read_regular_bytes(child / "raw" / "epoch1.bundle", "E1 bundle")
                    ),
                },
                "epoch2_bundle": {
                    "path": _relative(child / "raw" / "epoch2.bundle", root),
                    "raw_sha256": _sha256_bytes(
                        _read_regular_bytes(child / "raw" / "epoch2.bundle", "E2 bundle")
                    ),
                },
                "w9_epoch1_roots": w9_epoch1_roots,
                "w9_epoch2_roots": w9_epoch2_roots,
                "raw_eligible_ranking": raw_ranking,
                "ranking_matches_w9_epoch2_roots": True,
                "w9_changed_root_position_count": _integer(
                    arm.get("changed_root_position_count"),
                    f"{pair_id} changed root count",
                ),
                "w9_root_set_turnover_count": _integer(
                    arm.get("root_set_turnover_count"), f"{pair_id} turnover count"
                ),
                "throughput": throughput,
            }
        )

    comparisons = [
        _pairwise(pair_rows[left], pair_rows[right])
        for left in range(len(pair_rows))
        for right in range(left + 1, len(pair_rows))
    ]
    rankings = [set(row["raw_eligible_ranking"]) for row in pair_rows]
    survivor_union = sorted(set().union(*rankings))
    survivor_intersection = sorted(set.intersection(*rankings))
    frequencies = [
        {
            "replica_id": replica,
            "count": sum(replica in ranking for ranking in rankings),
        }
        for replica in survivor_union
    ]
    histogram = [
        {
            "selection_count": count,
            "replica_count": sum(row["count"] == count for row in frequencies),
        }
        for count in range(1, 6)
        if any(row["count"] == count for row in frequencies)
    ]
    paired_ratios = [
        int(row["throughput"]["paired_ratio_ppm"])  # type: ignore[index]
        for row in pair_rows
    ]
    aggregate = {
        "pair_count": 5,
        "pairwise_comparison_count": 10,
        "root_count_per_ranking": 21,
        "survivor_union_ids": survivor_union,
        "survivor_intersection_ids": survivor_intersection,
        "selection_frequency": frequencies,
        "selection_count_histogram": histogram,
        "root_set_overlap": _range_summary(
            [int(row["common_root_count"]) for row in comparisons]
        ),
        "jaccard": _range_summary(
            [float(row["jaccard"]["decimal"]) for row in comparisons]  # type: ignore[index]
        ),
        "kendall_common_tau": _range_summary(
            [float(row["kendall_common"]["tau"]) for row in comparisons]  # type: ignore[index]
        ),
        "exact_position_matches": _range_summary(
            [int(row["exact_position_match_count"]) for row in comparisons]
        ),
        "validated_paired_ratio_ppm": {
            "values": paired_ratios,
            "median": statistics.median(paired_ratios),
            "negative_count": sum(value < 1_000_000 for value in paired_ratios),
            "positive_count": sum(value > 1_000_000 for value in paired_ratios),
            "zero_count": sum(value == 1_000_000 for value in paired_ratios),
        },
    }
    invocation = {
        "argv": [
            GENERATOR_ID,
            "--campaign-root",
            str(root),
            "--campaign-validation",
            str(campaign_validation_path),
            "--placement-contrast",
            str(placement_contrast_path),
            "--output",
            str(output_path),
        ],
        "resolved_campaign_root": str(root),
        "resolved_campaign_validation": str(campaign_validation_path),
        "resolved_placement_contrast": str(placement_contrast_path),
        "resolved_output": str(output_path),
    }
    return {
        "schema_version": 1,
        "domain": DOMAIN,
        "verdict": "PASS",
        "analysis_id": ANALYSIS_ID,
        "generator_id": GENERATOR_ID,
        "invocation": invocation,
        "source": {
            **source_provenance,
            "campaign_id": CAMPAIGN_ID,
            "campaign_revision": CAMPAIGN_REVISION,
            "campaign_root": str(root),
            "campaign_tree_sha256": seal.tree_sha256,
            "campaign_seal_sha256": seal.seal_sha256,
            "plan": {
                "path": _relative(plan_path, root),
                "raw_sha256": EXPECTED_PLAN_SHA256,
            },
            "placement_contrast": {
                "path": str(placement_contrast_path),
                "raw_sha256": EXPECTED_PLACEMENT_CONTRAST_SHA256,
            },
            "campaign_validation": {
                "path": str(campaign_validation_path),
                "raw_sha256": validation_raw_sha256,
                "canonical_sha256": validation_canonical_sha256,
            },
        },
        "fixed_denominator": {
            "pair_ids": list(PAIR_IDS),
            "adaptive_slot_by_pair": dict(PAIR_TO_ADAPTIVE_SLOT),
            "tree_root_position_count": 21,
            "responsive_survivor_count": 28,
            "crashed_replica_ids": [21, 22, 23],
        },
        "pairs": pair_rows,
        "pairwise_comparisons": comparisons,
        "aggregate": aggregate,
        "claim_boundary": CLAIM_BOUNDARY,
        "limitations": LIMITATIONS,
    }


def write_report(
    campaign_root: Path,
    campaign_validation_path: Path,
    placement_contrast_path: Path,
    output_path: Path,
) -> Path:
    output = output_path.resolve(strict=False)
    root = campaign_root.resolve(strict=True)
    try:
        output.relative_to(root)
    except ValueError:
        pass
    else:
        raise RankingRepeatabilityError("output must be outside the sealed campaign")
    if output.exists() or output.is_symlink():
        raise RankingRepeatabilityError("output already exists")
    report = build_report(
        root,
        campaign_validation_path,
        placement_contrast_path,
        output,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="ascii", newline="") as destination:
        destination.write(canonical_json(report))
    return output


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", required=True, type=Path)
    parser.add_argument("--campaign-validation", required=True, type=Path)
    parser.add_argument("--placement-contrast", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--expected-campaign-validation-sha256",
        default=EXPECTED_CAMPAIGN_VALIDATION_RAW_SHA256,
    )
    parser.add_argument(
        "--expected-placement-contrast-sha256",
        default=EXPECTED_PLACEMENT_CONTRAST_SHA256,
    )
    args = parser.parse_args(argv)
    if args.expected_campaign_validation_sha256 != EXPECTED_CAMPAIGN_VALIDATION_RAW_SHA256:
        parser.error("expected campaign-validation SHA-256 differs from frozen input")
    if args.expected_placement_contrast_sha256 != EXPECTED_PLACEMENT_CONTRAST_SHA256:
        parser.error("expected placement-contrast SHA-256 differs from frozen input")
    try:
        path = write_report(
            args.campaign_root,
            args.campaign_validation,
            args.placement_contrast,
            args.output,
        )
    except (OSError, RankingRepeatabilityError) as exc:
        print(f"ranking-repeatability rejected: {exc}", file=sys.stderr)
        return 2
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
