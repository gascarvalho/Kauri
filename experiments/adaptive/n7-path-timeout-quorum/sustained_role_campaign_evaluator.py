#!/usr/bin/env python3
"""Prospective matched-campaign gate for the sustained-role N=7 study.

The evaluator never launches a process and never accepts producer summaries.
It first reruns the independent per-arm raw-bundle validator, then separately
reopens the receipt-pinned replica streams and recomputes the frozen late
all-seven common-commit metric.  Its positive gate is deliberately scoped to
one actor, one fault policy, one Epoch-0 tree schedule, and one host.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
from types import ModuleType
from typing import Any, Mapping, Sequence


PAIR_COUNT = 6
LATE_START_NS = 20_000_000_000
LATE_END_NS = 60_000_000_000
DESIGNATED_OBSERVER = 2
FIXED_ARM = "fixed_e0"
ADAPTIVE_ARM = "adaptive_e1"
FROZEN_PAIR_SCHEDULE = (
    (FIXED_ARM, ADAPTIVE_ARM),
    (ADAPTIVE_ARM, FIXED_ARM),
    (FIXED_ARM, ADAPTIVE_ARM),
    (ADAPTIVE_ARM, FIXED_ARM),
    (FIXED_ARM, ADAPTIVE_ARM),
    (ADAPTIVE_ARM, FIXED_ARM),
)

FREEZE_KIND = "kauri-n7-sustained-role-matched-campaign-freeze-v1"
RESULT_KIND = "kauri-n7-sustained-role-matched-campaign-result-v1"
MANIFEST_BOUND_RESULT_KIND = "kauri-n7-sustained-role-matched-campaign-result-v2"
RECEIPT_KIND = "kauri-n7-sustained-role-raw-bundle-receipt-v1"
MANIFEST_KIND = "kauri-n7-sustained-role-serial-campaign-manifest-v1"
STAGE_RECEIPT_KIND = "kauri-n7-sustained-role-serial-cell-stage-receipt-v1"
NEGATIVE_MARKERS = (
    "runtime/sustained-role-campaign-staging-abort.json",
    "runtime/sustained-role-campaign-launch-abort.json",
    "sustained-role-fixed-e0-abort.json",
    "sustained-role-adaptive-e1-abort.json",
)
VALIDATOR_VERDICT = "PASS_COMPONENT_ONLY_NO_CLAIM"
SCENARIO_SCOPE = "n7-one-hard-actor-role-scoped-persistent-selected-omission-v1"
_HEX = frozenset("0123456789abcdef")
_MAX_SMALL = 256 * 1024
_MAX_RAW = 16 * 1024 * 1024
_HERE = Path(__file__).resolve().parent

_MAIN_CONFIG_FIXED = {
    "block-size": "1",
    "nworker": "2",
    "repnworker": "1",
    "pace-maker": "dummy",
    "proposer": "0",
    "fan-out": "2",
    "piped_latency": "1",
    "async_blocks": "2",
    "base-timeout": "2.0",
    "prop-delay": "0.1",
    "aggregation-timeout": "0.5",
    "leader-progress-timeout": "5.0",
    "leader-activation-grace": "1.0",
    "client-ip": "127.0.0.1",
    "tree-generation": "file",
    "tree-switch-period": "2",
    "epoch-protocol-mode": "adaptive_v2",
    "epoch-change-issuer-id": "1",
    "epoch-change-minimum-activation-delay": "5",
    "epoch-change-maximum-activation-delay": "5",
    "epoch-change-maximum-block-extra-bytes": "4096",
    "epoch-change-maximum-ancestry-blocks": "128",
    "max-rep-msg": "4194304",
}
_MAIN_CONFIG_DYNAMIC = {
    "tree-generation-fpath", "epoch-change-issuer-public-key",
    "epoch-manager-address", "epoch-manager-tls-cert",
}
_REPLICA_ENTRY = re.compile(
    r"^127\.0\.0\.1:(?P<peer>[0-9]+);(?P<client>[0-9]+), "
    r"(?P<bls>[^,\s]+), (?P<tls>[^,\s]+)$"
)
_BOOT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


FROZEN_DESIGN: dict[str, Any] = {
    "schema_version": 1,
    "scenario_scope": SCENARIO_SCOPE,
    "pair_schedule": [list(order) for order in FROZEN_PAIR_SCHEDULE],
    "pair_count": PAIR_COUNT,
    "cell_count": PAIR_COUNT * 2,
    "retry_policy": "none",
    "metric": {
        "name": "all-seven-common-authoritative-commit-count-v1",
        "clock": "CLOCK_MONOTONIC_RAW",
        "window": "[anchor+20s,anchor+60s)",
        "start_offset_ns": LATE_START_NS,
        "end_offset_ns": LATE_END_NS,
        "designated_observer": DESIGNATED_OBSERVER,
        "primary_pair_effect": "adaptive_count_minus_fixed_count",
        "ratio_policy": "null-when-fixed-zero",
    },
    "improvement_gate": {
        "required_accepted_pairs": 6,
        "required_positive_pairs": 5,
        "required_positive_pairs_per_order": 2,
        "aggregate_ratio_numerator": 11,
        "aggregate_ratio_denominator": 10,
        "aggregate_positive_fixed_denominator_required": True,
        "zero_denominator_policy": {
            "fixed_zero_adaptive_positive": "positive-pair-ratio-null",
            "both_zero": "neutral-pair-ratio-null",
        },
    },
    "comparability": {
        "must_match": [
            "repository revision", "source-derived Epoch-0 digest",
            "Epoch-0 tree bytes", "normalized causal main-config settings",
            "native fault-profile bytes", "selection-policy bytes",
            "application and manager binary bytes", "hard timeout",
            "fault schedule duration", "scheduled window duration",
        ],
        "allowed_per_cell_differences": [
            "BLS keys", "TLS keys and certificate identities",
            "epoch issuer key", "loopback base ports", "run and source identities",
            "absolute paths below each cell root", "absolute raw-clock timestamps",
        ],
        "epoch0_digest_basis": (
            "membership IDs plus epoch0.tree definitions only; native "
            "adaptive_v2_epoch_zero_input does not consume replica keys"
        ),
    },
    "pilot_policy": "excluded-from-campaign-figure-and-claim",
    "claim_scope": "one N=7 same-host fault/tree scenario; no general Byzantine or safety claim",
}


class CampaignEvaluationError(ValueError):
    """The supplied cells cannot form the prospectively frozen campaign."""


def _fail(message: str) -> None:
    raise CampaignEvaluationError(message)


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("ascii") + b"\n"
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CampaignEvaluationError("value is not canonical ASCII JSON") from exc


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _hex(value: object, length: int, label: str) -> str:
    if (not isinstance(value, str) or len(value) != length or
            any(character not in _HEX for character in value)):
        _fail(f"{label} is not lower-case hexadecimal of length {length}")
    return value


def _read(path: Path, label: str, maximum: int) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise CampaignEvaluationError(f"{label} is not a readable regular file") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > maximum:
            _fail(f"{label} is not a bounded non-empty regular file")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                _fail(f"{label} changed during read")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        stable_identity = lambda value: (
            value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
            value.st_uid, value.st_gid, value.st_size, value.st_mtime_ns,
            value.st_ctime_ns,
        )
        if os.read(descriptor, 1) or stable_identity(after) != stable_identity(before):
            _fail(f"{label} changed during read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _pairs(label: str):
    def decode(items: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                _fail(f"{label} repeats JSON field {key}")
            result[key] = value
        return result
    return decode


def _strict_json(raw: bytes, label: str, *, canonical: bool) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs(label),
                           parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CampaignEvaluationError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        _fail(f"{label} is not a JSON object")
    if canonical and raw != _canonical(value):
        _fail(f"{label} is not canonical JSON")
    return value


def _safe_child(root: Path, relative: object, label: str) -> Path:
    if (not isinstance(relative, str) or not relative or Path(relative).is_absolute() or
            ".." in Path(relative).parts):
        _fail(f"{label} path is not a safe relative child")
    lexical_root = _reject_lexical_symlink_ancestors(root, f"{label} root")
    lexical_child = _reject_lexical_symlink_ancestors(lexical_root / relative, label)
    path = lexical_child.resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise CampaignEvaluationError(f"{label} path escapes its root") from exc
    return path


def _reject_lexical_symlink_ancestors(path: Path, label: str) -> Path:
    lexical = Path(path).absolute()
    for ancestor in (lexical, *lexical.parents):
        try:
            if ancestor.is_symlink():
                _fail(f"{label} has a symlink ancestor")
        except OSError as exc:
            raise CampaignEvaluationError(f"cannot inspect {label} ancestry") from exc
    return lexical


def _descriptor(root: Path, value: object, label: str, maximum: int) -> tuple[bytes, str]:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256"}:
        _fail(f"{label} descriptor schema drifted")
    expected = _hex(value["sha256"], 64, f"{label} SHA-256")
    raw = _read(_safe_child(root, value["path"], label), label, maximum)
    if _sha(raw) != expected:
        _fail(f"{label} differs from its receipt SHA-256")
    return raw, expected


def frozen_design_sha256() -> str:
    return _sha(_canonical(FROZEN_DESIGN))


def build_campaign_freeze(*, campaign_id: str, frozen_utc: str,
                          campaign_approval_reference: str,
                          repository_revision: str) -> dict[str, Any]:
    """Create the immutable pre-live campaign gate; this performs no launch."""
    if not isinstance(campaign_id, str) or not campaign_id.strip():
        _fail("campaign ID is empty")
    if not isinstance(campaign_approval_reference, str) or not campaign_approval_reference.strip():
        _fail("campaign approval reference is empty")
    _timestamp(frozen_utc, "campaign freeze timestamp")
    _hex(repository_revision, 40, "campaign repository revision")
    freeze: dict[str, Any] = {
        "schema_version": 1,
        "kind": FREEZE_KIND,
        "campaign_id": campaign_id,
        "frozen_utc": frozen_utc,
        "campaign_approval_reference": campaign_approval_reference,
        "repository_revision": repository_revision,
        "design": deepcopy(FROZEN_DESIGN),
        "design_sha256": frozen_design_sha256(),
    }
    freeze["freeze_sha256"] = _sha(_canonical(freeze))
    return freeze


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        _fail(f"{label} is not an explicit UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise CampaignEvaluationError(f"{label} is invalid") from exc
    if parsed.tzinfo != timezone.utc:
        _fail(f"{label} is not UTC")
    return parsed


def _check_freeze(freeze: Mapping[str, Any]) -> datetime:
    required = {
        "schema_version", "kind", "campaign_id", "frozen_utc",
        "campaign_approval_reference", "repository_revision", "design",
        "design_sha256", "freeze_sha256",
    }
    if set(freeze) != required or freeze.get("schema_version") != 1 or freeze.get("kind") != FREEZE_KIND:
        _fail("campaign freeze schema drifted")
    if not isinstance(freeze.get("campaign_id"), str) or not freeze["campaign_id"].strip():
        _fail("campaign freeze ID is invalid")
    if (not isinstance(freeze.get("campaign_approval_reference"), str) or
            not freeze["campaign_approval_reference"].strip()):
        _fail("campaign approval reference is invalid")
    _hex(freeze.get("repository_revision"), 40, "campaign repository revision")
    if freeze.get("design") != FROZEN_DESIGN or freeze.get("design_sha256") != frozen_design_sha256():
        _fail("campaign design differs from the prospective frozen gate")
    semantic = {key: value for key, value in freeze.items() if key != "freeze_sha256"}
    if freeze.get("freeze_sha256") != _sha(_canonical(semantic)):
        _fail("campaign freeze SHA-256 does not recompute")
    return _timestamp(freeze.get("frozen_utc"), "campaign freeze timestamp")


def _manifest_relative_root(value: object, label: str) -> str:
    if (not isinstance(value, str) or not value or Path(value).is_absolute() or
            ".." in Path(value).parts):
        _fail(f"{label} is not a safe relative path")
    return value


def _check_campaign_manifest(freeze: Mapping[str, Any], manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate the pre-run serial manifest without importing its operator.

    This duplicated, deliberately small parser prevents the evaluator from
    accepting a caller-selected subset after runs have completed.  The
    operator imports this module, so importing its validation helper here
    would create a circular trust dependency.
    """
    required = {"schema_version", "kind", "campaign_id", "freeze_sha256",
                "repository_revision", "prepared_utc", "cells", "manifest_sha256"}
    if (set(manifest) != required or manifest.get("schema_version") != 1 or
            manifest.get("kind") != MANIFEST_KIND):
        _fail("campaign manifest schema drifted")
    if (manifest.get("campaign_id") != freeze.get("campaign_id") or
            manifest.get("freeze_sha256") != freeze.get("freeze_sha256") or
            manifest.get("repository_revision") != freeze.get("repository_revision")):
        _fail("campaign manifest is not bound to the supplied freeze")
    _timestamp(manifest.get("prepared_utc"), "campaign manifest preparation timestamp")
    semantic = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if manifest.get("manifest_sha256") != _sha(_canonical(semantic)):
        _fail("campaign manifest SHA-256 does not recompute")
    values = manifest.get("cells")
    if not isinstance(values, list) or len(values) != PAIR_COUNT * 2:
        _fail("campaign manifest requires exactly twelve cells")
    parsed: list[dict[str, Any]] = []
    ordinal = 0
    for pair_index, order in enumerate(FROZEN_PAIR_SCHEDULE, start=1):
        for arm in order:
            ordinal += 1
            cell = values[ordinal - 1]
            required_cell = {"ordinal", "pair_index", "arm", "run_id", "run_root",
                             "target_host", "hard_timeout_seconds", "retry_policy"}
            if not isinstance(cell, Mapping) or set(cell) != required_cell:
                _fail(f"campaign manifest cell {ordinal} schema drifted")
            if (cell.get("ordinal"), cell.get("pair_index"), cell.get("arm")) != (
                    ordinal, pair_index, arm):
                _fail(f"campaign manifest cell {ordinal} contradicts the frozen AB/BA schedule")
            run_id = cell.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                _fail(f"campaign manifest cell {ordinal} run ID is invalid")
            target_host = cell.get("target_host")
            if (not isinstance(target_host, str) or not target_host.strip() or
                    cell.get("hard_timeout_seconds") != 210 or cell.get("retry_policy") != "none"):
                _fail(f"campaign manifest cell {ordinal} host, timeout, or retry contract drifted")
            parsed.append({"ordinal": ordinal, "pair_index": pair_index, "arm": arm,
                           "run_id": run_id,
                           "run_root": _manifest_relative_root(
                               cell.get("run_root"), f"campaign manifest cell {ordinal} run root"),
                           "target_host": target_host,
                           "hard_timeout_seconds": 210, "retry_policy": "none"})
    if len({cell["run_id"] for cell in parsed}) != len(parsed):
        _fail("campaign manifest reuses a run ID")
    if len({cell["run_root"] for cell in parsed}) != len(parsed):
        _fail("campaign manifest reuses a run root")
    if len({cell["target_host"] for cell in parsed}) != 1:
        _fail("campaign manifest does not retain one target host")
    return parsed


def _stage_receipt(root: Path, record: Mapping[str, Any], expected: Mapping[str, Any],
                   manifest: Mapping[str, Any], freeze: Mapping[str, Any],
                   *, required_host_identity: Mapping[str, str] | None = None) -> str:
    path = _safe_child(root, record.get("stage_receipt_path"),
                       f"cell {expected['ordinal']} stage receipt")
    raw = _read(path, f"cell {expected['ordinal']} stage receipt", _MAX_SMALL)
    if _sha(raw) != _hex(record.get("stage_receipt_sha256"), 64,
                         f"cell {expected['ordinal']} stage receipt pin"):
        _fail(f"cell {expected['ordinal']} stage receipt differs from its campaign pin")
    receipt = _strict_json(raw, f"cell {expected['ordinal']} stage receipt", canonical=True)
    required = {"schema_version", "kind", "state", "manifest_sha256", "freeze_sha256",
                "ordinal", "pair_index", "arm", "run_id", "run_root", "target_host",
                "host_identity", "clock", "scheduled_window", "hard_timeout_seconds",
                "no_retry", "authorization_request_sha256", "claim_eligible",
                "figure_eligible", "stage_receipt_sha256"}
    if set(receipt) != required or receipt.get("schema_version") != 1 or receipt.get("kind") != STAGE_RECEIPT_KIND:
        _fail(f"cell {expected['ordinal']} stage receipt schema drifted")
    semantic = {key: value for key, value in receipt.items() if key != "stage_receipt_sha256"}
    if receipt.get("stage_receipt_sha256") != _sha(_canonical(semantic)):
        _fail(f"cell {expected['ordinal']} stage receipt SHA-256 does not recompute")
    required_values = {
        "manifest_sha256": manifest["manifest_sha256"], "freeze_sha256": freeze["freeze_sha256"],
        "ordinal": expected["ordinal"], "pair_index": expected["pair_index"], "arm": expected["arm"],
        "run_id": expected["run_id"], "run_root": expected["run_root"],
        "target_host": expected["target_host"], "clock": "CLOCK_MONOTONIC_RAW",
        "hard_timeout_seconds": expected["hard_timeout_seconds"], "no_retry": True,
        "claim_eligible": False, "figure_eligible": False,
    }
    if any(receipt.get(key) != value for key, value in required_values.items()):
        _fail(f"cell {expected['ordinal']} stage receipt is not bound to the frozen manifest")
    if (receipt.get("state") != "MATERIALIZED_NO_LAUNCH_EXTERNAL_EXACT_APPROVAL_REQUIRED" or
            not isinstance(receipt.get("scheduled_window"), Mapping) or
            type(receipt["scheduled_window"].get("start_monotonic_ns")) is not int or
            type(receipt["scheduled_window"].get("end_monotonic_ns")) is not int or
            receipt["scheduled_window"]["start_monotonic_ns"] >= receipt["scheduled_window"]["end_monotonic_ns"] or
            not isinstance(receipt.get("authorization_request_sha256"), str) or
            _hex(receipt["authorization_request_sha256"], 64,
                 f"cell {expected['ordinal']} authorization request SHA-256") is None):
        _fail(f"cell {expected['ordinal']} stage receipt materialization evidence is invalid")
    identity = receipt.get("host_identity")
    if (not isinstance(identity, Mapping) or set(identity) != {"hostname", "linux_boot_id"} or
            identity.get("hostname") != expected["target_host"] or
            not isinstance(identity.get("linux_boot_id"), str) or
            _BOOT_ID.fullmatch(identity["linux_boot_id"]) is None):
        _fail(f"cell {expected['ordinal']} stage receipt host identity is invalid")
    if required_host_identity is not None and identity != required_host_identity:
        _fail(f"cell {expected['ordinal']} stage receipt is from a different host or Linux boot")
    return receipt["authorization_request_sha256"]


def _reject_negative_markers(root: Path, *, ordinal: int) -> None:
    """An inner raw receipt cannot override an outer no-retry abort."""
    for relative in NEGATIVE_MARKERS:
        path = _safe_child(root, relative, f"cell {ordinal} negative marker")
        if path.exists() or path.is_symlink():
            _fail(f"cell {ordinal} has a sealed negative marker: {relative}")


_VALIDATOR_MODULE: ModuleType | None = None


def _validator() -> ModuleType:
    global _VALIDATOR_MODULE
    if _VALIDATOR_MODULE is None:
        path = _HERE / "sustained_role_validator.py"
        specification = importlib.util.spec_from_file_location(
            "kauri_n7_sustained_role_validator_for_campaign", path)
        if specification is None or specification.loader is None:
            _fail("independent raw validator cannot be loaded")
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        _VALIDATOR_MODULE = module
    return _VALIDATOR_MODULE


def _validate_accepted_bundle(root: Path, receipt_relative: Path) -> Mapping[str, Any]:
    try:
        result = _validator().validate_raw_bundle(root, receipt_relative)
    except Exception as exc:
        raise CampaignEvaluationError("independent per-arm raw validation failed") from exc
    if not isinstance(result, Mapping) or result.get("verdict") != VALIDATOR_VERDICT:
        _fail("independent per-arm raw validator did not accept the receipt")
    return result


def _events(raw: bytes, *, run_id: str, source: str) -> list[dict[str, Any]]:
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise CampaignEvaluationError(f"{source} raw stream is not UTF-8") from exc
    if not lines:
        _fail(f"{source} raw stream is empty")
    expected_kind = "replica"
    result: list[dict[str, Any]] = []
    prior_sequence = 0
    prior_ns = -1
    source_instance: str | None = None
    required = {
        "event_schema_version", "run_id", "source_kind", "source_id",
        "source_instance", "source_sequence", "source_monotonic_ns",
        "event_type", "payload",
    }
    for line in lines:
        event = _strict_json(line.encode("utf-8"), f"{source} event", canonical=False)
        if (set(event) != required or event.get("event_schema_version") != 1 or
                event.get("run_id") != run_id or event.get("source_kind") != expected_kind or
                event.get("source_id") != source):
            _fail(f"{source} raw envelope drifted")
        instance = event.get("source_instance")
        sequence = event.get("source_sequence")
        timestamp = event.get("source_monotonic_ns")
        if not isinstance(instance, str) or not instance:
            _fail(f"{source} source instance is invalid")
        if source_instance is None:
            source_instance = instance
        elif source_instance != instance:
            _fail(f"{source} mixes source instances")
        if type(sequence) is not int or sequence <= prior_sequence:
            _fail(f"{source} sequence is not strictly increasing")
        if type(timestamp) is not int or timestamp < prior_ns:
            _fail(f"{source} CLOCK_MONOTONIC_RAW regressed")
        prior_sequence, prior_ns = sequence, timestamp
        result.append(event)
    return result


def _commit_metadata(event: Mapping[str, Any], *, authoritative: bool,
                     expected_epoch_number: int, expected_epoch_digest: str) -> tuple[tuple[int, str], tuple[Any, ...]]:
    payload = event.get("payload")
    fields = ({"block_height", "block_hash", "parent_hash", "transaction_count",
               "designated_observer", "decision_proof", "view_generation", "commit_batch_index"}
              if authoritative else
              {"block_height", "block_hash", "parent_hash", "transaction_count", "commit_batch_index"})
    if not isinstance(payload, Mapping) or set(payload) != fields:
        _fail("late commit event schema drifted")
    height = payload.get("block_height")
    block_hash = _hex(payload.get("block_hash"), 64, "late commit block hash")
    parent_hash = payload.get("parent_hash")
    if parent_hash is not None:
        _hex(parent_hash, 64, "late commit parent hash")
    transactions = payload.get("transaction_count")
    batch = payload.get("commit_batch_index")
    if (type(height) is not int or height <= 0 or type(transactions) is not int or transactions < 0 or
            type(batch) is not int or batch < 0):
        _fail("late commit counters are invalid")
    if authoritative:
        if payload.get("designated_observer") is not True:
            _fail("replica-2 authoritative commit is not designated")
        proof = payload.get("decision_proof")
        if (not isinstance(proof, Mapping) or
                set(proof) != {"epoch_number", "tree_id", "epoch_digest", "block_hash"} or
                type(proof.get("epoch_number")) is not int or type(proof.get("tree_id")) is not int or
                proof.get("block_hash") != block_hash):
            _fail("late authoritative decision proof drifted")
        if (proof.get("epoch_number") != expected_epoch_number or
                _hex(proof.get("epoch_digest"), 64, "late authoritative epoch digest") !=
                expected_epoch_digest):
            _fail("late authoritative commit does not bind the expected epoch digest")
    return (height, block_hash), (parent_hash, transactions)


def _late_common_commits(streams: Mapping[str, Sequence[Mapping[str, Any]]], *,
                         anchor_ns: int, expected_epoch_number: int,
                         expected_epoch_digest: str) -> list[dict[str, Any]]:
    start_ns = anchor_ns + LATE_START_NS
    end_ns = anchor_ns + LATE_END_NS
    authoritative: dict[tuple[int, str], tuple[tuple[Any, ...], int]] = {}
    observations: dict[tuple[int, str], dict[str, tuple[tuple[Any, ...], int]]] = {}
    observer = f"replica-{DESIGNATED_OBSERVER}"
    for source, events in streams.items():
        for event in events:
            timestamp = event["source_monotonic_ns"]
            if not start_ns <= timestamp < end_ns:
                continue
            if event["event_type"] == "block.committed":
                if source != observer:
                    continue
                key, metadata = _commit_metadata(
                    event, authoritative=True,
                    expected_epoch_number=expected_epoch_number,
                    expected_epoch_digest=expected_epoch_digest)
                if key in authoritative:
                    _fail("designated observer repeats a late authoritative commit")
                authoritative[key] = (metadata, timestamp)
            elif event["event_type"] == "block.commit_observed":
                key, metadata = _commit_metadata(
                    event, authoritative=False,
                    expected_epoch_number=expected_epoch_number,
                    expected_epoch_digest=expected_epoch_digest)
                by_source = observations.setdefault(key, {})
                if source in by_source:
                    _fail(f"{source} repeats a late commit observation")
                by_source[source] = (metadata, timestamp)
    expected_sources = set(streams)
    common: list[dict[str, Any]] = []
    for (height, block_hash), (metadata, designated_ns) in authoritative.items():
        witnesses = observations.get((height, block_hash), {})
        if set(witnesses) != expected_sources or any(value[0] != metadata for value in witnesses.values()):
            _fail("late authoritative commit lacks identical all-seven raw witnesses")
        completion_ns = max(designated_ns, *(value[1] for value in witnesses.values()))
        if completion_ns >= end_ns:
            _fail("late common commit completes outside the frozen window")
        common.append({"height": height, "block_hash": block_hash,
                       "designated_ns": designated_ns, "completion_ns": completion_ns})
    common.sort(key=lambda item: (item["designated_ns"], item["height"], item["block_hash"]))
    return common


def _late_fault_gate(streams: Mapping[str, Sequence[Mapping[str, Any]]], *,
                     anchor_ns: int, arm: str, expected_epoch_number: int,
                     expected_epoch_digest: str) -> int:
    start_ns = anchor_ns + LATE_START_NS
    end_ns = anchor_ns + LATE_END_NS
    count = 0
    for event in streams["replica-1"]:
        if event.get("event_type") != "fault.contribution_opportunity":
            continue
        timestamp = event["source_monotonic_ns"]
        if not start_ns <= timestamp < end_ns:
            continue
        payload = event.get("payload")
        proposal = payload.get("proposal") if isinstance(payload, Mapping) else None
        if (not isinstance(payload, Mapping) or not isinstance(proposal, Mapping) or
                payload.get("actor") != 1 or
                payload.get("fault_mode") != "role_scoped_persistent_selected_omission_v1"):
            continue
        if arm == ADAPTIVE_ARM:
            expected = (1, "leaf", "direct_vote", "omit_direct_vote")
        else:
            expected = (0, payload.get("physical_role"), payload.get("expected_message_type"),
                        payload.get("scheduled_action"))
            if expected[1:] not in {
                ("internal", "aggregate_relay", "omit_aggregate"),
                ("leaf", "direct_vote", "omit_direct_vote"),
            }:
                continue
        observed = (proposal.get("epoch_number"), payload.get("physical_role"),
                    payload.get("expected_message_type"), payload.get("scheduled_action"))
        if observed != expected:
            continue
        if (_hex(proposal.get("epoch_digest"), 64, "late fault proposal epoch digest") !=
                expected_epoch_digest):
            _fail(f"{arm} late physical omission does not bind the expected epoch digest")
        count += 1
    if count == 0:
        _fail(f"{arm} has no physical omission in the frozen late window")
    return count


def _adaptive_activation_gate(streams: Mapping[str, Sequence[Mapping[str, Any]]], *,
                              anchor_ns: int) -> dict[str, Any]:
    deadline = anchor_ns + LATE_START_NS
    identity: tuple[int, str, int] | None = None
    for replica in range(7):
        candidates: list[tuple[int, str, int]] = []
        for event in streams[f"replica-{replica}"]:
            if (event.get("event_type") != "epoch.activated" or
                    not anchor_ns <= event["source_monotonic_ns"] <= deadline):
                continue
            payload = event.get("payload")
            fields = {"epoch_number", "tree_id", "epoch_digest", "activation_height"}
            if (not isinstance(payload, Mapping) or set(payload) != fields or
                    payload.get("epoch_number") != 1 or type(payload.get("tree_id")) is not int or
                    payload["tree_id"] not in range(7) or type(payload.get("activation_height")) is not int or
                    payload["activation_height"] <= 0):
                continue
            candidates.append((payload["epoch_number"],
                               _hex(payload["epoch_digest"], 64, "adaptive activation digest"),
                               payload["activation_height"]))
        if len(candidates) != 1:
            _fail("adaptive cell lacks exactly one all-seven E1 activation by anchor plus 20 seconds")
        if identity is None:
            identity = candidates[0]
        elif candidates[0] != identity:
            _fail("adaptive all-seven E1 activation identity differs")
    if identity is None:
        _fail("adaptive cell has no common E1 activation identity")
    return {"epoch_number": identity[0], "epoch_digest": identity[1],
            "activation_height": identity[2]}


def _fixed_activation_gate(streams: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
    for events in streams.values():
        for event in events:
            payload = event.get("payload")
            if (event.get("event_type") == "epoch.activated" and isinstance(payload, Mapping) and
                    payload.get("epoch_number") == 1):
                _fail("fixed-E0 cell contains an Epoch-1 activation")


def _artifact_document(root: Path, artifacts: Mapping[str, Any], key: str,
                       label: str) -> dict[str, Any]:
    raw, _digest = _descriptor(root, artifacts.get(key), label, _MAX_SMALL)
    return _strict_json(raw, label, canonical=True)


def _main_config_invariants(raw: bytes, *, root: Path, label: str) -> dict[str, Any]:
    """Parse causal settings while allowing only regenerated identity material.

    Each materialized cell intentionally receives fresh BLS, TLS, and issuer
    keys and fresh loopback ports/absolute paths.  Those values are verified
    within that cell by the independent raw validator.  They must not be used
    as cross-cell equality keys.  Everything affecting tree shape, consensus,
    epoch timing, quorum progress, or message bounds remains exact here.
    """
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise CampaignEvaluationError(f"{label} is not ASCII") from exc
    if not lines or not raw.endswith(b"\n") or any(not line or " = " not in line for line in lines):
        _fail(f"{label} is not canonical key-value configuration")
    scalars: dict[str, str] = {}
    replicas: list[str] = []
    for line in lines:
        key, value = line.split(" = ", 1)
        if not key or not value or value.strip() != value:
            _fail(f"{label} contains a malformed option")
        if key == "replica":
            replicas.append(value)
        elif key in scalars:
            _fail(f"{label} repeats option {key}")
        else:
            scalars[key] = value
    expected_keys = set(_MAIN_CONFIG_FIXED) | _MAIN_CONFIG_DYNAMIC
    if set(scalars) != expected_keys or any(scalars[key] != value for key, value in _MAIN_CONFIG_FIXED.items()):
        _fail(f"{label} consensus or causal settings differ from frozen W19")
    if len(replicas) != 7:
        _fail(f"{label} does not contain exactly seven replicas")

    tree_path = Path(scalars["tree-generation-fpath"])
    expected_tree = (root / "config/epoch0.tree").resolve()
    if (not tree_path.is_absolute() or tree_path.resolve() != expected_tree or
            tree_path.is_symlink() or not tree_path.is_file()):
        _fail(f"{label} does not bind its cell-local Epoch-0 tree")
    issuer = scalars["epoch-change-issuer-public-key"]
    if not issuer or any(character.isspace() for character in issuer):
        _fail(f"{label} issuer public key is malformed")
    manager_match = re.fullmatch(r"127\.0\.0\.1:([0-9]+)", scalars["epoch-manager-address"])
    if manager_match is None or not 1024 < int(manager_match.group(1)) <= 65535:
        _fail(f"{label} manager endpoint is not bounded loopback")
    manager_certificate = scalars["epoch-manager-tls-cert"]
    if not manager_certificate or any(character.isspace() for character in manager_certificate):
        _fail(f"{label} manager TLS certificate identity is malformed")

    parsed: list[tuple[int, int, str, str]] = []
    for entry in replicas:
        match = _REPLICA_ENTRY.fullmatch(entry)
        if match is None:
            _fail(f"{label} replica entry is malformed")
        peer, client = int(match.group("peer")), int(match.group("client"))
        if not 1024 < peer <= 65535 or not 1024 < client <= 65535:
            _fail(f"{label} replica port is out of range")
        parsed.append((peer, client, match.group("bls"), match.group("tls")))
    peer_base, client_base = parsed[0][0], parsed[0][1]
    if ([item[0] for item in parsed] != list(range(peer_base, peer_base + 7)) or
            [item[1] for item in parsed] != list(range(client_base, client_base + 7)) or
            {item[0] for item in parsed} & {item[1] for item in parsed} or
            len({item[2] for item in parsed}) != 7 or len({item[3] for item in parsed}) != 7):
        _fail(f"{label} replica membership, ports, or identities are malformed")
    if int(manager_match.group(1)) in {item for row in parsed for item in row[:2]}:
        _fail(f"{label} manager port overlaps a replica endpoint")
    return {
        "fixed_consensus_options": dict(_MAIN_CONFIG_FIXED),
        "replica_count": 7,
        "replica_ids": list(range(7)),
        "peer_port_offsets": list(range(7)),
        "client_port_offsets": list(range(7)),
        "network_scope": "loopback-per-cell-dynamic-ports",
        "tree_binding": "cell-local-config/epoch0.tree",
        "allowed_per_cell_differences": [
            "BLS public keys", "TLS certificate identities",
            "epoch issuer public key", "loopback base ports",
            "absolute cell-local tree path",
        ],
    }


def _fault_schedule_invariants(plan: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    schedule = plan.get("native_fault_schedule")
    values = schedule.get("values") if isinstance(schedule, Mapping) else None
    protocol = values.get("protocol") if isinstance(values, Mapping) else None
    fault = values.get("fault") if isinstance(values, Mapping) else None
    expected_values = {"schema_version", "profile_id", "status", "claim_boundary", "protocol", "fault"}
    expected_fault = {
        "native_mode", "actor_id", "hard_actor_count", "responsive_degraded_actor_count",
        "responsive_omission_period", "max_omissions_per_proposal", "context_limit",
        "window_start_monotonic_ns", "window_end_monotonic_ns", "common_horizon_ns",
        "minimum_post_start_anchor_slack_ns", "argv_overlay",
    }
    phase_field = "first_omission_tree"
    if (not isinstance(values, Mapping) or set(values) != expected_values or
            values.get("schema_version") != 1 or
            values.get("profile_id") != "n7-role-scoped-persistent-selected-omission-v1" or
            values.get("status") != "PREFLIGHT_ONLY_NO_EXECUTION" or
            protocol != {"replica_ids": list(range(7)), "fault_threshold": 2, "quorum": 5} or
            not isinstance(fault, Mapping) or
            set(fault) not in (expected_fault, expected_fault | {phase_field})):
        _fail(f"{label} native fault schedule schema drifted")
    start, end = fault.get("window_start_monotonic_ns"), fault.get("window_end_monotonic_ns")
    fixed = {
        "native_mode": "role_scoped_persistent_selected_omission_v1",
        "actor_id": 1,
        "hard_actor_count": 1,
        "responsive_degraded_actor_count": 0,
        "responsive_omission_period": 0,
        "max_omissions_per_proposal": 1,
        "context_limit": 100_000,
        "common_horizon_ns": LATE_END_NS,
        "minimum_post_start_anchor_slack_ns": 10_000_000_000,
    }
    if (type(start) is not int or type(end) is not int or end <= start or
            any(fault.get(key) != value for key, value in fixed.items())):
        _fail(f"{label} native fault policy differs from frozen W19")
    first_omission_tree = fault.get(phase_field)
    if first_omission_tree is not None and first_omission_tree != 4:
        _fail(f"{label} first omission tree differs from frozen tree 4")
    expected_overlay = [
        "--experiment-byzantine-mode", fixed["native_mode"],
        "--experiment-byzantine-window", values["profile_id"],
        "--experiment-rotating-omission-actors", "1",
        "--experiment-byzantine-window-start-monotonic-ns", str(start),
        "--experiment-byzantine-window-end-monotonic-ns", str(end),
        "--experiment-byzantine-max-omissions-per-proposal", "1",
        "--experiment-rotating-omission-context-limit", "100000",
    ]
    if first_omission_tree is not None:
        expected_overlay.extend(("--experiment-byzantine-first-omission-tree", "4"))
    if fault.get("argv_overlay") != expected_overlay:
        _fail(f"{label} native fault argv differs from its declared window")
    return {**fixed, "first_omission_tree": first_omission_tree,
            "fault_schedule_duration_ns": end - start}


def _one_cell(record: Mapping[str, Any], *, expected_pair: int, expected_ordinal: int,
              expected_arm: str, freeze: Mapping[str, Any], frozen_at: datetime) -> dict[str, Any]:
    required = {"pair_index", "ordinal", "arm", "root", "receipt_path", "receipt_sha256"}
    if set(record) != required:
        _fail(f"cell {expected_ordinal} record schema drifted")
    if (record.get("pair_index"), record.get("ordinal"), record.get("arm")) != (
            expected_pair, expected_ordinal, expected_arm):
        _fail(f"cell {expected_ordinal} contradicts the frozen AB/BA schedule")
    root_value = record.get("root")
    if not isinstance(root_value, str) or not root_value:
        _fail(f"cell {expected_ordinal} root is invalid")
    supplied_root = Path(root_value)
    if supplied_root.is_symlink():
        _fail(f"cell {expected_ordinal} root must not be a symlink")
    root = supplied_root.resolve()
    if not root.is_dir():
        _fail(f"cell {expected_ordinal} root is not a regular directory")
    receipt_relative = Path(str(record.get("receipt_path")))
    receipt_path = _safe_child(root, record.get("receipt_path"), f"cell {expected_ordinal} receipt")
    receipt_raw = _read(receipt_path, f"cell {expected_ordinal} receipt", _MAX_SMALL)
    if _sha(receipt_raw) != _hex(record.get("receipt_sha256"), 64, f"cell {expected_ordinal} receipt pin"):
        _fail(f"cell {expected_ordinal} receipt differs from its campaign pin")
    receipt = _strict_json(receipt_raw, f"cell {expected_ordinal} receipt", canonical=True)
    if (receipt.get("kind") != RECEIPT_KIND or receipt.get("arm") != expected_arm or
            receipt.get("state") != "SEALED_RAW_BUNDLE_NO_CLAIM"):
        _fail(f"cell {expected_ordinal} is not a sealed {expected_arm} raw receipt")

    accepted = _validate_accepted_bundle(root, receipt_relative)
    if (accepted.get("arm") != expected_arm or accepted.get("run_id") != receipt.get("run_id") or
            accepted.get("plan_sha256") != receipt.get("plan_sha256") or
            accepted.get("anchor_monotonic_ns") != receipt.get("anchor", {}).get("monotonic_ns")):
        _fail(f"cell {expected_ordinal} accepted verdict differs from its receipt")

    artifacts = receipt.get("artifacts")
    binding = receipt.get("launch_binding")
    if not isinstance(artifacts, Mapping) or not isinstance(binding, Mapping):
        _fail(f"cell {expected_ordinal} receipt lacks artifacts or launch binding")
    event_descriptors = artifacts.get("replica_events")
    if not isinstance(event_descriptors, list) or len(event_descriptors) != 7:
        _fail(f"cell {expected_ordinal} lacks seven receipt-pinned replica streams")
    streams: dict[str, list[dict[str, Any]]] = {}
    for replica, descriptor in enumerate(event_descriptors):
        source = f"replica-{replica}"
        raw, _digest = _descriptor(root, descriptor, f"cell {expected_ordinal} {source} events", _MAX_RAW)
        streams[source] = _events(raw, run_id=receipt["run_id"], source=source)
    anchor = receipt.get("anchor")
    if not isinstance(anchor, Mapping) or type(anchor.get("monotonic_ns")) is not int:
        _fail(f"cell {expected_ordinal} receipt anchor is invalid")
    anchor_ns = anchor["monotonic_ns"]
    e0_digest = _hex(binding.get("e0_digest"), 64, f"cell {expected_ordinal} Epoch-0 digest")
    activation = None
    if expected_arm == ADAPTIVE_ARM:
        activation = _adaptive_activation_gate(streams, anchor_ns=anchor_ns)
        expected_epoch_number = activation["epoch_number"]
        expected_epoch_digest = activation["epoch_digest"]
    else:
        _fixed_activation_gate(streams)
        expected_epoch_number = 0
        expected_epoch_digest = e0_digest
    late_fault_count = _late_fault_gate(
        streams, anchor_ns=anchor_ns, arm=expected_arm,
        expected_epoch_number=expected_epoch_number,
        expected_epoch_digest=expected_epoch_digest)
    commits = _late_common_commits(
        streams, anchor_ns=anchor_ns,
        expected_epoch_number=expected_epoch_number,
        expected_epoch_digest=expected_epoch_digest)

    plan = _artifact_document(root, artifacts, "execution_plan", f"cell {expected_ordinal} plan")
    request = _artifact_document(root, artifacts, "authorization_request", f"cell {expected_ordinal} request")
    approval = _artifact_document(root, artifacts, "approved_authorization", f"cell {expected_ordinal} approval")
    if plan.get("repository_revision") != freeze["repository_revision"]:
        _fail(f"cell {expected_ordinal} repository revision differs from campaign freeze")
    if approval.get("approval_reference") != freeze["campaign_approval_reference"]:
        _fail(f"cell {expected_ordinal} is not covered by the frozen campaign approval")
    if _timestamp(approval.get("approved_utc"), f"cell {expected_ordinal} approval timestamp") <= frozen_at:
        _fail(f"cell {expected_ordinal} approval does not postdate the campaign freeze")
    if (request.get("no_retry") is not True or request.get("claim_eligible") is not False or
            request.get("figure_eligible") is not False or request.get("arm") != expected_arm):
        _fail(f"cell {expected_ordinal} request is not a no-retry no-claim cell")
    scheduled = binding.get("scheduled_window")
    if (not isinstance(scheduled, Mapping) or type(scheduled.get("start_monotonic_ns")) is not int or
            type(scheduled.get("end_monotonic_ns")) is not int or
            scheduled["start_monotonic_ns"] >= scheduled["end_monotonic_ns"]):
        _fail(f"cell {expected_ordinal} scheduled window is invalid")
    if not scheduled["start_monotonic_ns"] <= anchor_ns < scheduled["end_monotonic_ns"]:
        _fail(f"cell {expected_ordinal} anchor is outside its scheduled window")
    executables = artifacts.get("executables")
    if not isinstance(executables, Mapping) or set(executables) != {"hotstuff_app", "adaptation_manager"}:
        _fail(f"cell {expected_ordinal} executable descriptors drifted")
    executable_sha = {
        name: _descriptor(root, descriptor, f"cell {expected_ordinal} {name}", _MAX_RAW)[1]
        for name, descriptor in executables.items()
    }
    profile_sha = _descriptor(root, artifacts.get("profile"), f"cell {expected_ordinal} profile", _MAX_SMALL)[1]
    tree_sha = _descriptor(root, artifacts.get("epoch0_tree"), f"cell {expected_ordinal} Epoch-0 tree", _MAX_SMALL)[1]
    main_raw, _main_sha = _descriptor(
        root, artifacts.get("main_config"), f"cell {expected_ordinal} main config", _MAX_SMALL)
    config_invariants = _main_config_invariants(
        main_raw, root=root, label=f"cell {expected_ordinal} main config")
    fault_invariants = _fault_schedule_invariants(
        plan, label=f"cell {expected_ordinal} plan")
    scheduled_duration = scheduled["end_monotonic_ns"] - scheduled["start_monotonic_ns"]
    if fault_invariants["fault_schedule_duration_ns"] != scheduled_duration:
        _fail(f"cell {expected_ordinal} fault schedule duration differs from launch window")
    hard_timeout = request.get("hard_timeout_seconds")
    if type(hard_timeout) is not int or hard_timeout < 120:
        _fail(f"cell {expected_ordinal} hard timeout is invalid")
    if plan.get("hard_timeout_seconds") != hard_timeout or plan.get("scheduled_window") != scheduled:
        _fail(f"cell {expected_ordinal} plan timing differs from its accepted launch binding")
    return {
        "pair_index": expected_pair,
        "ordinal": expected_ordinal,
        "arm": expected_arm,
        "root": str(root),
        "receipt_sha256": _sha(receipt_raw),
        "run_id": receipt["run_id"],
        "plan_sha256": receipt["plan_sha256"],
        "anchor_monotonic_ns": anchor_ns,
        "late_window": {"start_monotonic_ns": anchor_ns + LATE_START_NS,
                        "end_monotonic_ns": anchor_ns + LATE_END_NS},
        "late_fault_opportunity_count": late_fault_count,
        "late_common_commit_count": len(commits),
        "late_common_commits": commits,
        "adaptive_activation": activation,
        "scheduled_window": dict(scheduled),
        "hard_timeout_seconds": hard_timeout,
        "identity": {
            "repository_revision": plan["repository_revision"],
            "e0_digest": binding.get("e0_digest"),
            "native_profile_sha256": binding.get("native_profile_sha256"),
            "selection_profile_sha256": binding.get("selection_profile_sha256"),
            "profile_sha256": profile_sha,
            "epoch0_tree_sha256": tree_sha,
            "normalized_main_config": config_invariants,
            "fault_schedule": fault_invariants,
            "executable_sha256": executable_sha,
            "scheduled_window_duration_ns": scheduled_duration,
            "hard_timeout_seconds": hard_timeout,
        },
    }


def _pair(pair_index: int, first: Mapping[str, Any], second: Mapping[str, Any]) -> dict[str, Any]:
    arms = {first["arm"]: first, second["arm"]: second}
    if set(arms) != {FIXED_ARM, ADAPTIVE_ARM}:
        _fail(f"pair {pair_index} does not contain one fixed and one adaptive arm")
    fixed = arms[FIXED_ARM]["late_common_commit_count"]
    adaptive = arms[ADAPTIVE_ARM]["late_common_commit_count"]
    difference = adaptive - fixed
    ratio = None if fixed == 0 else {"numerator": adaptive, "denominator": fixed}
    classification = "positive" if difference > 0 else "negative" if difference < 0 else "neutral"
    return {
        "pair_index": pair_index,
        "order": [first["arm"], second["arm"]],
        "fixed_count": fixed,
        "adaptive_count": adaptive,
        "adaptive_minus_fixed": difference,
        "adaptive_to_fixed_ratio": ratio,
        "classification": classification,
        "zero_denominator_outcome": (
            "adaptive-progress-fixed-zero" if fixed == 0 and adaptive > 0 else
            "both-zero" if fixed == 0 else "not-applicable"
        ),
        "cells": [dict(first), dict(second)],
    }


def evaluate_campaign(freeze: Mapping[str, Any], cells: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Validate all 12 frozen cells and compute the predeclared campaign gate."""
    frozen_at = _check_freeze(freeze)
    if isinstance(cells, (str, bytes, Mapping)) or len(cells) != PAIR_COUNT * 2:
        _fail("campaign requires exactly 12 ordered cells")
    validated: list[dict[str, Any]] = []
    ordinal = 0
    for pair_index, order in enumerate(FROZEN_PAIR_SCHEDULE, start=1):
        for arm in order:
            ordinal += 1
            record = cells[ordinal - 1]
            if not isinstance(record, Mapping):
                _fail(f"cell {ordinal} is not an object")
            validated.append(_one_cell(record, expected_pair=pair_index,
                                       expected_ordinal=ordinal, expected_arm=arm,
                                       freeze=freeze, frozen_at=frozen_at))

    if len({cell["root"] for cell in validated}) != len(validated):
        _fail("campaign reuses a result root")
    if len({cell["receipt_sha256"] for cell in validated}) != len(validated):
        _fail("campaign reuses a receipt, including an excluded pilot")
    if len({cell["run_id"] for cell in validated}) != len(validated):
        _fail("campaign reuses a run ID")
    common_identity = validated[0]["identity"]
    for cell in validated[1:]:
        if cell["identity"] != common_identity:
            _fail("campaign cells are not strictly comparable")
    for previous, current in zip(validated, validated[1:]):
        if current["scheduled_window"]["start_monotonic_ns"] < previous["scheduled_window"]["end_monotonic_ns"]:
            _fail("campaign cells overlap or contradict the frozen execution order")

    pairs = [_pair(index, validated[(index - 1) * 2], validated[(index - 1) * 2 + 1])
             for index in range(1, PAIR_COUNT + 1)]
    positives = [pair["classification"] == "positive" for pair in pairs]
    fixed_total = sum(pair["fixed_count"] for pair in pairs)
    adaptive_total = sum(pair["adaptive_count"] for pair in pairs)
    forward_positive = sum(positives[::2])
    reverse_positive = sum(positives[1::2])
    aggregate_gate = (fixed_total > 0 and adaptive_total > 0 and
                      adaptive_total * FROZEN_DESIGN["improvement_gate"]["aggregate_ratio_denominator"] >=
                      fixed_total * FROZEN_DESIGN["improvement_gate"]["aggregate_ratio_numerator"])
    direction_gate = (sum(positives) >= 5 and forward_positive >= 2 and
                      reverse_positive >= 2)
    technical_gate = direction_gate and aggregate_gate
    aggregate_ratio = None if fixed_total == 0 else Fraction(adaptive_total, fixed_total)
    return {
        "schema_version": 1,
        "kind": RESULT_KIND,
        "verdict": "CAMPAIGN_COMPLETE_NO_AUTOMATIC_CLAIM",
        "campaign_id": freeze["campaign_id"],
        "freeze_sha256": freeze["freeze_sha256"],
        "design_sha256": freeze["design_sha256"],
        "scenario_scope": SCENARIO_SCOPE,
        "pairs": pairs,
        "aggregate": {
            "fixed_count": fixed_total,
            "adaptive_count": adaptive_total,
            "adaptive_minus_fixed": adaptive_total - fixed_total,
            "adaptive_to_fixed_ratio": (None if aggregate_ratio is None else
                                          {"numerator": aggregate_ratio.numerator,
                                           "denominator": aggregate_ratio.denominator}),
        },
        "direction_gate": {
            "positive_pair_count": sum(positives),
            "forward_positive_count": forward_positive,
            "reverse_positive_count": reverse_positive,
            "required_positive_pair_count": 5,
            "required_positive_per_order": 2,
            "passed": direction_gate,
        },
        "aggregate_ratio_gate": {
            "required_numerator": 11,
            "required_denominator": 10,
            "passed": aggregate_gate,
        },
        "technical_improvement_gate_passed": technical_gate,
        "thesis_integration_candidate": technical_gate,
        "claim_eligible": False,
        "figure_eligible": False,
        "claim_boundary": FROZEN_DESIGN["claim_scope"],
        "pilot_policy": FROZEN_DESIGN["pilot_policy"],
        "comparability_identity": common_identity,
    }


def evaluate_manifest_bound_campaign(
        freeze: Mapping[str, Any], manifest: Mapping[str, Any], campaign_root: Path,
        cells: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Evaluate exactly the twelve cells committed in a pre-run manifest.

    ``evaluate_campaign`` remains for v1 artifact inspection.  This v2 entry
    point is the only final campaign verdict: it binds each raw-bundle receipt
    to the immutable manifest root, ordered cell identity, and staged target
    host evidence before replaying the existing independent raw validator.
    """
    _check_freeze(freeze)
    if not isinstance(manifest, Mapping):
        _fail("campaign manifest is not an object")
    expected_cells = _check_campaign_manifest(freeze, manifest)
    root = _reject_lexical_symlink_ancestors(Path(campaign_root), "campaign root").resolve()
    if not root.is_dir():
        _fail("campaign root is not a regular directory")
    if isinstance(cells, (str, bytes, Mapping)) or len(cells) != len(expected_cells):
        _fail("manifest-bound campaign requires exactly twelve ordered cells")
    raw_records: list[dict[str, Any]] = []
    boot_ids: set[str] = set()
    for expected, record in zip(expected_cells, cells):
        required = {"pair_index", "ordinal", "arm", "root", "receipt_path", "receipt_sha256",
                    "stage_receipt_path", "stage_receipt_sha256"}
        if not isinstance(record, Mapping) or set(record) != required:
            _fail(f"cell {expected['ordinal']} manifest-bound record schema drifted")
        if (record.get("pair_index"), record.get("ordinal"), record.get("arm")) != (
                expected["pair_index"], expected["ordinal"], expected["arm"]):
            _fail(f"cell {expected['ordinal']} contradicts the manifest cell identity")
        expected_root = _safe_child(root, expected["run_root"],
                                    f"cell {expected['ordinal']} manifest root")
        try:
            expected_root.relative_to(root)
        except ValueError as exc:
            raise CampaignEvaluationError("manifest cell root escapes campaign root") from exc
        supplied_root = record.get("root")
        if not isinstance(supplied_root, str) or supplied_root != str(expected_root):
            _fail(f"cell {expected['ordinal']} root is not the manifest-pinned root")
        _reject_lexical_symlink_ancestors(Path(supplied_root), f"cell {expected['ordinal']} supplied root")
        if not expected_root.is_dir():
            _fail(f"cell {expected['ordinal']} manifest-pinned root is unavailable")
        _reject_negative_markers(expected_root, ordinal=expected["ordinal"])
        staged_request_sha = _stage_receipt(expected_root, record, expected, manifest, freeze)
        stage_raw = _read(_safe_child(expected_root, record.get("stage_receipt_path"),
                                     f"cell {expected['ordinal']} stage receipt"),
                          f"cell {expected['ordinal']} stage receipt", _MAX_SMALL)
        if _sha(stage_raw) != record.get("stage_receipt_sha256"):
            _fail(f"cell {expected['ordinal']} stage receipt changed after validation")
        stage = _strict_json(stage_raw, f"cell {expected['ordinal']} stage receipt", canonical=True)
        boot_ids.add(stage["host_identity"]["linux_boot_id"])
        receipt_raw = _read(_safe_child(expected_root, record.get("receipt_path"),
                                        f"cell {expected['ordinal']} receipt"),
                            f"cell {expected['ordinal']} receipt", _MAX_SMALL)
        if _sha(receipt_raw) != _hex(record.get("receipt_sha256"), 64,
                                     f"cell {expected['ordinal']} receipt pin"):
            _fail(f"cell {expected['ordinal']} receipt differs from its campaign pin")
        receipt = _strict_json(receipt_raw, f"cell {expected['ordinal']} receipt", canonical=True)
        artifacts = receipt.get("artifacts")
        if not isinstance(artifacts, Mapping):
            _fail(f"cell {expected['ordinal']} receipt lacks artifacts")
        _request_raw, request_sha = _descriptor(
            expected_root, artifacts.get("authorization_request"),
            f"cell {expected['ordinal']} authorization request", _MAX_SMALL)
        if request_sha != staged_request_sha:
            _fail(f"cell {expected['ordinal']} raw receipt request differs from staged request")
        raw_records.append({key: record[key] for key in
                            ("pair_index", "ordinal", "arm", "root", "receipt_path", "receipt_sha256")})
    if len(boot_ids) != 1:
        _fail("campaign cells span more than one Linux boot and RAW clock domain")
    result = evaluate_campaign(freeze, raw_records)
    return {
        **result,
        "schema_version": 2,
        "kind": MANIFEST_BOUND_RESULT_KIND,
        "manifest_kind": MANIFEST_KIND,
        "manifest_sha256": manifest["manifest_sha256"],
        "target_host": expected_cells[0]["target_host"],
        "linux_boot_id": next(iter(boot_ids)),
        "claim_eligible": False,
        "figure_eligible": False,
    }
