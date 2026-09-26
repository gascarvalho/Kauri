"""Exact external CPU assignments for exploratory W16 static-E0 cells.

These contracts are deliberately figure-ineligible and manager-blind.  They
do not change Kauri consensus or the native topology bytes.
"""

from __future__ import annotations

from dataclasses import replace

from . import cpu_quota
from .n31_static_e0_feasibility import FeasibilityPlan


def frozen_contract(plan: FeasibilityPlan, mode: str) -> cpu_quota.CpuQuotaContract:
    if mode not in {"heterogeneous", "homogeneous"}:
        raise ValueError("W16 CPU mode must be heterogeneous or homogeneous")
    assignments = tuple(
        cpu_quota.CpuQuotaAssignment(
            replica_id=replica,
            capacity_class=(
                "slow" if mode == "heterogeneous" and replica < 6
                else "fast" if mode == "heterogeneous" else "uniform"
            ),
            cpu_quota_percent=(25 if mode == "heterogeneous" and replica < 6 else 100),
        )
        for replica in plan.profile.replica_ids
    )
    contract = cpu_quota.CpuQuotaContract(
        schema_version=1,
        contract_id=f"w16-e0-{mode}-v1",
        enabled=True,
        figure_eligible=False,
        launcher="systemd-user-scope-cpu-quota-v1",
        manager_visibility="none",
        sampling_interval_ms=1000,
        base_profile_id=plan.profile.profile_id,
        base_profile_sha256=plan.profile.sha256,
        base_profile_canonical_sha256=plan.profile.sha256,
        assignments=assignments,
        contract_sha256="",
    )
    return replace(contract, contract_sha256=cpu_quota.contract_digest(contract))
