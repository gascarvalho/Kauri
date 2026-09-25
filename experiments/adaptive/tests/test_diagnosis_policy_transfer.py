"""Retrospective diagnosis-to-policy join over immutable accepted evidence."""

from __future__ import annotations

from copy import deepcopy

import pytest

from experiments.adaptive import run_diagnosis_policy_transfer as producer


@pytest.fixture(autouse=True)
def allow_uncommitted_test_source(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unit tests exercise the analysis before its code commit. The final CLI
    # must still pass the real committed-source gate after that commit.
    monkeypatch.setattr(producer, "_require_committed_source", lambda _path: None)


def test_frozen_five_run_join_has_exact_descriptive_counts() -> None:
    artifact = producer.build()

    assert artifact["verdict"] == "PASS"
    assert [run["ordinal"] for run in artifact["runs"]] == [2, 4, 9, 12, 14]
    assert [run["evidence_cutoff"] for run in artifact["runs"]] == [
        549, 538, 508, 550, 502,
    ]
    assert artifact["summary"] == {
        "responsiveness": {
            "run_count": 5,
            "reporter_excluded_count": 2,
            "reporter_scalar_eligible_count": 5,
            "target_retained_count": 0,
        },
        "latency-priority": {
            "run_count": 5,
            "reporter_excluded_count": 3,
            "reporter_scalar_eligible_count": 5,
            "target_retained_count": 2,
        },
    }
    assert all(
        run["diagnosis"]["reporter_id"] == 6
        and run["diagnosis"]["target_id"] == 1
        and run["diagnosis"]["durable_role_exclusions"] == [6]
        for run in artifact["runs"]
    )


def test_build_is_deterministic_for_one_revision() -> None:
    assert producer.build() == producer.build()


def test_source_path_cannot_escape_repository() -> None:
    with pytest.raises(producer.TransferError, match="repository-contained"):
        producer._source("../outside.json")


def test_mutated_frozen_contract_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    original = producer._load

    def altered(path):
        document = original(path)
        if path.name == producer.PROFILE.name:
            document = deepcopy(document)
            document["fixed_contract"]["reporter_id"] = 5
        return document

    monkeypatch.setattr(producer, "_load", altered)
    with pytest.raises(producer.TransferError, match="fixed analysis contract"):
        producer.build()
