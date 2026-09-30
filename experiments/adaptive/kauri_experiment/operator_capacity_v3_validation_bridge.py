"""No-launch W18 post-run authority and raw-validation bridge.

This module is deliberately usable only *after* the runner has sealed a
successful, no-retry arm.  It derives the native verifier invocations from
the retained execution artefacts, not from caller-provided policy values; the
only caller-supplied values are the two already externally approved verifier
binary paths and fresh external output paths.  It neither launches processes
nor makes a campaign or thesis claim.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import operator_capacity_consumption_audit as consumption_audit
from . import operator_capacity_v3_authority as authority
from . import operator_capacity_v3_pair_evaluator as pair_evaluator
from . import operator_capacity_v3_raw_validator as raw_validator


class OperatorCapacityV3ValidationBridgeError(ValueError):
    """A completed arm does not provide safely derivable validation inputs."""


@dataclass(frozen=True)
class ValidationInputs:
    """Exact post-run inputs used by the external authority and revalidator."""

    pins: Mapping[str, object]
    stage_a_command: tuple[str, ...]
    stage_b_command: tuple[str, ...]


def _fail(message: str) -> None:
    raise OperatorCapacityV3ValidationBridgeError(message)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = authority._read(path, label, 256 * 1024)
        value = authority._json(raw, label)
    except authority.OperatorCapacityV3AuthorityError as exc:
        raise OperatorCapacityV3ValidationBridgeError(str(exc)) from exc
    return value


def _value(argv: Sequence[str], flag: str, label: str) -> str:
    if not isinstance(argv, Sequence) or isinstance(argv, (str, bytes)):
        _fail(f"{label} is not an argv sequence")
    indexes = [index for index, item in enumerate(argv) if item == flag]
    if len(indexes) != 1 or indexes[0] + 1 >= len(argv):
        _fail(f"{label} lacks exactly one {flag}")
    value = argv[indexes[0] + 1]
    if not isinstance(value, str) or not value:
        _fail(f"{label} {flag} value is invalid")
    return value


def _main_config_issuer(root: Path, manifest: Mapping[str, object]) -> tuple[int, str]:
    try:
        raw = authority._read(root / "config/hotstuff.gen.conf", "materialized main configuration", 256 * 1024)
        artifacts = manifest.get("artifact_sha256")
        if not isinstance(artifacts, Mapping) or artifacts.get("config/hotstuff.gen.conf") != hashlib.sha256(raw).hexdigest():
            _fail("materialized main configuration differs from manifest hash")
        lines = raw.decode("ascii").splitlines()
    except (UnicodeDecodeError, authority.OperatorCapacityV3AuthorityError) as exc:
        _fail("materialized main configuration is unavailable")
        raise AssertionError from exc
    values: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition(" = ")
        if separator and key in {"epoch-change-issuer-id", "epoch-change-issuer-public-key"}:
            if key in values:
                _fail("materialized main configuration repeats epoch issuer setting")
            values[key] = value
    if set(values) != {"epoch-change-issuer-id", "epoch-change-issuer-public-key"}:
        _fail("materialized main configuration lacks epoch issuer identity")
    try:
        issuer_id = int(values["epoch-change-issuer-id"], 10)
    except ValueError as exc:
        _fail("materialized epoch issuer ID is invalid")
        raise AssertionError from exc
    public_key = values["epoch-change-issuer-public-key"]
    if issuer_id <= 0 or len(public_key) != 66 or any(char not in "0123456789abcdef" for char in public_key):
        _fail("materialized epoch issuer public key is invalid")
    return issuer_id, public_key


def _retained_manager_issuer_reference(root: Path, manifest: Mapping[str, object]) -> str:
    try:
        raw = authority._read(root / "runtime/manager-argv.json", "retained pre-launch manager argv", 256 * 1024)
        value = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError, authority.OperatorCapacityV3AuthorityError) as exc:
        _fail("retained pre-launch manager argv is unavailable")
        raise AssertionError from exc
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"
    if (not isinstance(value, list) or not all(isinstance(item, str) for item in value) or
            raw != canonical or hashlib.sha256(raw).hexdigest() != manifest.get("manager_argv_sha256")):
        _fail("retained pre-launch manager argv differs from materialization manifest")
    reference = _value(value, "--operator-capacity-stage-b-issuer-reference", "retained manager argv")
    if len(reference) > 128 or any(ord(character) < 0x20 or ord(character) > 0x7e for character in reference):
        _fail("retained epoch issuer reference is invalid")
    return reference


def _strict_pins(pins: Mapping[str, object]) -> dict[str, object]:
    result = dict(pins)
    try:
        consumption_audit._validate_pins(result)
    except consumption_audit.ConsumptionAuditError as exc:
        raise OperatorCapacityV3ValidationBridgeError(str(exc)) from exc
    return result


def derive_validation_inputs(
    root: Path, *, stage_a_verifier_binary: Path, stage_b_verifier_binary: Path,
) -> ValidationInputs:
    """Derive all W18 authority pins from retained native outputs.

    This is intentionally post-run.  The consumption record provides dynamic
    decision values before the authority creates the first Stage-B receipt.
    The epoch issuer reference is recovered from the exact manager argv which
    passed pre-spawn admission and is hash-bound to the materialization.
    """
    root = Path(root).resolve()
    manifest = _read_json(root / "materialization-manifest.json", "materialization manifest")
    stage_a = _read_json(root / "runtime/stage-a-verifier-receipt.json", "Stage-A verifier receipt")
    stage_b_path = root / "runtime/stage-b-verifier-receipt.json"
    stage_b = _read_json(stage_b_path, "Stage-B verifier receipt") if stage_b_path.exists() else None
    consumption = _read_json(root / "raw/consumption.json", "consumption record")
    required_manifest = {"revision", "arm", "stage_a_native_arm", "stage_a_verifier_arguments"}
    if not required_manifest.issubset(manifest) or not isinstance(manifest["stage_a_verifier_arguments"], list):
        _fail("materialization manifest lacks native Stage-A invocation")
    required_a = {
        "source_revision", "arm", "epoch0_consensus_digest", "epoch0_topology_digest",
        "approved_capacity_digest", "issuer_id", "issuer_reference",
        "issuer_public_key_fingerprint",
    }
    required_b = {
        "issuer_id", "issuer_reference", "issuer_public_key_fingerprint",
        "baseline_snapshot_id", "baseline_evidence_cutoff", "decision_monotonic_raw_ns",
    }
    required_consumption = {
        "run_id", "source_instance", "hard_deadline_monotonic_raw_ns",
        "successor_policy_snapshot_id", "baseline_snapshot_id",
        "baseline_evidence_cutoff", "decision_monotonic_raw_ns",
    }
    if (not required_a.issubset(stage_a) or not required_consumption.issubset(consumption) or
            (stage_b is not None and not required_b.issubset(stage_b))):
        _fail("retained native receipts lack required authority fields")
    if stage_a.get("source_revision") != manifest["revision"] or stage_a.get("arm") != manifest["stage_a_native_arm"]:
        _fail("Stage-A receipt differs from materialized arm or revision")
    epoch_change_issuer_reference = _retained_manager_issuer_reference(root, manifest)
    issuer_id, issuer_public_key = _main_config_issuer(root, manifest)
    epoch_fingerprint = hashlib.sha256(bytes.fromhex(issuer_public_key)).hexdigest()
    if stage_b is not None and (stage_b.get("issuer_id") != issuer_id or
            stage_b.get("issuer_reference") != epoch_change_issuer_reference or
            stage_b.get("issuer_public_key_fingerprint") != epoch_fingerprint):
        _fail("Stage-B receipt differs from materialized epoch issuer identity")
    pins = _strict_pins({
        "source_revision": manifest["revision"],
        "arm": stage_a["arm"],
        "epoch0_consensus_digest": stage_a["epoch0_consensus_digest"],
        "epoch0_topology_digest": stage_a["epoch0_topology_digest"],
        "approved_capacity_digest": stage_a["approved_capacity_digest"],
        "label_issuer_id": stage_a["issuer_id"],
        "label_issuer_reference": stage_a["issuer_reference"],
        "label_issuer_public_key_fingerprint": stage_a["issuer_public_key_fingerprint"],
        "epoch_change_issuer_id": issuer_id,
        "epoch_change_issuer_reference": epoch_change_issuer_reference,
        "epoch_change_issuer_public_key_fingerprint": epoch_fingerprint,
        "run_id": consumption["run_id"],
        "source_instance": consumption["source_instance"],
        "baseline_snapshot_id": consumption["baseline_snapshot_id"],
        "baseline_evidence_cutoff": consumption["baseline_evidence_cutoff"],
        "decision_monotonic_raw_ns": consumption["decision_monotonic_raw_ns"],
        "hard_deadline_monotonic_raw_ns": consumption["hard_deadline_monotonic_raw_ns"],
        "successor_policy_snapshot_id": consumption["successor_policy_snapshot_id"],
    })
    if (consumption.get("run_id") != pins["run_id"] or
            consumption.get("source_instance") != pins["source_instance"]):
        _fail("consumption record is not stable while deriving authority pins")
    stage_a_command = (str(Path(stage_a_verifier_binary).resolve()), *manifest["stage_a_verifier_arguments"])
    stage_b_command = (
        str(Path(stage_b_verifier_binary).resolve()),
        "--epoch0-tree-file", str(root / "config/epoch0.tree"),
        "--stage-b-authorization-wire", str(root / "raw/stage-b-authorization.wire"),
        "--issuer-id", str(pins["epoch_change_issuer_id"]),
        "--issuer-reference", str(pins["epoch_change_issuer_reference"]),
        "--issuer-public-key-hex", issuer_public_key,
        "--issuer-public-key-fingerprint", str(pins["epoch_change_issuer_public_key_fingerprint"]),
        "--label-issuer-reference", str(pins["label_issuer_reference"]),
        "--approved-capacity-digest", str(pins["approved_capacity_digest"]),
        "--arm", str(pins["arm"]),
        "--source-revision", str(pins["source_revision"]),
    )
    return ValidationInputs(pins=pins, stage_a_command=stage_a_command, stage_b_command=stage_b_command)


def independently_recompute_verifiers(
    root: Path, authority_document: Mapping[str, object], *,
    stage_a_verifier_binary: Path, stage_b_verifier_binary: Path,
) -> Mapping[str, object]:
    """Re-run both native verifiers and return only their retained hashes."""
    inputs = derive_validation_inputs(
        root, stage_a_verifier_binary=stage_a_verifier_binary,
        stage_b_verifier_binary=stage_b_verifier_binary)
    if authority_document.get("pins") != inputs.pins:
        _fail("independent verifier inputs differ from the external authority pins")
    try:
        manifest = _read_json(Path(root) / "materialization-manifest.json", "materialization manifest")
        stage_a_command = authority._stage_a_command_is_bound(
            inputs.stage_a_command, root=Path(root), manifest=manifest)
        stage_b_command = authority._stage_b_command_is_bound(
            inputs.stage_b_command, root=Path(root), manifest=manifest, pins=inputs.pins)
        authority._rerun(stage_a_command, "Stage-A",
                          authority._read(Path(root) / "runtime/stage-a-verifier-receipt.json", "Stage-A verifier receipt"),
                          runner=subprocess.run)
        authority._rerun(stage_b_command, "Stage-B",
                          authority._read(Path(root) / "runtime/stage-b-verifier-receipt.json", "Stage-B verifier receipt"),
                          runner=subprocess.run)
    except authority.OperatorCapacityV3AuthorityError as exc:
        raise OperatorCapacityV3ValidationBridgeError(str(exc)) from exc
    return {
        "stage_a_receipt_sha256": hashlib.sha256(authority._read(Path(root) / "runtime/stage-a-verifier-receipt.json", "Stage-A verifier receipt")).hexdigest(),
        "stage_b_receipt_sha256": hashlib.sha256(authority._read(Path(root) / "runtime/stage-b-verifier-receipt.json", "Stage-B verifier receipt")).hexdigest(),
    }


def make_pair_revalidator(
    *, stage_a_verifier_binary: Path, stage_b_verifier_binary: Path,
) -> Callable[[Path, Path], Mapping[str, Any]]:
    """Return the exact callback expected by the matched-pair evaluator.

    The callback always repeats the native verifier work.  It does not trust a
    previously persisted raw result and therefore cannot turn a single arm
    into an accepted pair or campaign result.
    """
    def revalidate(candidate_root: Path, authority_path: Path) -> Mapping[str, Any]:
        try:
            return raw_validator.validate_operator_capacity_v3_raw(
                candidate_root, authority_path=authority_path,
                independently_recompute_verifiers=lambda checked_root, document: independently_recompute_verifiers(
                    checked_root, document, stage_a_verifier_binary=stage_a_verifier_binary,
                    stage_b_verifier_binary=stage_b_verifier_binary))
        except Exception as exc:
            return raw_validator._failure("RAW_CONTRACT_INCOMPLETE", str(exc))
    return revalidate


def validate_completed_arm(
    root: Path, *, authority_output: Path, raw_validation_output: Path,
    stage_a_verifier_binary: Path, stage_b_verifier_binary: Path,
) -> dict[str, Any]:
    """Write a fresh external authority and canonical non-claim raw result."""
    root = Path(root).resolve()
    try:
        inputs = derive_validation_inputs(
            root, stage_a_verifier_binary=stage_a_verifier_binary,
            stage_b_verifier_binary=stage_b_verifier_binary)
        authority.produce_operator_capacity_v3_authority(
            root, authority_path=authority_output, pins=inputs.pins,
            stage_a_command=inputs.stage_a_command, stage_b_command=inputs.stage_b_command)
        return pair_evaluator.write_canonical_raw_validation_result(
            root=root, authority_path=authority_output, output_path=raw_validation_output,
            revalidate=make_pair_revalidator(
                stage_a_verifier_binary=stage_a_verifier_binary,
                stage_b_verifier_binary=stage_b_verifier_binary))
    except Exception as exc:
        return _write_external_incomplete(root, raw_validation_output, str(exc))


def _write_external_incomplete(root: Path, output_path: Path, detail: str) -> dict[str, Any]:
    """Seal an external raw-validation abort without overwriting evidence."""
    root, output_path = Path(root).resolve(), Path(output_path).resolve()
    try:
        output_path.relative_to(root)
    except ValueError:
        pass
    else:
        _fail("raw validation abort output must be outside runner-owned output")
    result = raw_validator._failure("RAW_CONTRACT_INCOMPLETE", detail or "validation bridge failed")
    raw = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"
    if not output_path.parent.is_dir():
        _fail("raw validation abort parent does not exist")
    try:
        descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        raise OperatorCapacityV3ValidationBridgeError("cannot create fresh external raw validation abort") from error
    try:
        offset = 0
        while offset < len(raw):
            count = os.write(descriptor, raw[offset:])
            if count <= 0:
                _fail("cannot write external raw validation abort")
            offset += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return result
