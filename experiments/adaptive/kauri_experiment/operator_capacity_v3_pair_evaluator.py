"""Fail-closed descriptive evaluator for one prospective W18 matched pair.

This is deliberately *after* per-arm raw validation.  It reopens the raw
files named by each independent authority so a caller cannot substitute a
favourable count after validation.  A complete pair is still only one
descriptive observation: campaign acceptance is intentionally outside this
module.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Callable, Mapping, Sequence


N = 31
WINDOW_NS = 30 * 1_000_000_000
_AUTHORITY_KIND = "kauri-n31-operator-capacity-v3-raw-validation-authority-v1"
_RAW_KIND = "kauri-n31-operator-capacity-v3-raw-validation-v1"
_MANIFEST_KIND = "kauri-n31-operator-capacity-v3-matched-pair-manifest-v1"
_RESULT_KIND = "kauri-n31-operator-capacity-v3-matched-pair-result-v1"
_HEX = frozenset("0123456789abcdef")


class PairEvaluationError(ValueError):
    """An arm does not provide a complete, independently pinned raw result."""


RawRevalidator = Callable[[Path, Path], Mapping[str, Any]]


def _fail(message: str) -> None:
    raise PairEvaluationError(message)


def _read(path: Path, label: str, maximum: int = 16 * 1024 * 1024) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise PairEvaluationError(f"{label} is not a readable regular file") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > maximum:
            _fail(f"{label} is not a bounded regular file")
        result = b""
        while len(result) < before.st_size:
            chunk = os.read(fd, before.st_size - len(result))
            if not chunk:
                _fail(f"{label} changed during read")
            result += chunk
        after = os.fstat(fd)
        stable_identity = lambda value: (
            value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
            value.st_uid, value.st_gid, value.st_size, value.st_mtime_ns,
            value.st_ctime_ns,
        )
        if os.read(fd, 1) or stable_identity(after) != stable_identity(before):
            _fail(f"{label} changed during read")
        return result
    finally:
        os.close(fd)


def _pairs(label: str):
    def decode(items: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                _fail(f"{label} repeats JSON field {key}")
            result[key] = value
        return result
    return decode


def _document(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    raw = _read(path, label)
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_pairs(label),
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PairEvaluationError(f"{label} is not strict ASCII JSON") from exc
    if not isinstance(value, dict):
        _fail(f"{label} is not an object")
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True, allow_nan=False).encode("ascii") + b"\n"
    if raw != canonical:
        _fail(f"{label} is not canonical")
    return value, raw


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _hex64(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(item not in _HEX for item in value):
        _fail(f"{label} is not lower-case SHA-256")
    return value


def _outside(path: Path, root: Path, label: str) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return
    _fail(f"{label} must be retained outside its runner-owned root")


def _artifact_hashes(root: Path, authority: Mapping[str, Any]) -> dict[str, str]:
    """Reopen every raw file pin supplied by the independent authority."""
    expected_streams = {"manager", *(f"replica-{item}" for item in range(N))}
    streams = authority.get("event_stream_sha256")
    if not isinstance(streams, Mapping) or set(streams) != expected_streams:
        _fail("authority does not pin manager plus 31 replica streams")
    paths: dict[str, tuple[Path, str]] = {
        "materialization_manifest_sha256": (root / "materialization-manifest.json", "materialization manifest"),
        "runner_receipt_sha256": (root / "runtime/local-shakedown-receipt.json", "runner receipt"),
        "cpu_quota_frozen_contract_sha256": (root / "runtime/frozen-cpu-quota-contract.json", "frozen CPU contract"),
        "cpu_quota_contract_sha256": (root / "runtime/cpu-quota-contract.json", "CPU contract"),
        "cpu_quota_launch_sha256": (root / "runtime/cpu-quota-launch.json", "CPU launch"),
        "cpu_quota_samples_sha256": (root / "raw/cpu-quota-samples.jsonl", "CPU samples"),
        "cpu_quota_rounds_sha256": (root / "raw/cpu-quota-monitor-rounds.jsonl", "CPU monitor rounds"),
    }
    result: dict[str, str] = {}
    for key, (path, label) in paths.items():
        pinned = _hex64(authority.get(key), f"authority {key}")
        observed = _sha(_read(path, label))
        if observed != pinned:
            _fail(f"{label} differs from external authority pin")
        result[key] = observed
    for source in sorted(expected_streams):
        path = root / ("raw/manager-events.jsonl" if source == "manager" else f"raw/{source}.jsonl")
        pinned = _hex64(streams[source], f"authority {source} stream")
        observed = _sha(_read(path, f"{source} stream"))
        if observed != pinned:
            _fail(f"{source} stream differs from external authority pin")
        result[f"stream:{source}"] = observed
    return result


def _frozen_cpu_identity(root: Path) -> dict[str, str]:
    contract, _raw = _document(root / "runtime/frozen-cpu-quota-contract.json", "frozen CPU contract")
    required = {"base_profile_id", "base_profile_sha256", "base_profile_canonical_sha256"}
    if not required.issubset(contract):
        _fail("frozen CPU contract does not identify its base profile")
    profile_id = contract["base_profile_id"]
    if not isinstance(profile_id, str) or not profile_id:
        _fail("frozen CPU contract base profile ID is invalid")
    return {"base_profile_id": profile_id,
            "base_profile_sha256": _hex64(contract["base_profile_sha256"], "base profile SHA-256"),
            "base_profile_canonical_sha256": _hex64(
                contract["base_profile_canonical_sha256"], "canonical base profile SHA-256")}


def write_canonical_raw_validation_result(
    *, root: Path, authority_path: Path, output_path: Path, revalidate: RawRevalidator,
) -> dict[str, Any]:
    """Persist one independently recomputed raw result as canonical external input.

    This writer performs no acceptance and must not be used as a substitute for
    the later recomputation in :func:`evaluate_matched_pair`.
    """
    root = Path(root).resolve()
    output_path = Path(output_path).resolve()
    _outside(output_path, root, "raw validation output")
    if output_path.exists() or output_path.is_symlink() or not output_path.parent.is_dir():
        _fail("raw validation output must be a fresh file below an existing external directory")
    try:
        result = revalidate(root, Path(authority_path).resolve())
    except Exception as exc:
        raise PairEvaluationError("independent raw-validator recomputation failed") from exc
    if not isinstance(result, Mapping) or result.get("kind") != _RAW_KIND or result.get("verdict") != "COMPLETE_NO_CLAIM":
        _fail("independent raw-validator did not produce COMPLETE_NO_CLAIM")
    payload = dict(result)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                     allow_nan=False).encode("ascii") + b"\n"
    try:
        fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise PairEvaluationError("cannot create external raw validation output") from exc
    try:
        offset = 0
        while offset < len(raw):
            count = os.write(fd, raw[offset:])
            if count <= 0:
                _fail("cannot write external raw validation output")
            offset += count
        os.fsync(fd)
    finally:
        os.close(fd)
    return payload


def _arm(
    *, arm_name: str, root: Path, raw_validation_path: Path, authority_path: Path,
    expected_raw_validation_sha256: str, expected_authority_sha256: str,
    revalidate: RawRevalidator,
) -> dict[str, Any]:
    root = root.resolve()
    _outside(raw_validation_path, root, f"{arm_name} raw validation")
    _outside(authority_path, root, f"{arm_name} raw authority")
    raw, raw_bytes = _document(raw_validation_path, f"{arm_name} raw validation")
    authority, authority_bytes = _document(authority_path, f"{arm_name} raw authority")
    if _sha(raw_bytes) != _hex64(expected_raw_validation_sha256, f"{arm_name} expected raw validation hash"):
        _fail(f"{arm_name} raw validation differs from manifest pin")
    if _sha(authority_bytes) != _hex64(expected_authority_sha256, f"{arm_name} expected authority hash"):
        _fail(f"{arm_name} raw authority differs from manifest pin")
    if raw.get("kind") != _RAW_KIND or raw.get("verdict") != "COMPLETE_NO_CLAIM":
        _fail(f"{arm_name} raw validation is not COMPLETE_NO_CLAIM")
    if raw.get("claim_eligible") is not False or raw.get("figure_eligible") is not False:
        _fail(f"{arm_name} raw validation is not claim-ineligible")
    if authority.get("kind") != _AUTHORITY_KIND or authority.get("schema_version") != 1:
        _fail(f"{arm_name} raw authority schema differs")
    pins = authority.get("pins")
    if not isinstance(pins, Mapping):
        _fail(f"{arm_name} raw authority pins are absent")
    if pins.get("arm") != arm_name:
        _fail(f"{arm_name} raw authority arm differs")
    window = raw.get("measurement_window")
    if (not isinstance(window, Mapping) or set(window) != {"start_monotonic_ns", "end_monotonic_ns"} or
            type(window["start_monotonic_ns"]) is not int or type(window["end_monotonic_ns"]) is not int or
            window["end_monotonic_ns"] - window["start_monotonic_ns"] != WINDOW_NS):
        _fail(f"{arm_name} raw validation lacks an exact 30-second E1 window")
    count = raw.get("complete_common_commit_count")
    commits = raw.get("common_commits")
    if type(count) is not int or count < 0 or not isinstance(commits, list) or len(commits) != count:
        _fail(f"{arm_name} raw validation commit count does not bind its common commits")
    for item in commits:
        if not isinstance(item, Mapping) or set(item) != {"height", "block_hash", "designated_ns", "completion_ns"}:
            _fail(f"{arm_name} common commit schema differs")
        if (type(item["height"]) is not int or item["height"] <= 0 or
                type(item["designated_ns"]) is not int or type(item["completion_ns"]) is not int or
                not window["start_monotonic_ns"] <= item["designated_ns"] < window["end_monotonic_ns"] or
                not item["designated_ns"] <= item["completion_ns"] < window["end_monotonic_ns"]):
            _fail(f"{arm_name} common commit lies outside its E1 window")
        _hex64(item["block_hash"], f"{arm_name} common commit hash")
    chain = raw.get("consumption_chain")
    if not isinstance(chain, Mapping) or chain.get("arm") != arm_name:
        _fail(f"{arm_name} raw validation does not bind its consumed arm")
    try:
        recomputed = revalidate(root, authority_path.resolve())
    except Exception as exc:
        raise PairEvaluationError(f"{arm_name} independent raw-validator recomputation failed") from exc
    if not isinstance(recomputed, Mapping) or dict(recomputed) != raw:
        _fail(f"{arm_name} persisted raw validation differs from independent recomputation")
    artifacts = _artifact_hashes(root, authority)
    required_pins = ("source_revision", "epoch0_consensus_digest", "epoch0_topology_digest",
                     "approved_capacity_digest", "run_id")
    for key in required_pins:
        if key not in pins or not isinstance(pins[key], str) or not pins[key]:
            _fail(f"{arm_name} authority pin {key} is absent")
    return {"count": count, "window": dict(window), "pins": dict(pins),
            "raw_validation_sha256": _sha(raw_bytes), "authority_sha256": _sha(authority_bytes),
            "artifacts": artifacts, "stage_a_wire_sha256": chain.get("stage_a_wire_sha256"),
            "stage_b_authorization_wire_sha256": chain.get("stage_b_authorization_wire_sha256"),
            "frozen_cpu_identity": _frozen_cpu_identity(root)}


def evaluate_matched_pair(
    manifest: Mapping[str, Any], *, sham_root: Path, treatment_root: Path,
    sham_raw_validation: Path, treatment_raw_validation: Path,
    sham_authority: Path, treatment_authority: Path,
    revalidate: RawRevalidator,
) -> dict[str, Any]:
    """Evaluate one exact sham/treatment pair without promoting a thesis claim."""
    required = {"schema_version", "kind", "pair_id", "measurement", "effect_statistic",
                "threshold_numerator", "threshold_denominator", "arms"}
    if not isinstance(manifest, Mapping) or set(manifest) != required:
        _fail("pair manifest schema differs")
    if manifest.get("schema_version") != 1 or manifest.get("kind") != _MANIFEST_KIND:
        _fail("pair manifest identity differs")
    if not isinstance(manifest.get("pair_id"), str) or not manifest["pair_id"]:
        _fail("pair manifest ID is absent")
    if manifest.get("measurement") != {"epoch": 1, "window_ns": WINDOW_NS, "commit_metric": "all-31-common-commits-v1"}:
        _fail("pair manifest does not freeze the all-31 30-second E1 metric")
    if manifest.get("effect_statistic") != "treatment_to_sham_common_commit_ratio_v1":
        _fail("pair manifest effect statistic differs")
    numerator, denominator = manifest.get("threshold_numerator"), manifest.get("threshold_denominator")
    if type(numerator) is not int or type(denominator) is not int or numerator <= 0 or denominator <= 0:
        _fail("pair manifest practical threshold is invalid")
    arms = manifest.get("arms")
    if not isinstance(arms, Mapping) or set(arms) != {"sham", "treatment"}:
        _fail("pair manifest must pin sham and treatment")
    for name in ("sham", "treatment"):
        if not isinstance(arms[name], Mapping) or set(arms[name]) != {"raw_validation_sha256", "authority_sha256"}:
            _fail(f"pair manifest {name} pins differ")
    sham = _arm(arm_name="sham", root=Path(sham_root), raw_validation_path=Path(sham_raw_validation),
                authority_path=Path(sham_authority), **{
                    "expected_raw_validation_sha256": arms["sham"]["raw_validation_sha256"],
                    "expected_authority_sha256": arms["sham"]["authority_sha256"],
                    "revalidate": revalidate,
                })
    treatment = _arm(arm_name="treatment", root=Path(treatment_root), raw_validation_path=Path(treatment_raw_validation),
                     authority_path=Path(treatment_authority), **{
                         "expected_raw_validation_sha256": arms["treatment"]["raw_validation_sha256"],
                         "expected_authority_sha256": arms["treatment"]["authority_sha256"],
                         "revalidate": revalidate,
                     })
    for key in ("source_revision", "epoch0_consensus_digest", "epoch0_topology_digest", "approved_capacity_digest"):
        if sham["pins"][key] != treatment["pins"][key]:
            _fail(f"arms do not share pinned {key}")
    if sham["stage_a_wire_sha256"] == treatment["stage_a_wire_sha256"]:
        _fail("arms reuse one Stage-A authority wire")
    if sham["artifacts"]["cpu_quota_frozen_contract_sha256"] != treatment["artifacts"]["cpu_quota_frozen_contract_sha256"]:
        _fail("arms do not share exact frozen CPU quota contract bytes")
    if sham["frozen_cpu_identity"] != treatment["frozen_cpu_identity"]:
        _fail("arms do not share exact base CPU profile identity")
    sham_manifest, _ = _document(Path(sham_root) / "materialization-manifest.json", "sham materialization manifest")
    treatment_manifest, _ = _document(Path(treatment_root) / "materialization-manifest.json", "treatment materialization manifest")
    if sham_manifest.get("binary_sha256") != treatment_manifest.get("binary_sha256"):
        _fail("arms do not share exact materialized binary identity")
    if treatment["count"] < 0 or sham["count"] <= 0:
        _fail("sham common-commit denominator is not positive")
    meets = treatment["count"] * denominator >= sham["count"] * numerator
    return {
        "schema_version": 1, "kind": _RESULT_KIND,
        "verdict": "PAIR_COMPLETE_DESCRIPTIVE_ONLY", "claim_eligible": False,
        "figure_eligible": False, "campaign_eligible": False,
        "pair_id": manifest["pair_id"], "measurement": manifest["measurement"],
        "effect_statistic": manifest["effect_statistic"],
        "threshold": {"numerator": numerator, "denominator": denominator},
        "sham_common_commit_count": sham["count"],
        "treatment_common_commit_count": treatment["count"],
        "ratio": {"numerator": treatment["count"], "denominator": sham["count"]},
        "meets_practical_threshold": meets,
        "raw_validation_sha256": {"sham": sham["raw_validation_sha256"], "treatment": treatment["raw_validation_sha256"]},
        "raw_authority_sha256": {"sham": sham["authority_sha256"], "treatment": treatment["authority_sha256"]},
        "claim_boundary": "One matched pair is descriptive only; a frozen repeated campaign is required for any claim or figure.",
    }
