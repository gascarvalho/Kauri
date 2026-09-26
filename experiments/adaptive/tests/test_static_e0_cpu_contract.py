"""Frozen CPU assignments for the W16 exploratory 2-by-2 cells."""

from experiments.adaptive.kauri_experiment import cpu_quota
from experiments.adaptive.kauri_experiment.n31_static_e0_feasibility import frozen_plan
from experiments.adaptive.kauri_experiment.static_e0_cpu_contract import frozen_contract


def test_heterogeneous_assignment_is_exact_and_hash_bound() -> None:
    plan = frozen_plan(arm="slow-roots")
    contract = frozen_contract(plan, "heterogeneous")
    assert contract.replica_ids == tuple(range(31))
    assert tuple(contract.quota_percent(i) for i in range(31)) == (25,) * 6 + (100,) * 25
    assert contract.contract_sha256 == cpu_quota.contract_digest(contract)
    assert contract.figure_eligible is False
    assert contract.manager_visibility == "none"


def test_homogeneous_contract_keeps_all_31_at_one_cpu() -> None:
    plan = frozen_plan(arm="fast-roots")
    contract = frozen_contract(plan, "homogeneous")
    assert tuple(contract.quota_percent(i) for i in range(31)) == (100,) * 31
    assert contract.contract_sha256 == cpu_quota.contract_digest(contract)


def test_topology_arm_does_not_change_resource_assignment() -> None:
    left = frozen_contract(frozen_plan(arm="slow-roots"), "heterogeneous")
    right = frozen_contract(frozen_plan(arm="fast-roots"), "heterogeneous")
    assert left.assignments == right.assignments
    assert left.contract_sha256 == right.contract_sha256
