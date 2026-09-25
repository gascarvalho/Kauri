#!/usr/bin/env python3
"""Independently verify a CERT13 placement contrast report from source bytes."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.adaptive.kauri_experiment import factorial_validation  # noqa: E402
from experiments.adaptive.kauri_experiment.profiled_fault_archive import (  # noqa: E402
    EvidenceSealError,
    verify_evidence_seal,
)

CAMPAIGN_ID = "cert13-n31-campaign-v13-7adabc83-r50"
CAMPAIGN_REVISION = "7adabc838129082486e33d830d1d09cd0f2919e5"
ADAPTIVE_SLOTS = ("slot-02", "slot-03", "slot-06", "slot-07", "slot-10")
EXPECTED_CAMPAIGN_TREE_SHA256 = "a150b65d2938f8912917734dfd575c713519e378191470474604d71fc7196423"
EXPECTED_CAMPAIGN_SEAL_SHA256 = "b14c991a5f964434e85ec306c78ed4b0fb02ea96ec0fad28726c5a557b7bd470"
EXPECTED_CAMPAIGN_VALIDATION_SHA256 = "6d245dda770c32ad8fb427289003dc478fd765e461ec8aa94d4b4f904034aa18"
EXPECTED_ACCEPTED_SOURCE_MANIFEST_SHA256 = "4c7dbfd96f943014aa05239621a862210377c27a019bbec5d4640bb532d3da4c"
EXPECTED_TRUSTED_PROVENANCE_SHA256 = "d2051421e2b94f63b52d556198049c466d0b60786d6868ac281f544260a3e449"
PROVENANCE_DIRECTORY = ".cert13-n31-campaign-v13-7adabc83-r50-ba797cca2eca5d82-preflight"
AUDIT_ID = "cert13-placement-contrast-v1"
AUDIT_COMMAND = "experiments/adaptive/audit_cert13_placement_contrast.py"
CLAIM_BOUNDARY = "Descriptive accepted-artifact analysis only; C-007 remains rejected and unchanged."
LIMITATIONS = [
    "Root placement is descriptive and is not a throughput estimator.",
    "Accepted artifacts do not identify a responsive-survivor bottleneck.",
]


class VerificationError(ValueError):
    pass


def _read(path: Path, label: str) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise VerificationError(f"{label} must be a regular file")
    try:
        value = json.loads(path.read_bytes())
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise VerificationError(f"{label} is invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise VerificationError(f"{label} must be an object")
    return value


def _read_canonical_report(path: Path) -> Mapping[str, Any]:
    """Read the generator's canonical report encoding, rejecting ambiguous JSON."""
    if path.is_symlink() or not path.is_file():
        raise VerificationError("audit report must be a regular file")
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise VerificationError("audit report is invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise VerificationError("audit report must be an object")
    try:
        canonical = json.dumps(value, allow_nan=False, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    except ValueError as exc:
        raise VerificationError("audit report is not canonical JSON") from exc
    if raw != canonical.encode("ascii"):
        raise VerificationError("audit report is not canonical JSON")
    return value


def _hash(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise VerificationError(f"{path.name} must be a regular file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _accepted_validation_context(root: Path) -> Mapping[str, object]:
    directory = root.parent / PROVENANCE_DIRECTORY
    trusted = directory / "trusted-provenance-campaign.json"
    validation = directory / "campaign-validation.json"
    manifest = root.parents[2] / "thesis" / "Images" / "evaluation-cert13-n31-7adabc83" / "accepted-source-manifest.json"
    if _hash(manifest) != EXPECTED_ACCEPTED_SOURCE_MANIFEST_SHA256:
        raise VerificationError("accepted source manifest differs from pinned bytes")
    manifest_data = _read(manifest, "accepted source manifest")
    if (
        manifest_data.get("evidence_revision") != CAMPAIGN_REVISION
        or manifest_data.get("campaign_tree_sha256") != EXPECTED_CAMPAIGN_TREE_SHA256
        or manifest_data.get("campaign_seal_sha256") != EXPECTED_CAMPAIGN_SEAL_SHA256
        or manifest_data.get("trusted_provenance_sha256") != EXPECTED_TRUSTED_PROVENANCE_SHA256
        or manifest_data.get("campaign_validation_sha256") != EXPECTED_CAMPAIGN_VALIDATION_SHA256
        or manifest_data.get("claim_eligible") is not False
    ):
        raise VerificationError("accepted source manifest identity differs")
    if _hash(trusted) != EXPECTED_TRUSTED_PROVENANCE_SHA256:
        raise VerificationError("trusted provenance differs from accepted manifest")
    validation_data = _read(validation, "campaign validation")
    canonical = json.dumps(validation_data, allow_nan=False, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    if hashlib.sha256(canonical.encode("ascii")).hexdigest() != EXPECTED_CAMPAIGN_VALIDATION_SHA256:
        raise VerificationError("campaign validation differs from accepted manifest")
    if (
        validation_data.get("verdict") != "PASS"
        or validation_data.get("claim_eligible") is not False
        or validation_data.get("figure_eligible") is not True
    ):
        raise VerificationError("campaign validation verdict is not the accepted boundary")
    return {
        "state": "verified_external_context",
        "accepted_source_manifest_sha256": EXPECTED_ACCEPTED_SOURCE_MANIFEST_SHA256,
        "trusted_provenance_sha256": EXPECTED_TRUSTED_PROVENANCE_SHA256,
        "campaign_validation_sha256": EXPECTED_CAMPAIGN_VALIDATION_SHA256,
        "validator_verdict": "PASS",
        "claim_eligible": False,
        "figure_eligible": True,
    }


def _roots(child: Path, epoch: int) -> tuple[str, str, str, int, tuple[int, ...]]:
    issuer_path = child / "raw" / "issuer-public-key.txt"
    if issuer_path.is_symlink() or not issuer_path.is_file():
        raise VerificationError("issuer key is absent")
    issuer = issuer_path.read_text(encoding="ascii").strip()
    wire_path = child / "raw" / f"epoch{epoch}.bundle"
    if wire_path.is_symlink() or not wire_path.is_file():
        raise VerificationError(f"epoch {epoch} bundle is absent")
    try:
        bundle = factorial_validation.decode_adaptive_v3_epoch_change_bundle(wire_path.read_bytes(), issuer_public_key=issuer)
    except factorial_validation.FactorialValidationError as exc:
        raise VerificationError(f"epoch {epoch} bundle is invalid") from exc
    if bundle.epoch_number != epoch or tuple(tree.tree_id for tree in bundle.trees) != tuple(range(21)):
        raise VerificationError(f"epoch {epoch} tree identity is invalid")
    if any(len(tree.members) != 31 or set(tree.members) != set(range(31)) for tree in bundle.trees):
        raise VerificationError(f"epoch {epoch} tree membership is not the fixed N31 set")
    return (
        bundle.epoch_digest,
        bundle.previous_epoch_digest,
        bundle.evidence_snapshot_id,
        bundle.evidence_cutoff,
        tuple(tree.members[0] for tree in bundle.trees),
    )


def _expected_arm(root: Path, slot: Mapping[str, Any], plan: Mapping[str, Any]) -> Mapping[str, object]:
    slot_id = str(slot["slot_id"])
    child = root / "children" / slot_id
    try:
        seal = verify_evidence_seal(child)
    except (EvidenceSealError, OSError) as exc:
        raise VerificationError(f"{slot_id} child seal is invalid") from exc
    manifest = _read(child / "manifest.json", "manifest")
    outcome = _read(child / "runner-outcome.json", "runner outcome")
    if (
        manifest.get("slot_id") != slot_id
        or manifest.get("pair_id") != slot.get("pair_id")
        or manifest.get("profile_sha256") != plan.get("profile_sha256")
        or manifest.get("build_sha256") != plan.get("build_sha256")
        or outcome.get("arm") != "A"
        or outcome.get("quorum") != 21
        or outcome.get("epoch2_present") is not True
    ):
        raise VerificationError(f"{slot_id} is not the planned adaptive child")
    digest1, _previous1, _snapshot1, _cutoff1, roots1 = _roots(child, 1)
    _digest2, previous2, snapshot_id2, cutoff2, roots2 = _roots(child, 2)
    if previous2 != digest1:
        raise VerificationError(f"{slot_id} epoch chain is not E1 to E2")
    snapshot_path = child / "transitions" / "e1-to-e2-optimization" / "evidence-snapshot.json"
    snapshot = _read(snapshot_path, "E2 snapshot")
    ranking = snapshot.get("eligible_ranking")
    if (
        snapshot.get("schema_version") != 2
        or snapshot.get("policy_intent") != "performance_optimization"
        or snapshot.get("transition_artifact_id") != "e1-to-e2-optimization"
        or snapshot.get("predecessor_epoch_number") != 1
        or snapshot.get("predecessor_epoch_digest") != digest1
        or snapshot.get("evidence_snapshot_id") != snapshot_id2
        or snapshot.get("current_cutoff") != cutoff2
        or not isinstance(ranking, list)
        or tuple(ranking) != roots2
    ):
        raise VerificationError(f"{slot_id} ranking does not bind E2 roots")
    if len(set(roots2)) != 21:
        raise VerificationError(f"{slot_id} E2 roots are not distinct")
    if {21, 22, 23} & set(roots2):
        raise VerificationError(f"{slot_id} has crashed root")
    changed = [index for index, values in enumerate(zip(roots1, roots2, strict=True)) if values[0] != values[1]]
    return {
        "slot_id": slot_id,
        "pair_id": slot["pair_id"],
        "source": {
            "child_tree_sha256": seal.tree_sha256,
            "child_seal_sha256": seal.seal_sha256,
            "epoch1_bundle_sha256": _hash(child / "raw" / "epoch1.bundle"),
            "epoch2_bundle_sha256": _hash(child / "raw" / "epoch2.bundle"),
            "epoch2_snapshot_sha256": _hash(snapshot_path),
        },
        "epoch1_roots": list(roots1),
        "epoch2_roots": list(roots2),
        "changed_root_positions": changed,
        "changed_root_position_count": len(changed),
        "retained_root_ids": sorted(set(roots1) & set(roots2)),
        "entered_root_ids": sorted(set(roots2) - set(roots1)),
        "left_root_ids": sorted(set(roots1) - set(roots2)),
        "root_set_turnover_count": len(set(roots2) - set(roots1)),
        "epoch2_exact_eligible_ranking_binding": True,
        "score_margin": {"state": "unknown", "reason": "accepted snapshot records only eligible ordering"},
        "limitations": LIMITATIONS,
    }


def _checked_invocation(report: Mapping[str, Any], root: Path, report_path: Path) -> Mapping[str, object]:
    invocation = report.get("invocation")
    if not isinstance(invocation, Mapping):
        raise VerificationError("report invocation is absent")
    if set(invocation) != {"argv", "resolved_campaign_root", "resolved_output_root"}:
        raise VerificationError("report invocation has an unexpected schema")
    argv = invocation.get("argv")
    if not isinstance(argv, list) or any(not isinstance(value, str) for value in argv):
        raise VerificationError("report invocation argv is invalid")
    if (
        len(argv) != 5
        or argv[0] != AUDIT_COMMAND
        or argv[1] != "--campaign-root"
        or argv[3] != "--output-root"
    ):
        raise VerificationError("report invocation argv is not the exact audit command")
    if not argv[2] or not argv[4]:
        raise VerificationError("report invocation paths are empty")

    resolved_root = root.resolve()
    resolved_report_parent = report_path.parent.resolve(strict=True)
    if invocation.get("resolved_campaign_root") != str(resolved_root):
        raise VerificationError("report invocation campaign path differs")
    output_value = invocation.get("resolved_output_root")
    if not isinstance(output_value, str):
        raise VerificationError("report invocation output path is invalid")
    if output_value != str(resolved_report_parent):
        raise VerificationError("report invocation output path differs from report parent")

    argv_root = Path(argv[2]).resolve(strict=False)
    argv_output = Path(argv[4]).resolve(strict=False)
    if argv_root != resolved_root:
        raise VerificationError("report invocation --campaign-root differs")
    if argv_output != resolved_report_parent:
        raise VerificationError("report invocation --output-root differs")
    if argv_output.is_relative_to(resolved_root):
        raise VerificationError("report invocation output is inside sealed campaign")
    return dict(invocation)


def verify(campaign_root: Path, report_path: Path) -> Mapping[str, object]:
    root = Path(campaign_root).resolve(strict=True)
    if root.name != CAMPAIGN_ID:
        raise VerificationError("wrong campaign root")
    try:
        seal = verify_evidence_seal(root)
    except (EvidenceSealError, OSError) as exc:
        raise VerificationError("campaign seal is invalid") from exc
    if (
        seal.tree_sha256 != EXPECTED_CAMPAIGN_TREE_SHA256
        or seal.seal_sha256 != EXPECTED_CAMPAIGN_SEAL_SHA256
    ):
        raise VerificationError("campaign seal differs from accepted evidence")
    plan = _read(root / "plan.json", "plan")
    validation_context = _accepted_validation_context(root)
    slots = plan.get("slots")
    if plan.get("revision") != CAMPAIGN_REVISION or plan.get("pair_count") != 5 or not isinstance(slots, list) or len(slots) != 10:
        raise VerificationError("campaign identity is invalid")
    adaptive = [slot for slot in slots if isinstance(slot, Mapping) and slot.get("arm") == "adaptive"]
    if tuple(slot.get("slot_id") for slot in adaptive) != ADAPTIVE_SLOTS or len({slot.get("pair_id") for slot in adaptive}) != 5:
        raise VerificationError("fixed denominator differs")
    report = _read_canonical_report(report_path)
    invocation = _checked_invocation(report, root, report_path)
    expected = [_expected_arm(root, slot, plan) for slot in adaptive]
    changed = [int(arm["changed_root_position_count"]) for arm in expected]
    turnover = [int(arm["root_set_turnover_count"]) for arm in expected]
    expected_report = {
        "schema_version": 1,
        "audit_id": AUDIT_ID,
        "verdict": "PASS",
        "generator_id": AUDIT_COMMAND,
        "invocation": invocation,
        "source": {
            "campaign_id": CAMPAIGN_ID,
            "campaign_revision": CAMPAIGN_REVISION,
            "campaign_tree_sha256": seal.tree_sha256,
            "campaign_seal_sha256": seal.seal_sha256,
            "plan_sha256": _hash(root / "plan.json"),
        },
        "accepted_validation_context": validation_context,
        "fixed_denominator": {
            "adaptive_arm_count": 5,
            "slot_ids": list(ADAPTIVE_SLOTS),
            "tree_root_position_count": 21,
            "consensus_quorum_Q": 21,
        },
        "arms": expected,
        "summary": {
            "changed_root_position_counts": changed,
            "root_set_turnover_counts": turnover,
            "changed_root_position_range": [min(changed), max(changed)],
            "root_set_turnover_range": [min(turnover), max(turnover)],
            "all_epoch2_exact_eligible_ranking_binding": True,
            "score_margin_state": "unknown",
        },
        "claim_boundary": CLAIM_BOUNDARY,
    }
    if report != expected_report:
        raise VerificationError("report differs from the independent exact reconstruction")
    return {"schema_version": 1, "verdict": "PASS", "audited_arm_count": 5, "slot_ids": list(ADAPTIVE_SLOTS)}


def write_receipt(campaign_root: Path, report_path: Path, output_path: Path) -> Mapping[str, object]:
    """Verify and persist a separate, non-overwriting verifier receipt."""
    source = Path(campaign_root).resolve(strict=True)
    output = Path(output_path).resolve(strict=False)
    try:
        output.relative_to(source)
    except ValueError:
        pass
    else:
        raise VerificationError("verifier receipt must be outside the sealed campaign")
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise VerificationError("verifier receipt path must be a new file in an existing directory")
    verdict = dict(verify(campaign_root, report_path))
    receipt = {
        "schema_version": 1,
        "verifier_id": "cert13-placement-contrast-independent-v1",
        "report_sha256": _hash(report_path),
        "campaign_tree_sha256": EXPECTED_CAMPAIGN_TREE_SHA256,
        "campaign_seal_sha256": EXPECTED_CAMPAIGN_SEAL_SHA256,
        "verdict": verdict,
    }
    output.write_text(json.dumps(receipt, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="ascii")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--output", type=Path, help="new verifier receipt file in the report directory")
    args = parser.parse_args()
    try:
        result = (
            write_receipt(args.campaign_root, args.report, args.output)
            if args.output is not None
            else verify(args.campaign_root, args.report)
        )
        print(json.dumps(result, sort_keys=True))
    except VerificationError as exc:
        print(f"verification rejected: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
