"""Contracts for the bounded reputation-policy replay experiment."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from experiments.adaptive.kauri_experiment.reputation_policy_replay import (
    ReputationPolicyReplayError,
    load_bound_arm_verdict,
    mechanisms_share_eligibility,
    rank_replicas,
)
from experiments.adaptive import validate_reputation_policy_replay as validator


REPOSITORY = Path(__file__).resolve().parents[3]
PROFILE_PATH = (
    REPOSITORY
    / "experiments/adaptive/profiles/n7-reputation-policy-replay-v1.json"
)


def _observation(
    sequence: int,
    replica: int,
    outcome: str,
    latency_us: int,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "observation_id": f"{sequence:064x}",
        "reporter_id": (replica + 1) % 7,
        "observed_replica_id": replica,
        "configuration": {
            "epoch_number": 0,
            "tree_id": 0,
            "epoch_digest": "a" * 64,
        },
        "block_hash": f"{sequence + 1000:064x}",
        "expected_message_type": "direct_vote",
        "outcome": outcome,
        "response_duration_us": latency_us,
        "deadline_duration_us": 1_500_000,
        "reporter_monotonic_ns": sequence * 1_000,
        "reporter_sequence": sequence,
        "signer_set": [] if outcome == "timeout" else [replica],
        "ingestion_sequence": sequence,
    }


def _tradeoff_observations() -> list[dict[str, object]]:
    observations: list[dict[str, object]] = []
    sequence = 1
    # Replica 0 meets the shared threshold exactly but has the lowest latency.
    for outcome, latency in (
        ("timeout", 0),
        ("on_time", 10),
        ("on_time", 11),
        ("on_time", 12),
    ):
        observations.append(_observation(sequence, 0, outcome, latency))
        sequence += 1
    # Every other replica is perfectly responsive but progressively slower.
    for replica in range(1, 7):
        for offset in range(4):
            observations.append(
                _observation(
                    sequence,
                    replica,
                    "on_time",
                    1000 * replica + offset,
                )
            )
            sequence += 1
    return observations


def _profile() -> dict[str, object]:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def test_two_mechanisms_share_eligibility_but_change_role_projection() -> None:
    profile = _profile()
    observations = _tradeoff_observations()

    responsiveness = rank_replicas(observations, profile, "responsiveness")
    latency = rank_replicas(observations, profile, "latency-priority")

    assert responsiveness["eligible_ids"] == [1, 2, 3, 4, 5, 6, 0]
    assert latency["eligible_ids"] == [0, 1, 2, 3, 4, 5, 6]
    assert set(responsiveness["eligible_ids"]) == set(latency["eligible_ids"])
    assert mechanisms_share_eligibility(
        {
            "responsiveness": responsiveness,
            "latency-priority": latency,
        }
    )
    assert responsiveness["role_projection"] == {
        "root_id": 1,
        "internal_ids": [2, 3],
        "influential_ids": [1, 2, 3],
    }
    assert latency["role_projection"] == {
        "root_id": 0,
        "internal_ids": [1, 2],
        "influential_ids": [0, 1, 2],
    }


def test_independent_validator_reconstructs_both_rankings() -> None:
    profile = _profile()
    observations = _tradeoff_observations()
    validator_observations = [
        {**observation, "_sequence": observation["ingestion_sequence"]}
        for observation in observations
    ]
    policy = profile["responsiveness_policy"]
    membership = profile["membership"]

    for mechanism in ("responsiveness", "latency-priority"):
        produced = rank_replicas(observations, profile, mechanism)["ranking"]
        independently_reconstructed = validator._independent_rows(
            validator_observations,
            membership,
            policy,
            mechanism,
        )
        assert independently_reconstructed == produced


def test_policy_rejects_a_timeout_that_claims_response_latency() -> None:
    observations = _tradeoff_observations()
    observations[0]["response_duration_us"] = 1

    with pytest.raises(
        ReputationPolicyReplayError,
        match="timeout has a response duration",
    ):
        rank_replicas(observations, _profile(), "responsiveness")


def test_execution_binding_loads_the_bound_arm_verdict(tmp_path: Path) -> None:
    verdict = {"schema_version": 1, "run_id": "run-1", "verdict": "PASS"}
    verdict_path = tmp_path / "arm-verdict.json"
    payload = json.dumps(verdict).encode("utf-8")
    verdict_path.write_bytes(payload)
    import hashlib

    execution = {
        "arm_verdict": {
            "path": "/archived/location/arm-verdict.json",
            "raw_sha256": hashlib.sha256(payload).hexdigest(),
            "run_id": "run-1",
        }
    }

    assert load_bound_arm_verdict(execution, verdict_path) == verdict
