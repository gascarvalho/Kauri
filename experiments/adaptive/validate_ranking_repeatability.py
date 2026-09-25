#!/usr/bin/env python3
"""Independently verify the frozen CERT13 ranking-repeatability artifact."""

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
VERIFIER_ID = "cert13-ranking-repeatability-independent-v1"
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


class VerificationError(ValueError):
    """The artifact or its source evidence does not satisfy the frozen contract."""


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


def _regular_bytes(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise VerificationError(f"{label} must be a regular file")
    return path.read_bytes()


def _raw_sha256(path: Path, label: str) -> str:
    return hashlib.sha256(_regular_bytes(path, label)).hexdigest()


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    return _parse_json(_regular_bytes(path, label), label)


def _parse_json(raw: bytes, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise VerificationError(f"{label} is invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise VerificationError(f"{label} must be a JSON object")
    return value


def _read_pinned_json(
    path: Path, expected_sha256: str, label: str
) -> tuple[Mapping[str, Any], str]:
    raw = _regular_bytes(path, label)
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256:
        raise VerificationError(
            f"{label} SHA-256 differs: expected {expected_sha256}, got {actual}"
        )
    return _parse_json(raw, label), actual


def _git_bytes(*arguments: str) -> bytes:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise VerificationError(
            f"cannot resolve committed verifier provenance: git {' '.join(arguments)}"
        ) from exc


def _committed_source_provenance(path: Path, label: str) -> tuple[str, str]:
    """Bind source bytes to the current committed Kauri HEAD."""
    source = path.resolve(strict=True)
    try:
        relative = source.relative_to(ROOT).as_posix()
    except ValueError as exc:
        raise VerificationError(f"{label} source is outside the Kauri repository") from exc
    try:
        head = _git_bytes("rev-parse", "HEAD").decode("ascii").strip()
    except UnicodeError as exc:
        raise VerificationError(f"{label} source is not available at Kauri HEAD") from exc
    if len(head) != 40 or any(character not in "0123456789abcdef" for character in head):
        raise VerificationError("analysis revision is not a full Git SHA-1")
    committed = _git_bytes("show", f"{head}:{relative}")
    raw = _regular_bytes(source, f"{label} source")
    if raw != committed:
        raise VerificationError(f"{label} source differs from committed Kauri HEAD")
    return head, hashlib.sha256(raw).hexdigest()


def _read_canonical_artifact(path: Path) -> Mapping[str, Any]:
    raw = _regular_bytes(path, "ranking-repeatability artifact")
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise VerificationError("ranking-repeatability artifact is invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise VerificationError("ranking-repeatability artifact must be a JSON object")
    try:
        expected = canonical_json(value).encode("ascii")
    except (UnicodeEncodeError, ValueError) as exc:
        raise VerificationError("ranking-repeatability artifact is not canonical JSON") from exc
    if raw != expected:
        raise VerificationError("ranking-repeatability artifact is not canonical JSON")
    return value


def _integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise VerificationError(f"{label} must be an integer")
    return value


def _root_list(value: object, label: str) -> list[int]:
    if (
        not isinstance(value, list)
        or len(value) != 21
        or any(type(replica) is not int for replica in value)
        or len(set(value)) != 21
        or any(replica < 0 or replica >= 31 for replica in value)
    ):
        raise VerificationError(f"{label} must contain 21 unique N31 replica IDs")
    return list(value)


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise VerificationError(f"{path} is outside the campaign root") from exc


def _decode_bundle(child: Path, epoch: int) -> dict[str, object]:
    issuer_path = child / "raw" / "issuer-public-key.txt"
    try:
        issuer = _regular_bytes(issuer_path, "issuer public key").decode("ascii").strip()
    except UnicodeError as exc:
        raise VerificationError("issuer public key is not ASCII") from exc
    bundle_path = child / "raw" / f"epoch{epoch}.bundle"
    raw = _regular_bytes(bundle_path, f"epoch {epoch} bundle")
    try:
        decoded = factorial_validation.decode_adaptive_v3_epoch_change_bundle(
            raw, issuer_public_key=issuer
        )
    except factorial_validation.FactorialValidationError as exc:
        raise VerificationError(f"epoch {epoch} bundle is invalid") from exc
    if (
        decoded.epoch_number != epoch
        or tuple(tree.tree_id for tree in decoded.trees) != tuple(range(21))
        or any(
            len(tree.members) != 31 or set(tree.members) != set(range(31))
            for tree in decoded.trees
        )
    ):
        raise VerificationError(f"epoch {epoch} bundle has invalid N31 tree structure")
    roots = [tree.members[0] for tree in decoded.trees]
    _root_list(roots, f"epoch {epoch} roots")
    return {
        "digest": decoded.epoch_digest,
        "previous_digest": decoded.previous_epoch_digest,
        "snapshot_id": decoded.evidence_snapshot_id,
        "cutoff": decoded.evidence_cutoff,
        "roots": roots,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
    }


def _raw_optimization_event(stream_path: Path) -> tuple[Mapping[str, Any], str]:
    if stream_path.is_symlink() or not stream_path.is_file():
        raise VerificationError("raw manager event stream must be a regular file")
    digest = hashlib.sha256()
    matches: list[Mapping[str, Any]] = []
    with stream_path.open("rb") as stream:
        for line_number, line in enumerate(stream, start=1):
            digest.update(line)
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise VerificationError(
                    f"raw manager event stream line {line_number} is invalid JSON"
                ) from exc
            if not isinstance(event, Mapping):
                raise VerificationError(
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
                matches.append(event)
    if len(matches) != 1:
        raise VerificationError(
            "raw manager event stream must contain exactly one E2 optimization snapshot"
        )
    event = matches[0]
    if (
        event.get("event_schema_version") != 1
        or event.get("source_kind") != "adaptation_manager"
        or event.get("source_id") != "adaptive-manager"
        or type(event.get("source_sequence")) is not int
        or int(event["source_sequence"]) <= 0
    ):
        raise VerificationError("raw E2 optimization event envelope is invalid")
    return event, digest.hexdigest()


def _pairwise(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, object]:
    left_roots = _root_list(left.get("raw_eligible_ranking"), "left ranking")
    right_roots = _root_list(right.get("raw_eligible_ranking"), "right ranking")
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
    if denominator == 0:
        raise VerificationError("Kendall comparison has no comparable pair")
    numerator = concordant - discordant
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


def _index_exact_rows(
    value: object, row_ids: Sequence[str], label: str
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, list) or len(value) != len(row_ids):
        raise VerificationError(f"{label} must contain the exact five-pair denominator")
    rows: dict[str, Mapping[str, Any]] = {}
    for row in value:
        if not isinstance(row, Mapping) or not isinstance(row.get("pair_id"), str):
            raise VerificationError(f"{label} has an invalid pair row")
        pair_id = str(row["pair_id"])
        if pair_id in rows:
            raise VerificationError(f"{label} contains duplicate pair {pair_id}")
        rows[pair_id] = row
    if set(rows) != set(row_ids):
        raise VerificationError(f"{label} does not cover the exact five pairs")
    return rows


def _validate_plan(plan: Mapping[str, Any]) -> None:
    slots = plan.get("slots")
    if (
        plan.get("revision") != CAMPAIGN_REVISION
        or plan.get("pair_count") != 5
        or plan.get("automatic_retries") != 0
        or plan.get("replacement_policy") != "none"
        or plan.get("claim_slot_count") != 10
        or not isinstance(slots, list)
        or len(slots) != 10
    ):
        raise VerificationError("campaign plan differs from the frozen denominator")
    adaptive: dict[str, str] = {}
    pair_arms: dict[str, set[str]] = {}
    for slot in slots:
        if not isinstance(slot, Mapping):
            raise VerificationError("campaign plan contains an invalid slot")
        pair_id = slot.get("pair_id")
        arm = slot.get("arm")
        slot_id = slot.get("slot_id")
        if not isinstance(pair_id, str) or not isinstance(arm, str) or not isinstance(slot_id, str):
            raise VerificationError("campaign plan slot identity is invalid")
        pair_arms.setdefault(pair_id, set()).add(arm)
        if arm == "adaptive":
            if pair_id in adaptive:
                raise VerificationError("campaign plan duplicates an adaptive pair")
            adaptive[pair_id] = slot_id
    if adaptive != PAIR_TO_ADAPTIVE_SLOT or pair_arms != {
        pair_id: {"control", "adaptive"} for pair_id in PAIR_IDS
    }:
        raise VerificationError("campaign plan pair-to-arm provenance differs")


def _validate_accepted_inputs(
    placement: Mapping[str, Any], validation: Mapping[str, Any]
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    if (
        placement.get("schema_version") != 1
        or placement.get("audit_id") != "cert13-placement-contrast-v1"
        or placement.get("verdict") != "PASS"
        or placement.get("claim_boundary")
        != "Descriptive accepted-artifact analysis only; C-007 remains rejected and unchanged."
    ):
        raise VerificationError("placement contrast is outside its accepted boundary")
    source = placement.get("source")
    denominator = placement.get("fixed_denominator")
    if (
        not isinstance(source, Mapping)
        or source.get("campaign_id") != CAMPAIGN_ID
        or source.get("campaign_revision") != CAMPAIGN_REVISION
        or source.get("campaign_tree_sha256") != EXPECTED_CAMPAIGN_TREE_SHA256
        or source.get("campaign_seal_sha256") != EXPECTED_CAMPAIGN_SEAL_SHA256
        or source.get("plan_sha256") != EXPECTED_PLAN_SHA256
        or denominator
        != {
            "adaptive_arm_count": 5,
            "consensus_quorum_Q": 21,
            "slot_ids": ["slot-02", "slot-03", "slot-06", "slot-07", "slot-10"],
            "tree_root_position_count": 21,
        }
    ):
        raise VerificationError("placement contrast provenance differs")
    if (
        validation.get("schema_version") != 1
        or validation.get("verdict") != "PASS"
        or validation.get("pair_count") != 5
        or validation.get("automatic_retries") != 0
        or validation.get("replacement_policy") != "none"
        or validation.get("terminal_slot_count") != 10
        or validation.get("figure_eligible") is not True
        or validation.get("claim_eligible") is not False
    ):
        raise VerificationError("campaign validation is outside the accepted C-004 boundary")
    arms = _index_exact_rows(placement.get("arms"), PAIR_IDS, "placement contrast")
    outcomes = _index_exact_rows(validation.get("pairs"), PAIR_IDS, "campaign validation")
    if any(row.get("retained") is not True for row in outcomes.values()):
        raise VerificationError("campaign validation contains a non-retained pair")
    return arms, outcomes


def _checked_invocation(
    report: Mapping[str, Any],
    root: Path,
    validation_path: Path,
    placement_path: Path,
    artifact_path: Path,
) -> Mapping[str, object]:
    invocation = report.get("invocation")
    expected = {
        "argv": [
            GENERATOR_ID,
            "--campaign-root",
            str(root),
            "--campaign-validation",
            str(validation_path),
            "--placement-contrast",
            str(placement_path),
            "--output",
            str(artifact_path),
        ],
        "resolved_campaign_root": str(root),
        "resolved_campaign_validation": str(validation_path),
        "resolved_placement_contrast": str(placement_path),
        "resolved_output": str(artifact_path),
    }
    if invocation != expected:
        raise VerificationError("artifact invocation differs from the exact bound command")
    try:
        artifact_path.relative_to(root)
    except ValueError:
        pass
    else:
        raise VerificationError("ranking-repeatability artifact must be outside the campaign")
    return expected


def _expected_pair(
    root: Path,
    pair_index: int,
    pair_id: str,
    arm: Mapping[str, Any],
    outcome: Mapping[str, Any],
) -> dict[str, object]:
    slot_id = PAIR_TO_ADAPTIVE_SLOT[pair_id]
    if arm.get("pair_id") != pair_id or arm.get("slot_id") != slot_id:
        raise VerificationError(f"{pair_id} W9 adaptive provenance differs")
    child = root / "children" / slot_id
    try:
        child_seal = verify_evidence_seal(child)
    except (EvidenceSealError, OSError) as exc:
        raise VerificationError(f"{slot_id} child seal is invalid") from exc
    arm_source = arm.get("source")
    if (
        not isinstance(arm_source, Mapping)
        or arm_source.get("child_tree_sha256") != child_seal.tree_sha256
        or arm_source.get("child_seal_sha256") != child_seal.seal_sha256
    ):
        raise VerificationError(f"{pair_id} W9 child-seal provenance differs")

    epoch1 = _decode_bundle(child, 1)
    epoch2 = _decode_bundle(child, 2)
    if epoch2["previous_digest"] != epoch1["digest"]:
        raise VerificationError(f"{pair_id} epoch chain is not E1 to E2")
    w9_epoch1 = _root_list(arm.get("epoch1_roots"), f"{pair_id} W9 E1 roots")
    w9_epoch2 = _root_list(arm.get("epoch2_roots"), f"{pair_id} W9 E2 roots")
    if epoch1["roots"] != w9_epoch1 or epoch2["roots"] != w9_epoch2:
        raise VerificationError(f"{pair_id} W9 roots differ from signed bundles")
    if arm_source.get("epoch1_bundle_sha256") != epoch1["raw_sha256"] or arm_source.get(
        "epoch2_bundle_sha256"
    ) != epoch2["raw_sha256"]:
        raise VerificationError(f"{pair_id} W9 bundle hashes differ")

    raw_path = child / "raw" / "adaptive-manager-events.jsonl"
    event, raw_stream_sha256 = _raw_optimization_event(raw_path)
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        raise VerificationError(f"{pair_id} raw E2 payload is absent")
    raw_ranking = _root_list(payload.get("eligible_ranking"), f"{pair_id} raw ranking")
    if (
        payload.get("schema_version") != 2
        or payload.get("predecessor_epoch_number") != 1
        or payload.get("predecessor_epoch_digest") != epoch1["digest"]
        or payload.get("evidence_snapshot_id") != epoch2["snapshot_id"]
        or payload.get("current_cutoff") != epoch2["cutoff"]
        or raw_ranking != w9_epoch2
        or bool({21, 22, 23} & set(raw_ranking))
    ):
        raise VerificationError(f"{pair_id} raw E2 snapshot does not bind W9 and E2")

    materialized_path = (
        child / "transitions" / "e1-to-e2-optimization" / "evidence-snapshot.json"
    )
    materialized_raw = _regular_bytes(
        materialized_path, f"{pair_id} materialized E2 snapshot"
    )
    materialized = _parse_json(materialized_raw, f"{pair_id} materialized E2 snapshot")
    if dict(materialized) != dict(payload):
        raise VerificationError(f"{pair_id} materialized snapshot differs from raw event")
    materialized_sha256 = hashlib.sha256(materialized_raw).hexdigest()
    if arm_source.get("epoch2_snapshot_sha256") != materialized_sha256:
        raise VerificationError(f"{pair_id} W9 snapshot hash differs")

    changed_count = sum(
        left != right for left, right in zip(w9_epoch1, w9_epoch2, strict=True)
    )
    turnover_count = len(set(w9_epoch2) - set(w9_epoch1))
    if (
        arm.get("changed_root_position_count") != changed_count
        or arm.get("root_set_turnover_count") != turnover_count
        or arm.get("epoch2_exact_eligible_ranking_binding") is not True
    ):
        raise VerificationError(f"{pair_id} W9 placement metrics differ")

    adaptive_late = _integer(
        outcome.get("adaptive_late_window_throughput_milli_tps"),
        f"{pair_id} adaptive late throughput",
    )
    control_late = _integer(
        outcome.get("control_late_window_throughput_milli_tps"),
        f"{pair_id} control late throughput",
    )
    direct_effect = _integer(outcome.get("effect_milli_tps"), f"{pair_id} direct effect")
    if direct_effect != adaptive_late - control_late:
        raise VerificationError(f"{pair_id} direct effect is inconsistent")
    paired_ratio = _integer(outcome.get("paired_ratio_ppm"), f"{pair_id} paired ratio")
    scientific_outcome = outcome.get("scientific_outcome")
    if scientific_outcome not in {"FAVORABLE", "UNFAVORABLE"}:
        raise VerificationError(f"{pair_id} scientific outcome is invalid")

    return {
        "pair_index": pair_index,
        "pair_id": pair_id,
        "adaptive_slot_id": slot_id,
        "adaptive_child_path": _relative(child, root),
        "raw_snapshot": {
            "path": _relative(raw_path, root),
            "raw_sha256": raw_stream_sha256,
            "source_sequence": _integer(event.get("source_sequence"), "source sequence"),
            "evidence_snapshot_id": payload.get("evidence_snapshot_id"),
            "baseline_cutoff": _integer(payload.get("baseline_cutoff"), "baseline cutoff"),
            "current_cutoff": _integer(payload.get("current_cutoff"), "current cutoff"),
        },
        "materialized_snapshot": {
            "path": _relative(materialized_path, root),
            "raw_sha256": materialized_sha256,
        },
        "epoch1_bundle": {
            "path": _relative(child / "raw" / "epoch1.bundle", root),
            "raw_sha256": epoch1["raw_sha256"],
        },
        "epoch2_bundle": {
            "path": _relative(child / "raw" / "epoch2.bundle", root),
            "raw_sha256": epoch2["raw_sha256"],
        },
        "w9_epoch1_roots": w9_epoch1,
        "w9_epoch2_roots": w9_epoch2,
        "raw_eligible_ranking": raw_ranking,
        "ranking_matches_w9_epoch2_roots": True,
        "w9_changed_root_position_count": changed_count,
        "w9_root_set_turnover_count": turnover_count,
        "throughput": {
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
            "paired_ratio_ppm": paired_ratio,
            "adjusted_effect_ppm": paired_ratio - 1_000_000,
            "scientific_outcome": scientific_outcome,
        },
    }


def _reconstruct_expected(
    campaign_root: Path,
    campaign_validation_path: Path,
    placement_contrast_path: Path,
    artifact_path: Path,
    report: Mapping[str, Any],
    *,
    expected_campaign_validation_sha256: str,
    expected_placement_contrast_sha256: str,
) -> dict[str, object]:
    root = campaign_root.resolve(strict=True)
    validation_path = campaign_validation_path.resolve(strict=True)
    placement_path = placement_contrast_path.resolve(strict=True)
    resolved_artifact = artifact_path.resolve(strict=True)
    if root.name != CAMPAIGN_ID:
        raise VerificationError("campaign root is not the frozen CERT13 campaign")
    if expected_campaign_validation_sha256 != EXPECTED_CAMPAIGN_VALIDATION_RAW_SHA256:
        raise VerificationError("expected campaign-validation SHA-256 differs from frozen input")
    if expected_placement_contrast_sha256 != EXPECTED_PLACEMENT_CONTRAST_SHA256:
        raise VerificationError("expected placement-contrast SHA-256 differs from frozen input")
    try:
        seal = verify_evidence_seal(root)
    except (EvidenceSealError, OSError) as exc:
        raise VerificationError("campaign evidence seal is invalid") from exc
    if (
        seal.tree_sha256 != EXPECTED_CAMPAIGN_TREE_SHA256
        or seal.seal_sha256 != EXPECTED_CAMPAIGN_SEAL_SHA256
    ):
        raise VerificationError("campaign seal differs from accepted CERT13")
    analysis_revision, generator_source_sha256 = _committed_source_provenance(
        ROOT / GENERATOR_ID, "ranking-repeatability producer"
    )

    plan_path = root / "plan.json"
    plan, _plan_sha256 = _read_pinned_json(
        plan_path, EXPECTED_PLAN_SHA256, "campaign plan"
    )
    _validate_plan(plan)
    placement, placement_raw_sha256 = _read_pinned_json(
        placement_path, expected_placement_contrast_sha256, "placement contrast"
    )
    validation, validation_raw_sha256 = _read_pinned_json(
        validation_path, expected_campaign_validation_sha256, "campaign validation"
    )
    validation_canonical_sha256 = hashlib.sha256(
        canonical_json(validation).encode("ascii")
    ).hexdigest()
    if validation_canonical_sha256 != EXPECTED_CAMPAIGN_VALIDATION_CANONICAL_SHA256:
        raise VerificationError("campaign validation semantics differ from accepted input")
    arms, outcomes = _validate_accepted_inputs(placement, validation)
    invocation = _checked_invocation(
        report, root, validation_path, placement_path, resolved_artifact
    )

    pairs = [
        _expected_pair(root, index, pair_id, arms[pair_id], outcomes[pair_id])
        for index, pair_id in enumerate(PAIR_IDS, start=1)
    ]
    comparisons = [
        _pairwise(pairs[left], pairs[right])
        for left in range(len(pairs))
        for right in range(left + 1, len(pairs))
    ]
    rankings = [set(_root_list(row["raw_eligible_ranking"], "ranking")) for row in pairs]
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
        for row in pairs
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
    return {
        "schema_version": 1,
        "domain": DOMAIN,
        "verdict": "PASS",
        "analysis_id": ANALYSIS_ID,
        "generator_id": GENERATOR_ID,
        "invocation": invocation,
        "source": {
            "analysis_revision": analysis_revision,
            "generator_source_sha256": generator_source_sha256,
            "campaign_id": CAMPAIGN_ID,
            "campaign_revision": CAMPAIGN_REVISION,
            "campaign_root": str(root),
            "campaign_tree_sha256": seal.tree_sha256,
            "campaign_seal_sha256": seal.seal_sha256,
            "plan": {"path": "plan.json", "raw_sha256": EXPECTED_PLAN_SHA256},
            "placement_contrast": {
                "path": str(placement_path),
                "raw_sha256": placement_raw_sha256,
            },
            "campaign_validation": {
                "path": str(validation_path),
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
        "pairs": pairs,
        "pairwise_comparisons": comparisons,
        "aggregate": aggregate,
        "claim_boundary": CLAIM_BOUNDARY,
        "limitations": LIMITATIONS,
    }


def verify(
    artifact_path: Path,
    campaign_validation_path: Path,
    placement_contrast_path: Path,
    campaign_root: Path,
    *,
    expected_campaign_validation_sha256: str = EXPECTED_CAMPAIGN_VALIDATION_RAW_SHA256,
    expected_placement_contrast_sha256: str = EXPECTED_PLACEMENT_CONTRAST_SHA256,
) -> Mapping[str, object]:
    """Reconstruct the report from accepted source bytes and compare every field."""
    artifact = Path(artifact_path).resolve(strict=True)
    report = _read_canonical_artifact(artifact)
    expected = _reconstruct_expected(
        campaign_root,
        campaign_validation_path,
        placement_contrast_path,
        artifact,
        report,
        expected_campaign_validation_sha256=expected_campaign_validation_sha256,
        expected_placement_contrast_sha256=expected_placement_contrast_sha256,
    )
    if report != expected:
        raise VerificationError(
            "ranking-repeatability artifact differs from independent exact reconstruction"
        )
    return {
        "schema_version": 1,
        "verdict": "PASS",
        "analysis_id": ANALYSIS_ID,
        "pair_count": 5,
        "pairwise_comparison_count": 10,
        "claim_eligible": False,
    }


def write_receipt(
    artifact_path: Path,
    campaign_validation_path: Path,
    placement_contrast_path: Path,
    campaign_root: Path,
    output_path: Path,
    *,
    expected_campaign_validation_sha256: str = EXPECTED_CAMPAIGN_VALIDATION_RAW_SHA256,
    expected_placement_contrast_sha256: str = EXPECTED_PLACEMENT_CONTRAST_SHA256,
) -> Mapping[str, object]:
    root = Path(campaign_root).resolve(strict=True)
    artifact = Path(artifact_path).resolve(strict=True)
    output = Path(output_path).resolve(strict=False)
    try:
        output.relative_to(root)
    except ValueError:
        pass
    else:
        raise VerificationError("verifier receipt must be outside the sealed campaign")
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise VerificationError("verifier receipt must be a new file in an existing directory")
    artifact_sha256 = _raw_sha256(artifact, "ranking-repeatability artifact")
    verdict = dict(
        verify(
            artifact,
            campaign_validation_path,
            placement_contrast_path,
            root,
            expected_campaign_validation_sha256=expected_campaign_validation_sha256,
            expected_placement_contrast_sha256=expected_placement_contrast_sha256,
        )
    )
    if _raw_sha256(artifact, "ranking-repeatability artifact") != artifact_sha256:
        raise VerificationError("ranking-repeatability artifact changed during verification")
    verifier_revision, verifier_source_sha256 = _committed_source_provenance(
        Path(__file__), "ranking-repeatability verifier"
    )
    receipt = {
        "schema_version": 1,
        "verifier_id": VERIFIER_ID,
        "analysis_revision": verifier_revision,
        "verifier_source_sha256": verifier_source_sha256,
        "artifact_sha256": artifact_sha256,
        "campaign_tree_sha256": EXPECTED_CAMPAIGN_TREE_SHA256,
        "campaign_seal_sha256": EXPECTED_CAMPAIGN_SEAL_SHA256,
        "campaign_validation_raw_sha256": EXPECTED_CAMPAIGN_VALIDATION_RAW_SHA256,
        "placement_contrast_raw_sha256": EXPECTED_PLACEMENT_CONTRAST_SHA256,
        "verdict": verdict,
    }
    output.write_text(canonical_json(receipt), encoding="ascii", newline="")
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--campaign-validation", required=True, type=Path)
    parser.add_argument("--placement-contrast", required=True, type=Path)
    parser.add_argument("--campaign-root", required=True, type=Path)
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
    try:
        result = write_receipt(
            args.artifact,
            args.campaign_validation,
            args.placement_contrast,
            args.campaign_root,
            args.output,
            expected_campaign_validation_sha256=args.expected_campaign_validation_sha256,
            expected_placement_contrast_sha256=args.expected_placement_contrast_sha256,
        )
    except (OSError, VerificationError) as exc:
        print(f"ranking-repeatability verification rejected: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
