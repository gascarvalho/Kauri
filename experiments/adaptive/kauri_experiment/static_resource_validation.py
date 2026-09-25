"""Independent, source-blind validator for the excluded N=31 CPU shakedown.

This is deliberately a *consumer* of a sealed all-live backend result.  It
does not start processes, apply quotas, or treat a synthetic fixture as live
evidence.  The emitted verdict is always claim- and figure-ineligible for S4.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from . import factorial_validation
from . import cpu_quota, static_resource_pair
from .profiled_fault_archive import EvidenceSealError, verify_evidence_seal


STUDY_ID = "static-resource-n31-pair-v1"
ARMS = ("sham", "adaptive")
REPLICAS = tuple(range(31))
Q = 21


class StaticResourceValidationError(ValueError):
    """The sealed evidence is incomplete or cannot support a verdict."""


def _safe_path(root: Path, relative: str, label: str) -> Path:
    """Resolve one manifest path without permitting escape or symlink hops."""
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise StaticResourceValidationError(f"{label} path is invalid")
    parsed = PurePosixPath(relative)
    if parsed.is_absolute() or parsed.as_posix() != relative or any(part in {"", ".", ".."} for part in parsed.parts):
        raise StaticResourceValidationError(f"{label} path is not canonical and relative")
    current = root
    for part in parsed.parts:
        current = current / part
        try:
            state = current.lstat()
        except FileNotFoundError:
            # The final missing file is handled by the caller; a missing
            # intermediate component cannot be used as an escape hatch.
            continue
        except OSError as exc:
            raise StaticResourceValidationError(f"cannot inspect {label} path") from exc
        if current != root / parsed.parts[-1] and current.is_symlink():
            raise StaticResourceValidationError(f"{label} path has a symlink ancestor")
    try:
        current.resolve(strict=False).relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise StaticResourceValidationError(f"{label} path escapes pair root") from exc
    return current


def _reject_constant(value: str) -> None:
    raise StaticResourceValidationError(f"non-finite JSON constant: {value}")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StaticResourceValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read(root: Path, relative: str, label: str) -> Mapping[str, Any]:
    path = _safe_path(root, relative, label)
    if path.is_symlink() or not path.is_file():
        raise StaticResourceValidationError(f"{label} is not a regular file")
    try:
        value = json.loads(path.read_bytes(), object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StaticResourceValidationError(f"{label} is invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise StaticResourceValidationError(f"{label} must be an object")
    return value


def _hash(root: Path, relative: str) -> str:
    path = _safe_path(root, relative, "sealed source")
    if path.is_symlink() or not path.is_file():
        raise StaticResourceValidationError(f"sealed source is absent: {relative}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, allow_nan=False, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"


def _expect_keys(document: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(document) != expected:
        raise StaticResourceValidationError(f"{label} schema drifted")


def _hex(value: object, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and all(character in "0123456789abcdef" for character in value)


def _event_lines(root: Path, relative: str, label: str) -> list[Mapping[str, Any]]:
    path = _safe_path(root, relative, label)
    if path.is_symlink() or not path.is_file():
        raise StaticResourceValidationError(f"{label} is absent")
    rows: list[Mapping[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line, object_pairs_hook=_pairs, parse_constant=_reject_constant)
            if not isinstance(item, Mapping):
                raise StaticResourceValidationError(f"{label} has a non-object event")
            rows.append(item)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StaticResourceValidationError(f"{label} is invalid JSONL") from exc
    if not rows:
        raise StaticResourceValidationError(f"{label} is empty")
    return rows


def _contains_forbidden(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            key in {"resource_contract", "resource_contract_sha256", "cohort", "quota", "cpu_quota"}
            or _contains_forbidden(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden(child) for child in value)
    return False


def _contains_resource_label(value: object) -> bool:
    if isinstance(value, str):
        lowered = value.lower()
        return any(token in lowered for token in ("cpu_quota", "cpu-quota", "resource_contract", "25%", "100%", "slow", "fast"))
    if isinstance(value, Mapping):
        return any(_contains_resource_label(key) or _contains_resource_label(child) for key, child in value.items())
    if isinstance(value, list):
        return any(_contains_resource_label(child) for child in value)
    return False


def _projection(bundle: object) -> list[dict[str, object]]:
    trees = getattr(bundle, "trees", None)
    if not isinstance(trees, tuple) or len(trees) != Q:
        raise StaticResourceValidationError("epoch bundle does not contain 21 trees")
    result = []
    for expected_id, tree in enumerate(trees):
        members = tuple(getattr(tree, "members", ()))
        if getattr(tree, "tree_id", None) != expected_id or len(members) != 31 or set(members) != set(REPLICAS):
            raise StaticResourceValidationError("epoch tree membership or ordering is invalid")
        result.append({"tree_id": expected_id, "members": list(members)})
    if len({row["members"][0] for row in result}) != Q:
        raise StaticResourceValidationError("epoch roots are not 21 distinct members")
    return result


def _decode(root: Path, bundle_path: str, key_path: str) -> object:
    key = _safe_path(root, key_path, "issuer key")
    wire = _safe_path(root, bundle_path, "epoch bundle")
    if key.is_symlink() or wire.is_symlink() or not key.is_file() or not wire.is_file():
        raise StaticResourceValidationError("epoch bundle or issuer key is absent")
    try:
        return factorial_validation.decode_adaptive_v3_epoch_change_bundle(
            wire.read_bytes(), issuer_public_key=key.read_text(encoding="ascii").strip()
        )
    except (OSError, UnicodeError, factorial_validation.FactorialValidationError) as exc:
        raise StaticResourceValidationError("epoch bundle is not independently decodable") from exc


def _validate_receipts(root: Path, arm: str, manifest: Mapping[str, Any]) -> tuple[bool, float]:
    activation = _read(root, str(manifest["activation_path"]), f"{arm} activation")
    _expect_keys(activation, {"epoch", "members"}, f"{arm} activation")
    members = activation["members"]
    if not isinstance(members, list) or len(members) != 31:
        raise StaticResourceValidationError(f"{arm} activation lacks all members")
    seen = set()
    for row in members:
        if not isinstance(row, Mapping) or set(row) != {"replica_id", "active", "alive", "eligible"}:
            raise StaticResourceValidationError(f"{arm} activation member schema drifted")
        replica = row["replica_id"]
        if type(replica) is not int or replica in seen or any(row[field] is not True for field in ("active", "alive", "eligible")):
            raise StaticResourceValidationError(f"{arm} is not all-live and eligible")
        seen.add(replica)
    if seen != set(REPLICAS) or activation["epoch"] != 1:
        raise StaticResourceValidationError(f"{arm} activation identity drifted")

    resource = _read(root, str(manifest["resource_receipt_path"]), f"{arm} resource receipt")
    _expect_keys(resource, {"window", "launch_path", "samples_path", "service_path"}, f"{arm} resource receipt")
    window = resource["window"]
    if not isinstance(window, Mapping) or set(window) != {"start_ns", "end_ns"} or type(window["start_ns"]) is not int or type(window["end_ns"]) is not int or window["start_ns"] >= window["end_ns"]:
        raise StaticResourceValidationError(f"{arm} resource window is invalid")
    launch = _read(root, str(resource["launch_path"]), f"{arm} CPU launch receipt")
    _expect_keys(launch, {"schema_version", "launcher", "contract_id", "contract_sha256", "manager_visibility", "replicas"}, f"{arm} CPU launch receipt")
    if launch["schema_version"] != 1 or launch["launcher"] != "systemd-user-scope-cpu-quota-v1" or launch["manager_visibility"] != "none" or not isinstance(launch["replicas"], list) or len(launch["replicas"]) != 31:
        raise StaticResourceValidationError(f"{arm} CPU launch receipt is invalid")
    samples = _event_lines(root, str(resource["samples_path"]), f"{arm} CPU samples")
    samples_by_replica: dict[int, list[Mapping[str, Any]]] = {}
    for sample in samples:
        expected = {"schema_version", "source_monotonic_ns", "replica_id", "cpu_quota_percent", "unit", "control_group", "cpu_stat_path", "cpu_quota_per_second_usec", "active_state", "sub_state", "cpu_stat"}
        if set(sample) != expected or sample["schema_version"] != 1:
            raise StaticResourceValidationError(f"{arm} CPU sample schema drifted")
        replica = sample["replica_id"]
        stat = sample["cpu_stat"]
        if type(replica) is not int or not isinstance(stat, Mapping) or set(stat) not in ({"usage_usec", "user_usec", "system_usec"}, {"usage_usec", "user_usec", "system_usec", "nr_periods", "nr_throttled", "throttled_usec"}) or any(type(value) is not int or value < 0 for value in stat.values()):
            raise StaticResourceValidationError(f"{arm} CPU sample accounting is invalid")
        samples_by_replica.setdefault(replica, []).append(sample)
    quotas: dict[int, int] = {}
    for row in launch["replicas"]:
        expected = {"replica_id", "cpu_quota_percent", "unit", "control_group", "cpu_stat_path", "owned_pid", "owned_pgid", "cgroup_pids", "active_state", "sub_state", "cpu_quota_per_second_usec"}
        if not isinstance(row, Mapping) or set(row) != expected:
            raise StaticResourceValidationError(f"{arm} CPU launch member schema drifted")
        replica, quota = row["replica_id"], row["cpu_quota_percent"]
        if type(replica) is not int or type(quota) is not int or replica in quotas or row["active_state"] != "active" or row["sub_state"] not in {"running", "start"} or row["cpu_quota_per_second_usec"] != quota * 10_000:
            raise StaticResourceValidationError(f"{arm} CPU launch member identity is invalid")
        replica_samples = samples_by_replica.get(replica, [])
        if len(replica_samples) < 2:
            raise StaticResourceValidationError(f"{arm} has no delivered CPU coverage")
        times = []
        for sample in replica_samples:
            if sample["cpu_quota_percent"] != quota or sample["cpu_quota_per_second_usec"] != quota * 10_000 or sample["active_state"] != "active" or sample["sub_state"] not in {"running", "start"} or sample["control_group"] != row["control_group"] or sample["unit"] != row["unit"]:
                raise StaticResourceValidationError(f"{arm} CPU sample does not bind effective launch quota")
            times.append(sample["source_monotonic_ns"])
        if any(type(time) is not int or time < 0 for time in times) or times != sorted(times) or times[0] > window["start_ns"] or times[-1] < window["end_ns"] or replica_samples[-1]["cpu_stat"]["usage_usec"] <= replica_samples[0]["cpu_stat"]["usage_usec"]:
            raise StaticResourceValidationError(f"{arm} CPU delivery does not cover the window")
        quotas[replica] = quota
    if quotas != {**{i: 25 for i in range(6)}, **{i: 100 for i in range(6, 31)}}:
        raise StaticResourceValidationError(f"{arm} CPU quota assignment drifted")

    service = _read(root, str(resource["service_path"]), f"{arm} delivered-service receipt")
    _expect_keys(service, {"window", "members"}, f"{arm} delivered-service receipt")
    if service["window"] != window or not isinstance(service["members"], list) or len(service["members"]) != 31:
        raise StaticResourceValidationError(f"{arm} delivered-service window is invalid")
    rates: dict[int, float] = {}
    for row in service["members"]:
        if not isinstance(row, Mapping) or set(row) != {"replica_id", "samples"} or type(row["replica_id"]) is not int or not isinstance(row["samples"], list) or len(row["samples"]) < 2:
            raise StaticResourceValidationError(f"{arm} delivered-service member is invalid")
        points = row["samples"]
        if any(not isinstance(point, Mapping) or set(point) != {"monotonic_ns", "completed_units"} or any(type(point[key]) is not int or point[key] < 0 for key in point) for point in points):
            raise StaticResourceValidationError(f"{arm} delivered-service sample is invalid")
        times = [point["monotonic_ns"] for point in points]
        units = [point["completed_units"] for point in points]
        if row["replica_id"] in rates or times != sorted(times) or units != sorted(units) or times[0] > window["start_ns"] or times[-1] < window["end_ns"] or units[-1] <= units[0]:
            raise StaticResourceValidationError(f"{arm} delivered-service coverage is incomplete")
        rates[row["replica_id"]] = (units[-1] - units[0]) / ((times[-1] - times[0]) / 1_000_000_000)
    if set(rates) != set(REPLICAS) or max(rates[replica] for replica in range(6)) >= min(rates[replica] for replica in range(6, 31)):
        raise StaticResourceValidationError(f"{arm} delivered-service separation is not demonstrated")

    cleanup = _read(root, str(manifest["cleanup_path"]), f"{arm} cleanup")
    _expect_keys(cleanup, {"complete", "owned"}, f"{arm} cleanup")
    owned = cleanup["owned"]
    if cleanup["complete"] is not True or not isinstance(owned, list) or set(owned) != {*(f"replica-{i}" for i in REPLICAS), "manager"}:
        raise StaticResourceValidationError(f"{arm} cleanup is incomplete")

    commits = _read(root, str(manifest["commit_path"]), f"{arm} commits")
    _expect_keys(commits, {"window", "events"}, f"{arm} commits")
    if commits["window"] != window or not isinstance(commits["events"], list):
        raise StaticResourceValidationError(f"{arm} commit window differs from resource window")
    previous_height = None
    previous_hash = None
    unique: set[tuple[int, str]] = set()
    for item in commits["events"]:
        if not isinstance(item, Mapping) or set(item) != {"height", "block_hash", "parent_hash", "transactions", "monotonic_ns"}:
            raise StaticResourceValidationError(f"{arm} authoritative commit schema drifted")
        height, block_hash, parent, transactions, timestamp = (item[k] for k in ("height", "block_hash", "parent_hash", "transactions", "monotonic_ns"))
        if type(height) is not int or not isinstance(block_hash, str) or not isinstance(parent, str) or type(transactions) is not int or transactions <= 0 or type(timestamp) is not int or timestamp < window["start_ns"] or timestamp > window["end_ns"] or (height, block_hash) in unique:
            raise StaticResourceValidationError(f"{arm} authoritative commit identity is invalid")
        if previous_height is not None and (height != previous_height + 1 or parent != previous_hash):
            raise StaticResourceValidationError(f"{arm} authoritative commits are not one chain")
        unique.add((height, block_hash)); previous_height, previous_hash = height, block_hash
    if not unique:
        raise StaticResourceValidationError(f"{arm} has no authoritative commits")
    duration = (window["end_ns"] - window["start_ns"]) / 1_000_000_000
    return True, sum(item["transactions"] for item in commits["events"]) / duration


def validate_pair(root: Path) -> dict[str, object]:
    """Validate one sealed S4 pair and return PASS/FAIL/INCOMPLETE only.

    Invalid evidence raises ``StaticResourceValidationError``; callers must
    persist it as an INCOMPLETE receipt rather than converting it to FAIL.
    """
    root = Path(root).resolve(strict=True)
    try:
        pair_seal = verify_evidence_seal(root)
    except (EvidenceSealError, OSError) as exc:
        raise StaticResourceValidationError("pair evidence seal is invalid") from exc
    pair = _read(root, "pair-manifest.json", "pair manifest")
    _expect_keys(pair, {"schema_version", "study_id", "mode", "pair_id", "execution", "protocol", "bindings", "shared_epoch0_projection_sha256", "shared_epoch0_digest", "issuer_public_key_sha256", "backend_raw_evidence_path", "arms", "paths"}, "pair manifest")
    if pair["schema_version"] != 1 or pair["study_id"] != STUDY_ID or pair["mode"] != "excluded_shakedown" or not isinstance(pair["pair_id"], str) or not pair["pair_id"]:
        raise StaticResourceValidationError("pair identity drifted")
    if pair["execution"] != {"schedule": list(ARMS), "automatic_retries": 0, "replacement_policy": "none", "claim_eligible": False, "figure_eligible": False} or pair["protocol"] != {"N": 31, "f": 10, "Q": Q, "tree_count": Q}:
        raise StaticResourceValidationError("pair execution or protocol drifted")
    bindings = pair["bindings"]
    if not isinstance(bindings, Mapping):
        raise StaticResourceValidationError("pair provenance bindings are absent")
    _expect_keys(bindings, {"revision", "build_sha256", "workload_sha256", "successor_schedule_sha256", "host_identity_sha256", "profile_path", "profile_sha256", "cpu_contract_path", "cpu_contract_sha256"}, "pair provenance bindings")
    if (
        not _hex(bindings["revision"], 40)
        or any(not _hex(bindings[field], 64) for field in ("build_sha256", "workload_sha256", "successor_schedule_sha256", "host_identity_sha256", "profile_sha256", "cpu_contract_sha256"))
        or not isinstance(bindings["profile_path"], str)
        or not isinstance(bindings["cpu_contract_path"], str)
        or _hash(root, bindings["profile_path"]) != bindings["profile_sha256"]
        or _hash(root, bindings["cpu_contract_path"]) != bindings["cpu_contract_sha256"]
    ):
        raise StaticResourceValidationError("pair provenance bindings drifted")
    try:
        profile = static_resource_pair.load_profile(root / str(bindings["profile_path"]))
        contract = static_resource_pair.load_cpu_contract(root / str(bindings["cpu_contract_path"]), profile_path=root / str(bindings["profile_path"]))
    except (static_resource_pair.StaticResourcePairError, cpu_quota.CpuQuotaContractError) as exc:
        raise StaticResourceValidationError("archived profile or CPU contract semantics are invalid") from exc
    if profile.profile_sha256 != bindings["profile_sha256"] or contract.contract_sha256 != bindings["cpu_contract_sha256"]:
        raise StaticResourceValidationError("archived profile or CPU contract hash drifted")
    if not isinstance(pair["arms"], Mapping) or set(pair["arms"]) != set(ARMS):
        raise StaticResourceValidationError("pair arm inventory drifted")
    if not _hex(pair["shared_epoch0_digest"], 64) or not _hex(pair["issuer_public_key_sha256"], 64) or not isinstance(pair["backend_raw_evidence_path"], str):
        raise StaticResourceValidationError("pair epoch or issuer provenance drifted")
    declared_paths = pair["paths"]
    sealed_paths = [entry.path for entry in pair_seal.entries]
    if (
        not isinstance(declared_paths, list)
        or any(not isinstance(path, str) for path in declared_paths)
        or declared_paths != sorted(set(declared_paths))
        or declared_paths != sealed_paths
    ):
        raise StaticResourceValidationError("pair manifest does not inventory exact sealed bytes")
    referenced_paths = {
        "pair-manifest.json",
        str(bindings["profile_path"]),
        str(bindings["cpu_contract_path"]),
        *(str(descriptor["manifest_path"]) for descriptor in pair["arms"].values() if isinstance(descriptor, Mapping)),
    }
    if not referenced_paths.issubset(set(declared_paths)):
        raise StaticResourceValidationError("pair manifest references bytes outside its sealed inventory")

    projections: dict[str, list[dict[str, object]]] = {}
    roots: dict[str, list[int]] = {}
    rates: dict[str, float] = {}
    for arm in ARMS:
        descriptor = pair["arms"][arm]
        if not isinstance(descriptor, Mapping) or set(descriptor) != {"manifest_path", "tree_sha256", "seal_sha256"}:
            raise StaticResourceValidationError(f"{arm} pair descriptor drifted")
        arm_root = root / arm
        try:
            seal = verify_evidence_seal(arm_root)
        except (EvidenceSealError, OSError) as exc:
            raise StaticResourceValidationError(f"{arm} evidence seal is invalid") from exc
        if descriptor["manifest_path"] != f"{arm}/arm-manifest.json" or descriptor["tree_sha256"] != seal.tree_sha256 or descriptor["seal_sha256"] != seal.seal_sha256:
            raise StaticResourceValidationError(f"{arm} child seal binding drifted")
        manifest = _read(root, str(descriptor["manifest_path"]), f"{arm} manifest")
        _expect_keys(manifest, {"schema_version", "study_id", "pair_id", "arm", "attempt", "retry_count", "issuer_key_path", "epoch0_path", "epoch1_path", "manager_path", "manager_argv_path", "activation_path", "resource_receipt_path", "commit_path", "cleanup_path"}, f"{arm} manifest")
        if manifest["schema_version"] != 1 or manifest["study_id"] != STUDY_ID or manifest["pair_id"] != pair["pair_id"] or manifest["arm"] != arm or manifest["attempt"] != 1 or manifest["retry_count"] != 0:
            raise StaticResourceValidationError(f"{arm} manifest identity or retry drifted")
        arm_references = {
            str(manifest[field])
            for field in ("issuer_key_path", "epoch0_path", "epoch1_path", "manager_path", "manager_argv_path", "activation_path", "resource_receipt_path", "commit_path", "cleanup_path")
        }
        arm_references.update({f"{arm}/manager-snapshot.json", *(f"{arm}/raw/replica-{replica}.jsonl" for replica in REPLICAS)})
        if not arm_references.issubset(set(declared_paths)):
            raise StaticResourceValidationError(f"{arm} manifest references bytes outside sealed inventory")
        manager_events = _event_lines(root, str(manifest["manager_path"]), f"{arm} manager stream")
        manager_argv = _read(root, str(manifest["manager_argv_path"]), f"{arm} manager argv")
        if set(manager_argv) != {"argv"} or not isinstance(manager_argv["argv"], list) or any(not isinstance(value, str) for value in manager_argv["argv"]):
            raise StaticResourceValidationError(f"{arm} manager argv schema drifted")
        if any(_contains_forbidden(event) or _contains_resource_label(event) for event in manager_events) or _contains_resource_label(manager_argv):
            raise StaticResourceValidationError(f"{arm} manager was given resource labels")
        # The activation receipt is not allowed to stand in for missing raw
        # replica provenance.  The backend must preserve one non-empty stream
        # for every exact member, even though this validator intentionally
        # does not interpret generic delivery logs as throughput.
        for replica in REPLICAS:
            _event_lines(root, f"{arm}/raw/replica-{replica}.jsonl", f"{arm} replica-{replica} stream")
        epoch0 = _decode(root, str(manifest["epoch0_path"]), str(manifest["issuer_key_path"]))
        epoch1 = _decode(root, str(manifest["epoch1_path"]), str(manifest["issuer_key_path"]))
        if getattr(epoch0, "epoch_number", None) != 0 or getattr(epoch1, "epoch_number", None) != 1 or getattr(epoch1, "previous_epoch_digest", None) != getattr(epoch0, "epoch_digest", None):
            raise StaticResourceValidationError(f"{arm} epoch transition does not bind E0 to E1")
        projection0, projection1 = _projection(epoch0), _projection(epoch1)
        if getattr(epoch0, "epoch_digest", None) != pair["shared_epoch0_digest"] or _hash(root, str(manifest["issuer_key_path"])) != pair["issuer_public_key_sha256"]:
            raise StaticResourceValidationError(f"{arm} common E0 digest or issuer binding drifted")
        projections[arm] = projection0
        roots[arm] = [row["members"][0] for row in projection1]  # type: ignore[index]
        snapshot = _read(root, f"{arm}/manager-snapshot.json", f"{arm} manager snapshot")
        _expect_keys(snapshot, {"eligible_ranking"}, f"{arm} manager snapshot")
        ranking = snapshot["eligible_ranking"]
        if not isinstance(ranking, list) or len(ranking) != 31 or set(ranking) != set(REPLICAS) or any(type(item) is not int for item in ranking):
            raise StaticResourceValidationError(f"{arm} ranking is not the all-live N31 order")
        if arm == "sham" and projection1 != projection0:
            raise StaticResourceValidationError("sham E1 is not an exact ordered E0 copy")
        if arm == "adaptive" and roots[arm] != ranking[:Q]:
            raise StaticResourceValidationError("adaptive E1 roots do not bind the native ranking")
        _, rates[arm] = _validate_receipts(root, arm, manifest)

    if projections["sham"] != projections["adaptive"] or hashlib.sha256(_canonical(projections["sham"]).encode("ascii")).hexdigest() != pair["shared_epoch0_projection_sha256"]:
        raise StaticResourceValidationError("arms do not share the frozen E0 projection")
    demoted = sorted(set(range(6)) & set(roots["sham"]) - set(roots["adaptive"]))
    ratio = rates["adaptive"] / rates["sham"]
    # The runner slice available at S2 only emits self-authored receipts.  A
    # result is deliberately non-admissible until a dedicated backend emits a
    # raw-evidence contract that this module can reconstruct independently.
    # Deliberately do not accept a merely present path: the current runner has
    # no dedicated backend event format from which activation, liveness,
    # commits, service, and cleanup can be reconstructed independently.  The
    # receipt-only path must never yield a scientific PASS or FAIL.
    raise StaticResourceValidationError(
        "dedicated backend reconstruction not implemented"
    )
    all_six = demoted == list(range(6))
    return {"schema_version": 1, "study_id": STUDY_ID, "verdict": "PASS" if all_six else "FAIL", "mechanism_state": "ALL_SIX_DEMOTED" if all_six else "PARTIAL_OR_NO_DEMOTION", "demoted_constrained_root_ids": demoted, "descriptive_late_window_tps": rates, "descriptive_adaptive_to_sham_ratio": ratio, "claim_eligible": False, "figure_eligible": False, "pair_tree_sha256": pair_seal.tree_sha256}
