"""Tests for passive T6-to-T0 aggregate-relay cross-check settlement."""

from __future__ import annotations

import importlib
import re

import pytest


EPOCH_DIGEST = "a" * 64
CERTIFICATE_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _api():
    return importlib.import_module(
        "experiments.adaptive.kauri_experiment.passive_crosscheck"
    )


def _scope(*, expected_message_type: str = "aggregate_relay"):
    api = _api()
    return api.PassiveCrosscheckScope(
        epoch_number=0,
        epoch_digest=EPOCH_DIGEST,
        target_id=1,
        expected_message_type=expected_message_type,
        phases=(
            api.PassiveCrosscheckPhase(tree_id=6, reporter_id=6),
            api.PassiveCrosscheckPhase(tree_id=0, reporter_id=0),
        ),
        diagnostic_fault_bound=1,
    )


def _observation(
    *,
    tree_id: int,
    reporter_id: int,
    outcome: str,
    observation_id: str,
    expected_message_type: str = "aggregate_relay",
):
    api = _api()
    return api.PassiveCrosscheckObservation(
        observation_id=observation_id,
        reporter_id=reporter_id,
        target_id=1,
        epoch_number=0,
        tree_id=tree_id,
        epoch_digest=EPOCH_DIGEST,
        expected_message_type=expected_message_type,
        outcome=outcome,
    )


def _tree6_timeout():
    return _observation(
        tree_id=6,
        reporter_id=6,
        outcome="timeout",
        observation_id="1" * 64,
    )


def test_one_manager_accepted_aggregate_relay_stays_pending() -> None:
    api = _api()
    certificate = api.build_passive_crosscheck_certificate(
        _scope(),
        [_tree6_timeout()],
    )

    assert certificate["status"] == "pending"
    assert certificate["ordering_basis"] == "manager_receipt_order"
    assert certificate["compatible_hypothesis_count"] == 2
    assert certificate["settled_hypothesis"] is None
    assert certificate["durable_role_exclusions"] == []
    assert certificate["pending_endpoints"] == [1, 6]
    assert CERTIFICATE_DIGEST.fullmatch(
        certificate["certificate_sha256"]
    )
    assert certificate == api.build_passive_crosscheck_certificate(
        _scope(),
        [_tree6_timeout()],
    )


@pytest.mark.parametrize(
    ("followup_outcome", "settled_hypothesis", "excluded_replica"),
    (
        (
            "response",
            {
                "false_reporters": [6],
                "persistent_omitters": [],
            },
            6,
        ),
        (
            "timeout",
            {
                "false_reporters": [],
                "persistent_omitters": [1],
            },
            1,
        ),
    ),
)
def test_distinct_t0_reporter_definitively_settles_the_two_modes(
    followup_outcome: str,
    settled_hypothesis: dict[str, list[int]],
    excluded_replica: int,
) -> None:
    api = _api()
    followup = _observation(
        tree_id=0,
        reporter_id=0,
        outcome=followup_outcome,
        observation_id="2" * 64,
    )

    certificate = api.build_passive_crosscheck_certificate(
        _scope(),
        [_tree6_timeout(), followup],
    )

    assert certificate["status"] == "settled"
    assert certificate["ordering_basis"] == "manager_receipt_order"
    assert certificate["compatible_hypothesis_count"] == 1
    assert certificate["settled_hypothesis"] == settled_hypothesis
    assert certificate["durable_role_exclusions"] == [excluded_replica]
    assert certificate["pending_endpoints"] == []
    assert CERTIFICATE_DIGEST.fullmatch(
        certificate["certificate_sha256"]
    )


@pytest.mark.parametrize(
    "scope_message_type",
    ("aggregate_relay", "direct_vote"),
)
def test_direct_vote_cannot_substitute_for_the_t0_aggregate_crosscheck(
    scope_message_type: str,
) -> None:
    api = _api()
    followup = _observation(
        tree_id=0,
        reporter_id=0,
        outcome="timeout",
        observation_id="2" * 64,
        expected_message_type="direct_vote",
    )

    with pytest.raises(
        api.PassiveCrosscheckError,
        match="aggregate_relay|message type",
    ):
        api.build_passive_crosscheck_certificate(
            _scope(expected_message_type=scope_message_type),
            [_tree6_timeout(), followup],
        )
