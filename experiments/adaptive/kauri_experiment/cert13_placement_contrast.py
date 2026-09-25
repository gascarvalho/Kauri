"""Read-only placement contrast audit for the accepted CERT13 campaign."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from . import factorial_validation
from .profiled_fault_archive import EvidenceSealError, verify_evidence_seal


CAMPAIGN_ID = "cert13-n31-campaign-v13-7adabc83-r50"
CAMPAIGN_REVISION = "7adabc838129082486e33d830d1d09cd0f2919e5"
ADAPTIVE_SLOTS = ("slot-02", "slot-03", "slot-06", "slot-07", "slot-10")
REPORT_NAME = "placement-contrast.json"
ABORT_NAME = "placement-contrast-abort.json"
AUDIT_COMMAND = "experiments/adaptive/audit_cert13_placement_contrast.py"
EXPECTED_CAMPAIGN_TREE_SHA256 = "a150b65d2938f8912917734dfd575c713519e378191470474604d71fc7196423"
EXPECTED_CAMPAIGN_SEAL_SHA256 = "b14c991a5f964434e85ec306c78ed4b0fb02ea96ec0fad28726c5a557b7bd470"
EXPECTED_CAMPAIGN_VALIDATION_SHA256 = "6d245dda770c32ad8fb427289003dc478fd765e461ec8aa94d4b4f904034aa18"
EXPECTED_ACCEPTED_SOURCE_MANIFEST_SHA256 = "4c7dbfd96f943014aa05239621a862210377c27a019bbec5d4640bb532d3da4c"
EXPECTED_TRUSTED_PROVENANCE_SHA256 = "d2051421e2b94f63b52d556198049c466d0b60786d6868ac281f544260a3e449"
PROVENANCE_DIRECTORY = ".cert13-n31-campaign-v13-7adabc83-r50-ba797cca2eca5d82-preflight"


class PlacementContrastError(ValueError):
    """The sealed campaign cannot support the bounded placement audit."""


class PlacementContrastAbort(PlacementContrastError):
    """A fresh, fail-closed abort artifact was written for an invalid source."""

    def __init__(self, path: Path, reason: str):
        super().__init__(reason)
        self.path = path


def canonical_json(value: object) -> str:
    return json.dumps(value, allow_nan=False, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PlacementContrastError(f"{label} must be a regular file")
    try:
        value = json.loads(path.read_bytes())
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise PlacementContrastError(f"{label} is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise PlacementContrastError(f"{label} must be an object")
    return value


def _sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise PlacementContrastError(f"{path.name} must be a regular file")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _accepted_validation_context(root: Path) -> dict[str, object]:
    """Load the pinned external validation provenance, fail-closed on mismatch."""
    directory = root.parent / PROVENANCE_DIRECTORY
    trusted = directory / "trusted-provenance-campaign.json"
    validation = directory / "campaign-validation.json"
    manifest = root.parents[2] / "thesis" / "Images" / "evaluation-cert13-n31-7adabc83" / "accepted-source-manifest.json"
    if _sha256_file(manifest) != EXPECTED_ACCEPTED_SOURCE_MANIFEST_SHA256:
        raise PlacementContrastError("accepted source manifest differs from pinned bytes")
    manifest_data = _read_json(manifest, "accepted source manifest")
    if (
        manifest_data.get("evidence_revision") != CAMPAIGN_REVISION
        or manifest_data.get("campaign_tree_sha256") != EXPECTED_CAMPAIGN_TREE_SHA256
        or manifest_data.get("campaign_seal_sha256") != EXPECTED_CAMPAIGN_SEAL_SHA256
        or manifest_data.get("trusted_provenance_sha256") != EXPECTED_TRUSTED_PROVENANCE_SHA256
        or manifest_data.get("campaign_validation_sha256") != EXPECTED_CAMPAIGN_VALIDATION_SHA256
        or manifest_data.get("claim_eligible") is not False
    ):
        raise PlacementContrastError("accepted source manifest identity differs")
    if _sha256_file(trusted) != EXPECTED_TRUSTED_PROVENANCE_SHA256:
        raise PlacementContrastError("trusted provenance differs from accepted manifest")
    validation_data = _read_json(validation, "campaign validation")
    if hashlib.sha256(canonical_json(validation_data).encode("ascii")).hexdigest() != EXPECTED_CAMPAIGN_VALIDATION_SHA256:
        raise PlacementContrastError("campaign validation differs from accepted manifest")
    if (
        validation_data.get("verdict") != "PASS"
        or validation_data.get("claim_eligible") is not False
        or validation_data.get("figure_eligible") is not True
    ):
        raise PlacementContrastError("campaign validation verdict is not the accepted boundary")
    return {
        "state": "verified_external_context",
        "accepted_source_manifest_sha256": EXPECTED_ACCEPTED_SOURCE_MANIFEST_SHA256,
        "trusted_provenance_sha256": EXPECTED_TRUSTED_PROVENANCE_SHA256,
        "campaign_validation_sha256": EXPECTED_CAMPAIGN_VALIDATION_SHA256,
        "validator_verdict": "PASS",
        "claim_eligible": False,
        "figure_eligible": True,
    }


def _issuer(child: Path) -> str:
    path = child / "raw" / "issuer-public-key.txt"
    if path.is_symlink() or not path.is_file():
        raise PlacementContrastError("issuer public key is absent")
    raw = path.read_bytes()
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise PlacementContrastError("issuer public key encoding is malformed")
    value = raw[:-1].decode("ascii")
    if len(value) not in {66, 130} or any(char not in "0123456789abcdef" for char in value):
        raise PlacementContrastError("issuer public key encoding is malformed")
    return value


def _decode(child: Path, issuer: str, epoch: int) -> tuple[bytes, Any]:
    path = child / "raw" / f"epoch{epoch}.bundle"
    if path.is_symlink() or not path.is_file():
        raise PlacementContrastError(f"epoch {epoch} bundle is absent")
    wire = path.read_bytes()
    try:
        decoded = factorial_validation.decode_adaptive_v3_epoch_change_bundle(
            wire, issuer_public_key=issuer
        )
    except factorial_validation.FactorialValidationError as exc:
        raise PlacementContrastError(f"epoch {epoch} bundle is invalid") from exc
    if decoded.epoch_number != epoch or len(decoded.trees) != 21:
        raise PlacementContrastError(f"epoch {epoch} bundle identity is invalid")
    if tuple(tree.tree_id for tree in decoded.trees) != tuple(range(21)):
        raise PlacementContrastError(f"epoch {epoch} tree order is invalid")
    expected_members = set(range(31))
    if any(len(tree.members) != 31 or set(tree.members) != expected_members for tree in decoded.trees):
        raise PlacementContrastError(f"epoch {epoch} tree membership is not the fixed N31 set")
    return wire, decoded


def _adaptive_slots(plan: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    if plan.get("revision") != CAMPAIGN_REVISION or plan.get("pair_count") != 5:
        raise PlacementContrastError("campaign identity differs from CERT13")
    slots = plan.get("slots")
    if not isinstance(slots, list) or len(slots) != 10:
        raise PlacementContrastError("campaign plan lacks ten fixed slots")
    adaptive = tuple(slot for slot in slots if isinstance(slot, Mapping) and slot.get("arm") == "adaptive")
    if tuple(slot.get("slot_id") for slot in adaptive) != ADAPTIVE_SLOTS:
        raise PlacementContrastError("campaign adaptive slots differ from the fixed denominator")
    if len({slot.get("pair_id") for slot in adaptive}) != 5:
        raise PlacementContrastError("campaign adaptive arms do not cover five pairs")
    return adaptive


def _arm_record(root: Path, slot: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, object]:
    slot_id = str(slot["slot_id"])
    child = root / "children" / slot_id
    try:
        child_seal = verify_evidence_seal(child)
    except (EvidenceSealError, OSError) as exc:
        raise PlacementContrastError(f"{slot_id} child seal is invalid") from exc
    manifest = _read_json(child / "manifest.json", f"{slot_id} manifest")
    outcome = _read_json(child / "runner-outcome.json", f"{slot_id} outcome")
    if manifest.get("slot_id") != slot_id or manifest.get("pair_id") != slot.get("pair_id"):
        raise PlacementContrastError(f"{slot_id} manifest identity differs from plan")
    if (
        manifest.get("profile_sha256") != plan.get("profile_sha256")
        or manifest.get("build_sha256") != plan.get("build_sha256")
    ):
        raise PlacementContrastError(f"{slot_id} manifest source binding differs from plan")
    if outcome.get("arm") != "A" or outcome.get("quorum") != 21 or outcome.get("epoch2_present") is not True:
        raise PlacementContrastError(f"{slot_id} is not an accepted adaptive arm")
    issuer = _issuer(child)
    wire1, epoch1 = _decode(child, issuer, 1)
    wire2, epoch2 = _decode(child, issuer, 2)
    if epoch2.previous_epoch_digest != epoch1.epoch_digest:
        raise PlacementContrastError(f"{slot_id} epoch chain is not E1 to E2")
    roots1 = tuple(tree.members[0] for tree in epoch1.trees)
    roots2 = tuple(tree.members[0] for tree in epoch2.trees)
    snapshot = _read_json(
        child / "transitions" / "e1-to-e2-optimization" / "evidence-snapshot.json",
        f"{slot_id} E2 snapshot",
    )
    ranking = snapshot.get("eligible_ranking")
    if (
        snapshot.get("schema_version") != 2
        or snapshot.get("policy_intent") != "performance_optimization"
        or snapshot.get("transition_artifact_id") != "e1-to-e2-optimization"
        or snapshot.get("predecessor_epoch_number") != 1
        or snapshot.get("predecessor_epoch_digest") != epoch1.epoch_digest
        or snapshot.get("evidence_snapshot_id") != epoch2.evidence_snapshot_id
        or snapshot.get("current_cutoff") != epoch2.evidence_cutoff
        or not isinstance(ranking, list)
        or len(ranking) != 21
        or any(type(replica) is not int for replica in ranking)
        or len(set(ranking)) != 21
    ):
        raise PlacementContrastError(f"{slot_id} E2 ranking snapshot is invalid")
    ranked_roots = tuple(ranking)
    if roots2 != ranked_roots:
        raise PlacementContrastError(f"{slot_id} E2 roots do not exactly bind the frozen ranking")
    if len(set(roots2)) != 21:
        raise PlacementContrastError(f"{slot_id} E2 roots are not distinct")
    crashed = {21, 22, 23}
    if crashed & set(roots2):
        raise PlacementContrastError(f"{slot_id} places a crashed replica at root")
    changed = [index for index, (left, right) in enumerate(zip(roots1, roots2, strict=True)) if left != right]
    retained = tuple(sorted(set(roots1) & set(roots2)))
    entered = tuple(sorted(set(roots2) - set(roots1)))
    left = tuple(sorted(set(roots1) - set(roots2)))
    return {
        "slot_id": slot_id,
        "pair_id": slot["pair_id"],
        "source": {
            "child_tree_sha256": child_seal.tree_sha256,
            "child_seal_sha256": child_seal.seal_sha256,
            "epoch1_bundle_sha256": _sha256_file(child / "raw" / "epoch1.bundle"),
            "epoch2_bundle_sha256": _sha256_file(child / "raw" / "epoch2.bundle"),
            "epoch2_snapshot_sha256": _sha256_file(child / "transitions" / "e1-to-e2-optimization" / "evidence-snapshot.json"),
        },
        "epoch1_roots": list(roots1),
        "epoch2_roots": list(roots2),
        "changed_root_positions": changed,
        "changed_root_position_count": len(changed),
        "retained_root_ids": list(retained),
        "entered_root_ids": list(entered),
        "left_root_ids": list(left),
        "root_set_turnover_count": len(entered),
        "epoch2_exact_eligible_ranking_binding": True,
        "score_margin": {"state": "unknown", "reason": "accepted snapshot records only eligible ordering"},
        "limitations": [
            "Root placement is descriptive and is not a throughput estimator.",
            "Accepted artifacts do not identify a responsive-survivor bottleneck.",
        ],
    }


def build_report(
    campaign_root: Path, *, command: str, invocation: Mapping[str, object] | None = None
) -> dict[str, object]:
    root = Path(campaign_root)
    if root.name != CAMPAIGN_ID:
        raise PlacementContrastError("source root is not the fixed CERT13 campaign")
    try:
        seal = verify_evidence_seal(root)
    except (EvidenceSealError, OSError) as exc:
        raise PlacementContrastError("campaign seal is invalid") from exc
    if (
        seal.tree_sha256 != EXPECTED_CAMPAIGN_TREE_SHA256
        or seal.seal_sha256 != EXPECTED_CAMPAIGN_SEAL_SHA256
    ):
        raise PlacementContrastError("campaign seal differs from accepted evidence")
    plan = _read_json(root / "plan.json", "campaign plan")
    validation_context = _accepted_validation_context(root)
    arms = [_arm_record(root, slot, plan) for slot in _adaptive_slots(plan)]
    changed = [int(arm["changed_root_position_count"]) for arm in arms]
    turnover = [int(arm["root_set_turnover_count"]) for arm in arms]
    if invocation is None:
        invocation = {
            "argv": [AUDIT_COMMAND, "--campaign-root", str(root), "--output-root", str(root.parent / "cert13-audit-output")],
            "resolved_campaign_root": str(root.resolve()),
            "resolved_output_root": str((root.parent / "cert13-audit-output").resolve()),
        }
    return {
        "schema_version": 1,
        "audit_id": "cert13-placement-contrast-v1",
        "verdict": "PASS",
        "generator_id": AUDIT_COMMAND,
        "invocation": dict(invocation),
        "source": {
            "campaign_id": CAMPAIGN_ID,
            "campaign_revision": CAMPAIGN_REVISION,
            "campaign_tree_sha256": seal.tree_sha256,
            "campaign_seal_sha256": seal.seal_sha256,
            "plan_sha256": _sha256_file(root / "plan.json"),
        },
        "accepted_validation_context": validation_context,
        "fixed_denominator": {
            "adaptive_arm_count": 5,
            "slot_ids": list(ADAPTIVE_SLOTS),
            "tree_root_position_count": 21,
            "consensus_quorum_Q": 21,
        },
        "arms": arms,
        "summary": {
            "changed_root_position_counts": changed,
            "root_set_turnover_counts": turnover,
            "changed_root_position_range": [min(changed), max(changed)],
            "root_set_turnover_range": [min(turnover), max(turnover)],
            "all_epoch2_exact_eligible_ranking_binding": True,
            "score_margin_state": "unknown",
        },
        "claim_boundary": "Descriptive accepted-artifact analysis only; C-007 remains rejected and unchanged.",
    }


def write_report(campaign_root: Path, output_root: Path, *, command: str) -> Path:
    source = Path(campaign_root).resolve(strict=False)
    output = Path(output_root).resolve(strict=False)
    try:
        output.relative_to(source)
    except ValueError:
        pass
    else:
        raise PlacementContrastError("audit output root must be outside the sealed campaign")
    if output.exists():
        raise PlacementContrastError("audit output root already exists")
    try:
        command_argv = command.split("\x00")
        report = build_report(
            source,
            command=command,
            invocation={
                # Store canonical operands so the report has no dependence on
                # the generator's working directory.
                "argv": [command_argv[0], "--campaign-root", str(source), "--output-root", str(output)],
                "resolved_campaign_root": str(source),
                "resolved_output_root": str(output),
            },
        )
    except PlacementContrastError as exc:
        output.mkdir(parents=True)
        match = re.match(r"^(slot-\d+)", str(exc))
        abort = {
            "schema_version": 1,
            "audit_id": "cert13-placement-contrast-v1",
            "verdict": "INVALID",
            "command": command,
            "failure_stage": "source_reconstruction",
            "slot_id": match.group(1) if match else None,
            "error_reason": str(exc),
            "source_identity": {
                "requested_campaign_id": source.name,
                "expected_campaign_id": CAMPAIGN_ID,
                "expected_campaign_tree_sha256": EXPECTED_CAMPAIGN_TREE_SHA256,
                "expected_campaign_seal_sha256": EXPECTED_CAMPAIGN_SEAL_SHA256,
                "state": "not_accepted_for_aggregate",
            },
            "aggregate_conclusion": None,
        }
        path = output / ABORT_NAME
        path.write_text(canonical_json(abort), encoding="ascii")
        raise PlacementContrastAbort(path, str(exc)) from exc
    output.mkdir(parents=True)
    path = output / REPORT_NAME
    path.write_text(canonical_json(report), encoding="ascii")
    return path
