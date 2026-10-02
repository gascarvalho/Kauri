"""Explicit physical quota regimes for the prospective W18 cluster study.

The nominal slow/fast ID labels and both epoch placements stay identical in
the homogeneous control. Only physical service quotas become 100/100. This
does not change the older local rehearsal's fixed 25/100 contract.
"""
from __future__ import annotations

import copy
import hashlib
import json

from .operator_capacity_preflight import _EXPECTED_QUOTA_PROFILE


def expected_quota_profile(regime: str) -> dict:
    if regime not in {"heterogeneous", "homogeneous"}:
        raise ValueError("unknown frozen W18 physical quota regime")
    result = copy.deepcopy(_EXPECTED_QUOTA_PROFILE)
    if regime == "homogeneous":
        result.update(
            contract_id="n31-operator-capacity-homogeneous-control-quota-v1",
            base_profile_id="n31-operator-capacity-homogeneous-control-v1",
            base_profile_sha256="9f75304b3482e35f3fa9c90cfb26398dd99a37e21de1aff853619373a0129339",
            base_profile_canonical_sha256="f7f888d78a08fa4391f6d82bd9d283f0264ca8b7939fef5374e17fc6e7db4d77",
        )
        for row in result["assignments"]:
            row["cpu_quota_percent"] = 100
    return result


def validate_quota_bytes(raw: bytes, regime: str) -> str:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("cluster quota contract repeats a field")
            result[key] = value
        return result
    value = json.loads(raw.decode("ascii"), object_pairs_hook=pairs)
    canonical = lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if canonical(value) != canonical(expected_quota_profile(regime)):
        raise ValueError("physical quota contract differs from frozen cluster regime")
    return hashlib.sha256(raw).hexdigest()


def verify_loaded_contract(contract, regime: str) -> None:
    expected = expected_quota_profile(regime)
    # Recreate the same canonical document used by the native scope runtime.
    from .cpu_quota import _contract_document
    canonical = lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if canonical(_contract_document(contract)) != canonical(expected):
        raise ValueError("loaded CPU contract differs from frozen physical regime")


def load_cluster_contract(path, *, base_profile_path, regime):
    """Exact equal-quota control admission; the generic heterogeneity gate stays strict."""
    from . import cpu_quota
    raw = cpu_quota._regular_bytes(path, "frozen cluster quota profile")
    validate_quota_bytes(raw, regime)
    if regime == "heterogeneous":
        return cpu_quota.load_cpu_quota_contract(path, base_profile_path=base_profile_path,
                                                expected_replica_ids=tuple(range(31)))
    profile_raw = cpu_quota._regular_bytes(base_profile_path, "homogeneous base profile")
    profile = json.loads(profile_raw)
    value = json.loads(raw)
    if (hashlib.sha256(profile_raw).hexdigest() != value["base_profile_sha256"] or
            cpu_quota._profile_canonical_sha256(profile) != value["base_profile_canonical_sha256"] or
            profile.get("profile_id") != value["base_profile_id"]):
        raise ValueError("homogeneous base profile differs from exact frozen hashes")
    contract = cpu_quota.CpuQuotaContract(**{key: value[key] for key in value if key != "assignments"},
        assignments=tuple(cpu_quota.CpuQuotaAssignment(**row) for row in value["assignments"]),
        contract_sha256=hashlib.sha256(raw).hexdigest())
    verify_loaded_contract(contract, regime)
    return contract
