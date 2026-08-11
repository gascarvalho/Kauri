"""Red-first campaign, ledger, and source-blind contracts for the N31 pair."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import importlib
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from experiments.adaptive.tests import test_n31_crash_pair_contract as pair_fixture


RUNNER = "experiments.adaptive.run_n31_crash_pair_campaign"
EXPERIMENT_ID = "n31-f5-q21-three-crash-pair-v1"
REVISION = "b" * 40
BUILD_SHA256 = "c" * 64
PROFILE_SHA256 = "d" * 64
TOPOLOGY_SHA256 = "e" * 64
CAMPAIGN_SEED = 41_719
PAIR_SEEDS = (41_720, 41_721, 41_722, 41_723, 41_724)
EXPECTED_SCHEDULE_SHA256 = (
    "d6f67df39400f44575927a4660d73c4706f33157396ffdc86b202affc2e1c166"
)
EPOCH_DIGESTS = (
    "145fac093343fa9cff20fcf49d85ad5443e93db14146f7854b17e28cf44f6d7a",
    "a8b25ca808229b2684156a3dfdb9e2e4735deb6a80cef2d899fa1599464d1bfd",
    "4364a13a95ce3ab8541e5158c307fc42edc16689a38f26a0abbce7da3b1d3721",
)


def _runner() -> Any:
    return importlib.import_module(RUNNER)


def _document(value: Any) -> dict[str, Any]:
    result = asdict(value) if hasattr(value, "__dataclass_fields__") else value
    assert isinstance(result, dict)
    return result


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        + b"\n"
    )


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _plan_sha256(plan: Mapping[str, Any]) -> str:
    unhashed = {
        key: value
        for key, value in plan.items()
        if key not in {"plan_sha256", "ledger_genesis_sha256"}
    }
    return _sha(unhashed)


def _ledger_genesis_sha256(plan_sha256: str) -> str:
    return _sha(
        {
            "schema_version": 1,
            "domain": "kauri-n31-crash-pair-ledger-genesis-v1",
            "plan_sha256": plan_sha256,
        }
    )


def _expected_slots() -> list[dict[str, object]]:
    pair_order = (
        (1, ("control", "adaptive")),
        (4, ("adaptive", "control")),
        (3, ("control", "adaptive")),
        (5, ("adaptive", "control")),
        (2, ("control", "adaptive")),
    )
    slots: list[dict[str, object]] = []
    for pair_execution_ordinal, (pair_ordinal, arms) in enumerate(pair_order, start=1):
        for within_pair_ordinal, arm in enumerate(arms, start=1):
            execution_ordinal = len(slots) + 1
            slots.append(
                {
                    "execution_ordinal": execution_ordinal,
                    "slot_id": f"slot-{execution_ordinal:02d}",
                    "pair_id": f"pair-{pair_ordinal:02d}",
                    "pair_ordinal": pair_ordinal,
                    "pair_execution_ordinal": pair_execution_ordinal,
                    "within_pair_ordinal": within_pair_ordinal,
                    "arm": arm,
                    "pair_seed": PAIR_SEEDS[pair_ordinal - 1],
                    "attempt_ordinal": 1,
                    "claim_slot": True,
                }
            )
    return slots


def _plan(runner: Any) -> dict[str, Any]:
    return _document(
        runner.derive_focused_campaign_plan(
            pair_count=5,
            campaign_seed=CAMPAIGN_SEED,
            revision=REVISION,
            build_sha256=BUILD_SHA256,
            profile_sha256=PROFILE_SHA256,
            topology_proof_sha256=TOPOLOGY_SHA256,
            pair_seeds=PAIR_SEEDS,
        )
    )


def test_plan_binds_frozen_identity_seeds_and_exact_factorial_schedule() -> None:
    plan = _plan(_runner())
    expected_slots = _expected_slots()
    assert plan["experiment_id"] == EXPERIMENT_ID
    assert plan["revision"] == REVISION
    assert plan["build_sha256"] == BUILD_SHA256
    assert plan["profile_sha256"] == PROFILE_SHA256
    assert plan["topology_proof_sha256"] == TOPOLOGY_SHA256
    assert plan["campaign_seed"] == CAMPAIGN_SEED
    assert tuple(plan["pair_seeds"]) == PAIR_SEEDS
    assert plan["slots"] == expected_slots
    assert _sha(expected_slots) == EXPECTED_SCHEDULE_SHA256
    assert plan["schedule_sha256"] == EXPECTED_SCHEDULE_SHA256
    assert plan["plan_sha256"] == _plan_sha256(plan)
    assert plan["ledger_genesis_sha256"] == _ledger_genesis_sha256(
        plan["plan_sha256"]
    )
    assert plan["automatic_retries"] == 0
    assert plan["replacement_policy"] == "none"
    assert plan["outcome_dependent_order"] is False
    assert "evidence_seal_sha256" not in plan
    assert len({slot["pair_seed"] for slot in plan["slots"]}) == 5
    position_counts = {
        arm: [
            sum(
                slot["arm"] == arm and slot["within_pair_ordinal"] == position
                for slot in plan["slots"]
            )
            for position in (1, 2)
        ]
        for arm in ("control", "adaptive")
    }
    assert position_counts == {"control": [3, 2], "adaptive": [2, 3]}


@pytest.mark.parametrize(
    "mutation",
    ("order", "pair-seed", "schedule-hash", "plan-hash", "genesis-hash"),
)
def test_plan_rejects_schedule_or_seed_drift(mutation: str) -> None:
    plan = _plan(_runner())
    changed = deepcopy(plan)
    if mutation == "order":
        changed["slots"][0], changed["slots"][1] = changed["slots"][1], changed["slots"][0]
    elif mutation == "pair-seed":
        changed["slots"][0]["pair_seed"] += 1
    elif mutation == "schedule-hash":
        changed["schedule_sha256"] = "0" * 64
    elif mutation == "plan-hash":
        changed["plan_sha256"] = "0" * 64
    else:
        changed["ledger_genesis_sha256"] = "0" * 64
    with pytest.raises(_runner().N31CrashPairCampaignError):
        _runner().validate_campaign_plan(changed)


def _ledger(plan: Mapping[str, Any]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    previous = str(plan["ledger_genesis_sha256"])
    strict_keys = {
        "schema_version",
        "plan_sha256",
        "execution_ordinal",
        "slot_id",
        "pair_id",
        "arm",
        "pair_seed",
        "attempt_ordinal",
        "state",
        "execution_outcome",
        "validation",
        "child_tree_sha256",
        "child_seal_sha256",
        "previous_record_sha256",
        "record_sha256",
    }
    for slot in plan["slots"]:
        record: dict[str, object] = {
            "schema_version": 1,
            "plan_sha256": plan["plan_sha256"],
            "execution_ordinal": slot["execution_ordinal"],
            "slot_id": slot["slot_id"],
            "pair_id": slot["pair_id"],
            "arm": slot["arm"],
            "pair_seed": slot["pair_seed"],
            "attempt_ordinal": 1,
            "state": "TERMINAL",
            "execution_outcome": "PASS",
            "validation": {
                "outcome": "PASS",
                "integrity_valid": True,
                "claim_slot": True,
            },
            "child_tree_sha256": hashlib.sha256(
                f"tree-{slot['slot_id']}".encode("ascii")
            ).hexdigest(),
            "child_seal_sha256": hashlib.sha256(
                f"seal-{slot['slot_id']}".encode("ascii")
            ).hexdigest(),
            "previous_record_sha256": previous,
        }
        record["record_sha256"] = _sha(record)
        assert set(record) == strict_keys
        previous = str(record["record_sha256"])
        records.append(record)
    return records


def _rehash_ledger(
    plan: Mapping[str, Any], records: list[dict[str, object]]
) -> None:
    previous = str(plan["ledger_genesis_sha256"])
    for record in records:
        record["previous_record_sha256"] = previous
        record.pop("record_sha256", None)
        record["record_sha256"] = _sha(record)
        previous = str(record["record_sha256"])


def test_ledger_is_one_strict_plan_bound_append_only_hash_chain() -> None:
    runner = _runner()
    plan = _plan(runner)
    records = _ledger(plan)
    summary = _document(runner.validate_campaign_ledger(plan, records))
    assert summary["execution_complete"] is True
    assert summary["attempted_slot_count"] == 10
    assert summary["validated_claim_slot_count"] == 10
    assert summary["ledger_head_sha256"] == records[-1]["record_sha256"]
    assert summary["consumed_execution_ordinals"] == list(range(1, 11))
    assert summary["consumed_slot_ids"] == [
        slot["slot_id"] for slot in plan["slots"]
    ]
    assert summary["unconsumed_slot_ids"] == []
    assert summary["extra_record_count"] == 0


@pytest.mark.parametrize(
    "mutation",
    (
        "appended-retry",
        "record-reorder",
        "slot-drift",
        "plan-drift",
        "incomplete",
        "record-hash",
        "predecessor",
    ),
)
def test_ledger_rejects_schema_hash_or_chain_drift(mutation: str) -> None:
    runner = _runner()
    plan = _plan(runner)
    records = _ledger(plan)
    if mutation == "appended-retry":
        retry = deepcopy(records[-1])
        retry["execution_ordinal"] = 11
        retry["attempt_ordinal"] = 2
        records.append(retry)
        _rehash_ledger(plan, records)
    elif mutation == "record-reorder":
        records[0], records[1] = records[1], records[0]
        _rehash_ledger(plan, records)
    elif mutation == "slot-drift":
        records[0]["pair_id"] = "pair-02"
        _rehash_ledger(plan, records)
    elif mutation == "plan-drift":
        records[0]["plan_sha256"] = "0" * 64
        _rehash_ledger(plan, records)
    elif mutation == "incomplete":
        records.pop()
    elif mutation == "record-hash":
        records[0]["record_sha256"] = "0" * 64
    else:
        records[1]["previous_record_sha256"] = "0" * 64
    with pytest.raises(runner.N31CrashPairCampaignError):
        runner.validate_campaign_ledger(plan, records)


def _archive() -> Any:
    return importlib.import_module(
        "experiments.adaptive.kauri_experiment.profiled_fault_archive"
    )


def _write_json_lines(path: Path, values: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(_canonical(value) for value in values))


def _event(
    source_kind: str,
    source_id: str,
    sequence: int,
    timestamp_ns: int,
    event_type: str,
    payload: Mapping[str, object],
) -> dict[str, object]:
    return {
        "event_schema_version": 1,
        "run_id": "opaque-child",
        "source_kind": source_kind,
        "source_id": source_id,
        "source_instance": f"opaque-{source_id}-instance",
        "source_sequence": sequence,
        "source_monotonic_ns": timestamp_ns,
        "event_type": event_type,
        "payload": dict(payload),
    }


def _observation_id(
    target: int,
    block_hash: str,
    epoch_digest: str = EPOCH_DIGESTS[1],
) -> str:
    payload = b"".join(
        (
            b"kauri-response-observation-v1",
            (30).to_bytes(2, "big"),
            target.to_bytes(2, "big"),
            (1).to_bytes(4, "big"),
            (0).to_bytes(4, "big"),
            bytes.fromhex(epoch_digest),
            bytes.fromhex(block_hash),
            (1).to_bytes(1, "big"),
        )
    )
    return hashlib.sha256(payload).hexdigest()


def _command_payload(decoded: Any, block_height: int) -> dict[str, object]:
    block_hash = f"{block_height:064x}"
    return {
        "command_block_height": block_height,
        "command_block_hash": block_hash,
        "payload_digest": decoded.command.payload_digest,
        "predecessor_epoch_number": decoded.epoch_number - 1,
        "predecessor_epoch_digest": decoded.previous_epoch_digest,
        "successor_epoch_number": decoded.epoch_number,
        "successor_epoch_digest": decoded.epoch_digest,
        "activation_delay_blocks": decoded.command.activation_delay_blocks,
        "activation_height": block_height + decoded.command.activation_delay_blocks,
    }


def _fault_receipt() -> dict[str, object]:
    plan, records, outcomes, journal = pair_fixture._fault_evidence()
    return {
        "schema_version": 1,
        "fault_plan": plan,
        "process_records": records,
        "sigkill_outcomes": outcomes,
        "fault_journal": journal,
    }


def _raw_child_events(slot: Mapping[str, Any]) -> list[dict[str, object]]:
    _, epoch1, _, epoch2 = pair_fixture._native_epoch_chain()
    pair_ordinal = int(slot["pair_ordinal"])
    desired_milli_tps = (
        100_000
        if slot["arm"] == "control"
        else (110_000, 105_000, 98_000, 120_000, 90_000)[pair_ordinal - 1]
    )
    transactions = (500, 400, 450, desired_milli_tps // 200)
    specifications: list[tuple[str, str, int, str, Mapping[str, object]]] = []

    def add(
        source_kind: str,
        source_id: str,
        timestamp_ns: int,
        event_type: str,
        payload: Mapping[str, object],
    ) -> None:
        specifications.append(
            (source_kind, source_id, timestamp_ns, event_type, payload)
        )

    for sequence, (timestamp, transaction_count) in enumerate(
        zip((1, 6, 11, 16), transactions),
        start=1,
    ):
        epoch = 0 if sequence < 3 else 1
        if sequence == 4 and slot["arm"] == "adaptive":
            epoch = 2
        block_hash = f"{sequence:064x}"
        digest = (EPOCH_DIGESTS[0], epoch1.epoch_digest, epoch2.epoch_digest)[
            epoch
        ]
        add(
            "replica",
            "replica-0",
            timestamp * 1_000_000_000,
            "block.committed",
            {
                "block_height": sequence,
                "block_hash": block_hash,
                "parent_hash": None if sequence == 1 else f"{sequence - 1:064x}",
                "transaction_count": transaction_count,
                "commit_batch_index": 0,
                "designated_observer": True,
                "decision_proof": {
                    "epoch_number": epoch,
                    "tree_id": 0,
                    "epoch_digest": digest,
                    "block_hash": block_hash,
                },
                "view_generation": 1,
            },
        )

    common_commit = {
        "block_height": 3,
        "block_hash": f"{3:064x}",
        "parent_hash": f"{2:064x}",
        "transaction_count": 450,
        "commit_batch_index": 0,
    }
    for replica in pair_fixture.SURVIVORS[: pair_fixture.Q]:
        add(
            "replica",
            f"replica-{replica}",
            10_500_000_000 + replica,
            "block.commit_observed",
            common_commit,
        )

    epoch1_command = _command_payload(epoch1, 4)
    epoch2_command = _command_payload(epoch2, 11)
    for replica in pair_fixture.SURVIVORS:
        add(
            "replica",
            f"replica-{replica}",
            9_000_000_000 + replica,
            "epoch.command_committed",
            epoch1_command,
        )
        add(
            "replica",
            f"replica-{replica}",
            10_000_000_000 + replica,
            "epoch.activated",
            {
                "epoch_number": 1,
                "tree_id": 0,
                "epoch_digest": epoch1.epoch_digest,
                "activation_height": 9,
            },
        )
        if slot["arm"] == "adaptive":
            add(
                "replica",
                f"replica-{replica}",
                14_000_000_000 + replica,
                "epoch.command_committed",
                epoch2_command,
            )
            add(
                "replica",
                f"replica-{replica}",
                15_000_000_000 + replica,
                "epoch.activated",
                {
                    "epoch_number": 2,
                    "tree_id": 0,
                    "epoch_digest": epoch2.epoch_digest,
                    "activation_height": 16,
                },
            )

    for target in range(31):
        eligible = target not in {22, 23, 24}
        block_hash = "9" * 64
        add(
            "adaptation_manager",
            "adaptive-manager",
            12_000_000_000 + target,
            "evidence.observation_accepted",
            {
                "ingestion_sequence": target + 1,
                "observation": {
                    "schema_version": 1,
                    "observation_id": _observation_id(
                        target,
                        block_hash,
                        epoch1.epoch_digest,
                    ),
                    "reporter_id": 30,
                    "observed_replica_id": target,
                    "configuration": {
                        "epoch_number": 1,
                        "tree_id": 0,
                        "epoch_digest": epoch1.epoch_digest,
                    },
                    "block_hash": block_hash,
                    "expected_message_type": "direct_vote",
                    "outcome": "on_time" if eligible else "timeout",
                    "response_duration_us": (
                        pair_fixture._response_latency(target)
                        if eligible
                        else 0
                    ),
                    "deadline_duration_us": 1_000,
                    "reporter_monotonic_ns": 11_000_000_000 + target,
                    "reporter_sequence": target + 1,
                    "signer_set": [target] if eligible else [],
                },
            },
        )

    ordered = sorted(specifications, key=lambda item: (item[2], item[1]))
    source_sequences: dict[str, int] = {}
    events: list[dict[str, object]] = []
    for source_kind, source_id, timestamp_ns, event_type, payload in ordered:
        source_sequences[source_id] = source_sequences.get(source_id, 0) + 1
        events.append(
            _event(
                source_kind,
                source_id,
                source_sequences[source_id],
                timestamp_ns,
                event_type,
                payload,
            )
        )
    child_nonce = hashlib.sha256(
        f"opaque-child-{slot['execution_ordinal']}".encode("ascii")
    ).hexdigest()[:16]
    for event in events:
        event["run_id"] = f"opaque-child-{child_nonce}"
        event["source_instance"] = (
            f"opaque-{event['source_id']}-{child_nonce}"
        )
    return events


def _load_raw_events(run_directory: Path) -> list[dict[str, object]]:
    lines = (run_directory / "raw/events.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def _reconstruct_raw_evidence(
    run_directory: Path,
    *,
    verify_seal: bool,
    classify_fault_receipt: bool,
) -> dict[str, object]:
    seal = (
        _archive().verify_evidence_seal(run_directory)
        if verify_seal
        else None
    )
    issuer_public_key = (run_directory / "raw/issuer-public-key.txt").read_text().strip()
    epoch1_wire = (run_directory / "raw/epoch1.bundle").read_bytes()
    epoch2_path = run_directory / "raw/epoch2.bundle"
    epoch2_wire = epoch2_path.read_bytes() if epoch2_path.exists() else None
    epoch1 = pair_fixture.factorial_validation.decode_epoch_change_bundle(
        epoch1_wire,
        issuer_public_key=issuer_public_key,
    )
    epoch2 = (
        pair_fixture.factorial_validation.decode_epoch_change_bundle(
            epoch2_wire,
            issuer_public_key=issuer_public_key,
        )
        if epoch2_wire is not None
        else None
    )
    assert epoch1.epoch_number == 1
    assert len(epoch1.trees) == 21
    if epoch2 is not None:
        assert epoch2.epoch_number == 2
        assert epoch2.previous_epoch_digest == epoch1.epoch_digest
        assert len(epoch2.trees) == 21
    for decoded in (epoch1,) if epoch2 is None else (epoch1, epoch2):
        assert tuple(tree.tree_id for tree in decoded.trees) == tuple(range(21))
        assert all(tree.fanout == 5 for tree in decoded.trees)
        assert all(tree.pipeline_stretch == 2 for tree in decoded.trees)
        assert all(len(tree.members) == 31 for tree in decoded.trees)

    events = _load_raw_events(run_directory)
    forbidden = {
        "slot_id",
        "pair_id",
        "pair_seed",
        "arm",
        "ground_truth",
        "expected_outcome",
        "outcome_valid",
        "verdict",
    }
    assert forbidden.isdisjoint(_nested_keys(events))

    by_source: dict[tuple[str, str, str], list[dict[str, object]]] = {}
    for event in events:
        source = (
            str(event["source_kind"]),
            str(event["source_id"]),
            str(event["source_instance"]),
        )
        by_source.setdefault(source, []).append(event)
    for source_events in by_source.values():
        ordered = sorted(source_events, key=lambda event: int(event["source_sequence"]))
        assert [event["source_sequence"] for event in ordered] == list(
            range(1, len(ordered) + 1)
        )

    commits = [event for event in events if event["event_type"] == "block.committed"]
    assert len(commits) == 4
    hashes_by_height: dict[int, set[str]] = {}
    for event in commits:
        payload = event["payload"]
        assert isinstance(payload, dict)
        assert payload["designated_observer"] is True
        assert payload["view_generation"] == 1
        assert payload["commit_batch_index"] == 0
        decision = payload["decision_proof"]
        assert isinstance(decision, dict)
        assert decision["block_hash"] == payload["block_hash"]
        hashes_by_height.setdefault(int(payload["block_height"]), set()).add(
            str(payload["block_hash"])
        )
    assert all(len(hashes) == 1 for hashes in hashes_by_height.values())

    authoritative = next(
        event
        for event in commits
        if event["payload"]["block_height"] == 3  # type: ignore[index]
    )
    authoritative_payload = authoritative["payload"]
    assert isinstance(authoritative_payload, dict)
    common_fields = {
        key: authoritative_payload[key]
        for key in (
            "block_height",
            "block_hash",
            "parent_hash",
            "transaction_count",
            "commit_batch_index",
        )
    }
    observed = [
        event
        for event in events
        if event["event_type"] == "block.commit_observed"
    ]
    assert len(observed) == len(epoch1.trees) == 21
    assert len({event["source_id"] for event in observed}) == 21
    assert all(event["payload"] == common_fields for event in observed)

    activations = [
        event for event in events if event["event_type"] == "epoch.activated"
    ]
    commands = [
        event
        for event in events
        if event["event_type"] == "epoch.command_committed"
    ]
    epoch1_activations = [
        event
        for event in activations
        if event["payload"]["epoch_number"] == 1  # type: ignore[index]
    ]
    epoch2_activations = [
        event
        for event in activations
        if event["payload"]["epoch_number"] == 2  # type: ignore[index]
    ]
    epoch1_commands = [
        event
        for event in commands
        if event["payload"]["successor_epoch_number"] == 1  # type: ignore[index]
    ]
    epoch2_commands = [
        event
        for event in commands
        if event["payload"]["successor_epoch_number"] == 2  # type: ignore[index]
    ]
    survivor_sources = {event["source_id"] for event in epoch1_activations}
    assert len(survivor_sources) == 28
    assert {event["source_id"] for event in epoch1_commands} == survivor_sources
    assert len(epoch1_commands) == len(epoch1_activations) == 28
    expected_epoch1_command = _command_payload(epoch1, 4)
    expected_epoch2_command = (
        _command_payload(epoch2, 11) if epoch2 is not None else None
    )
    assert all(event["payload"] == expected_epoch1_command for event in epoch1_commands)
    assert all(
        event["payload"]
        == {
            "epoch_number": 1,
            "tree_id": 0,
            "epoch_digest": epoch1.epoch_digest,
            "activation_height": expected_epoch1_command["activation_height"],
        }
        for event in epoch1_activations
    )
    assert len(epoch2_commands) == len(epoch2_activations)
    assert len(epoch2_commands) == (28 if epoch2 is not None else 0)
    if epoch2_commands:
        assert expected_epoch2_command is not None
        assert {event["source_id"] for event in epoch2_commands} == survivor_sources
        assert {event["source_id"] for event in epoch2_activations} == survivor_sources
        assert all(event["payload"] == expected_epoch2_command for event in epoch2_commands)
        assert all(
            event["payload"]
            == {
                "epoch_number": 2,
                "tree_id": 0,
                "epoch_digest": epoch2.epoch_digest,
                "activation_height": expected_epoch2_command["activation_height"],
            }
            for event in epoch2_activations
        )
    assert {event["source_id"] for event in observed}.issubset(survivor_sources)

    observations = [
        event
        for event in events
        if event["event_type"] == "evidence.observation_accepted"
    ]
    assert len(observations) == 31
    ranked: list[tuple[int, int]] = []
    observation_ids: list[str] = []
    for event in observations:
        payload = event["payload"]
        assert isinstance(payload, dict)
        observation = payload["observation"]
        assert isinstance(observation, dict)
        target = int(observation["observed_replica_id"])
        assert observation["configuration"]["epoch_digest"] == epoch1.epoch_digest
        expected_id = _observation_id(
            target,
            str(observation["block_hash"]),
            epoch1.epoch_digest,
        )
        assert observation["observation_id"] == expected_id
        observation_ids.append(expected_id)
        if observation["outcome"] == "on_time":
            ranked.append((int(observation["response_duration_us"]), target))
    ranked_ids = [target for _, target in sorted(ranked)]
    assert len(ranked_ids) == 28

    fault_receipt_sha256: str | None = None
    if classify_fault_receipt:
        receipt_path = run_directory / "raw/fault-receipt.json"
        receipt = json.loads(receipt_path.read_bytes())
        assert set(receipt) == {
            "schema_version",
            "fault_plan",
            "process_records",
            "sigkill_outcomes",
            "fault_journal",
        }
        assert receipt["schema_version"] == 1
        plan = receipt["fault_plan"]
        records = receipt["process_records"]
        outcomes = receipt["sigkill_outcomes"]
        journal = receipt["fault_journal"]
        plan_sha256 = hashlib.sha256(_canonical(plan).rstrip(b"\n")).hexdigest()
        targets = {action["replica_id"] for action in plan["actions"]}
        fault_ids = {action["fault_id"] for action in plan["actions"]}
        records_by_replica = {record["replica_id"]: record for record in records}
        assert set(records_by_replica) == targets
        assert len({record["pid"] for record in records}) == len(targets)
        assert len({record["pgid"] for record in records}) == len(targets)
        assert {outcome["fault_id"] for outcome in outcomes} == fault_ids
        assert all(outcome["signal_number"] == 9 for outcome in outcomes)
        assert all(outcome["returncode"] == -9 for outcome in outcomes)
        for outcome in outcomes:
            record = records_by_replica[outcome["replica_id"]]
            assert outcome["pid"] == record["pid"]
            assert outcome["pgid"] == record["pgid"]
        assert max(outcome["requested_monotonic_ns"] for outcome in outcomes) < min(
            outcome["confirmed_monotonic_ns"] for outcome in outcomes
        )
        assert len(journal) == 2 * len(fault_ids)
        assert all(event["plan_sha256"] == plan_sha256 for event in journal)
        assert {event["fault_id"] for event in journal} == fault_ids
        assert {
            (event["fault_id"], event["lifecycle"])
            for event in journal
        } == {
            (fault_id, lifecycle)
            for fault_id in fault_ids
            for lifecycle in ("started", "terminal")
        }
        membership = set(epoch1.trees[0].members)
        survivors = {
            int(str(source).removeprefix("replica-"))
            for source in survivor_sources
        }
        timeout_targets = {
            int(event["payload"]["observation"]["observed_replica_id"])  # type: ignore[index]
            for event in observations
            if event["payload"]["observation"]["outcome"] == "timeout"  # type: ignore[index]
        }
        assert targets == membership - survivors == timeout_targets
        assert len(targets) == 3
        internal_prefix = (len(membership) - 2) // epoch1.trees[0].fanout + 1
        for tree in epoch1.trees:
            assert targets.issubset(tree.wait_exempt)
            assert all(tree.members.index(target) >= internal_prefix for target in targets)
        if epoch2 is not None:
            assert tuple(tree.members[0] for tree in epoch2.trees) == tuple(
                ranked_ids[: len(epoch2.trees)]
            )
        fault_receipt_sha256 = _sha(receipt)

    phase_names = ("baseline", "fault", "epoch1", "late")
    phase_rows: list[dict[str, object]] = []
    for phase_index, phase in enumerate(phase_names):
        start_ns = phase_index * 5_000_000_000
        end_ns = start_ns + 5_000_000_000
        phase_commits = [
            event
            for event in commits
            if start_ns <= int(event["source_monotonic_ns"]) < end_ns
        ]
        transaction_count = sum(
            int(event["payload"]["transaction_count"])  # type: ignore[index]
            for event in phase_commits
        )
        phase_rows.append(
            {
                "phase": phase,
                "start_ns": start_ns,
                "end_ns": end_ns,
                "transactions": transaction_count,
                "mean_milli_tps": transaction_count * 200,
            }
        )
    assert all(row["transactions"] for row in phase_rows)

    source_inventory = [list(source) for source in sorted(by_source)]
    commit_identity = [
        {
            "source_id": event["source_id"],
            "source_instance": event["source_instance"],
            "source_sequence": event["source_sequence"],
            "payload": event["payload"],
        }
        for event in commits
    ]
    epoch_identity = {
        "epoch1_bundle_sha256": hashlib.sha256(epoch1_wire).hexdigest(),
        "epoch2_bundle_sha256": (
            hashlib.sha256(epoch2_wire).hexdigest()
            if epoch2_wire is not None
            else None
        ),
        "issuer_public_key_sha256": hashlib.sha256(
            bytes.fromhex(issuer_public_key)
        ).hexdigest(),
        "commands": [event["payload"] for event in commands],
        "activations": [event["payload"] for event in activations],
    }
    ranking_identity = {
        "source_epoch_digest": epoch1.epoch_digest,
        "observation_ids": sorted(observation_ids),
        "ranked_eligible_replica_ids": ranked_ids,
    }
    return {
        "evidence_tree_sha256": None if seal is None else seal.tree_sha256,
        "evidence_seal_sha256": None if seal is None else seal.seal_sha256,
        "source_inventory_sha256": _sha(source_inventory),
        "authoritative_commit_identity_sha256": _sha(commit_identity),
        "epoch_identity_sha256": _sha(epoch_identity),
        "ranking_identity_sha256": _sha(ranking_identity),
        "fault_receipt_sha256": fault_receipt_sha256,
        "fault_receipt_joined": classify_fault_receipt,
        "native_bundles_decoded": True,
        "runtime_graph_validated": True,
        "epoch2_present": epoch2 is not None,
        "scientific_measurements": {
            "phases": phase_rows,
            "late_window_throughput_milli_tps": phase_rows[-1][
                "mean_milli_tps"
            ],
        },
    }


def _children(
    plan: Mapping[str, Any], tmp_path: Path
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for slot in plan["slots"]:
        run_directory = tmp_path / f"neutral-{slot['execution_ordinal']:02d}"
        epoch1_wire, _, epoch2_wire, _ = pair_fixture._native_epoch_chain()
        _write_json_lines(
            run_directory / "raw/events.jsonl",
            _raw_child_events(slot),
        )
        (run_directory / "raw/epoch1.bundle").write_bytes(epoch1_wire)
        if slot["arm"] == "adaptive":
            (run_directory / "raw/epoch2.bundle").write_bytes(epoch2_wire)
        (run_directory / "raw/issuer-public-key.txt").write_text(
            pair_fixture.ISSUER_PUBLIC_KEY + "\n"
        )
        (run_directory / "raw/fault-receipt.json").write_bytes(
            _canonical(_fault_receipt())
        )
        expected_raw_files = [
            "raw/epoch1.bundle",
            "raw/events.jsonl",
            "raw/fault-receipt.json",
            "raw/issuer-public-key.txt",
        ]
        if slot["arm"] == "adaptive":
            expected_raw_files.append("raw/epoch2.bundle")
        assert sorted(
            str(path.relative_to(run_directory))
            for path in run_directory.rglob("*")
            if path.is_file()
        ) == sorted(expected_raw_files)
        seal = _archive().create_evidence_seal(run_directory)
        assert sorted(
            str(path.relative_to(run_directory))
            for path in run_directory.rglob("*")
            if path.is_file()
        ) == ["evidence-seal.json", *sorted(expected_raw_files)]
        reconstructed = _reconstruct_raw_evidence(
            run_directory,
            verify_seal=True,
            classify_fault_receipt=False,
        )
        result.append(
            {
                "slot_id": slot["slot_id"],
                "pair_id": slot["pair_id"],
                "arm": slot["arm"],
                "child_tree_sha256": seal.tree_sha256,
                "child_seal_sha256": seal.seal_sha256,
                "sealed_child_directory": run_directory,
                "source_inventory_sha256": reconstructed[
                    "source_inventory_sha256"
                ],
                "authoritative_commit_identity_sha256": reconstructed[
                    "authoritative_commit_identity_sha256"
                ],
                "epoch_identity_sha256": reconstructed[
                    "epoch_identity_sha256"
                ],
                "ranking_identity_sha256": reconstructed[
                    "ranking_identity_sha256"
                ],
            }
        )
    return result


def _nested_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(
            *(_nested_keys(child) for child in value.values())
        )
    if isinstance(value, list):
        return set().union(*(_nested_keys(child) for child in value))
    return set()


def _blind_classifier(
    run_directory: Path,
    *,
    trusted_provenance: object,
    override_identity_field: str | None = None,
) -> dict[str, object]:
    assert trusted_provenance is TRUSTED_PROVENANCE
    assert run_directory.name.startswith("opaque-")
    reconstructed = _reconstruct_raw_evidence(
        run_directory,
        verify_seal=True,
        classify_fault_receipt=True,
    )
    result = {
        "outcome": "PASS",
        "integrity_valid": True,
        "claim_slot": True,
        "child": {
            "path": str(run_directory),
            "run_id": run_directory.name,
            "evidence_tree_sha256": reconstructed["evidence_tree_sha256"],
            "evidence_seal_sha256": reconstructed["evidence_seal_sha256"],
        },
        "source_inventory_sha256": reconstructed["source_inventory_sha256"],
        "authoritative_commit_identity_sha256": reconstructed[
            "authoritative_commit_identity_sha256"
        ],
        "epoch_identity_sha256": reconstructed["epoch_identity_sha256"],
        "ranking_identity_sha256": reconstructed["ranking_identity_sha256"],
        "fault_receipt_sha256": reconstructed["fault_receipt_sha256"],
        "fault_receipt_joined": reconstructed["fault_receipt_joined"],
        "native_bundles_decoded": reconstructed["native_bundles_decoded"],
        "runtime_graph_validated": reconstructed["runtime_graph_validated"],
        "epoch2_present": reconstructed["epoch2_present"],
        "scientific_measurements": reconstructed["scientific_measurements"],
        "reconstructed_from_raw_evidence": True,
    }
    if override_identity_field is not None:
        result[override_identity_field] = "0" * 64
    return result


TRUSTED_PROVENANCE = object()


def test_source_blind_campaign_joins_ten_valid_children_into_five_pair_effects(
    tmp_path: Path,
) -> None:
    runner = _runner()
    plan = _plan(runner)
    children = _children(plan, tmp_path)
    ledger = _ledger(plan)
    for record, child in zip(ledger, children, strict=True):
        record["child_tree_sha256"] = child["child_tree_sha256"]
        record["child_seal_sha256"] = child["child_seal_sha256"]
    _rehash_ledger(plan, ledger)
    classified_paths: list[Path] = []

    def classify(
        run_directory: Path, *, trusted_provenance: object
    ) -> dict[str, object]:
        classified_paths.append(run_directory)
        return _blind_classifier(
            run_directory,
            trusted_provenance=trusted_provenance,
        )

    summary = _document(
        runner.validate_campaign_source_blind(
            plan,
            children,
            ledger_records=ledger,
            validate_child=classify,
            trusted_provenance=TRUSTED_PROVENANCE,
        )
    )
    assert len(classified_paths) == 10
    assert all(path.parent != tmp_path for path in classified_paths)
    assert summary["source_blind"] is True
    assert summary["validated_claim_slot_count"] == 10
    assert summary["campaign_acceptance"] == "ACCEPTED"
    assert summary["figure_eligible"] is True
    assert len(summary["child_verdicts"]) == 10
    assert all(
        {
            "child_tree_sha256",
            "child_seal_sha256",
            "source_inventory_sha256",
            "authoritative_commit_identity_sha256",
            "epoch_identity_sha256",
            "ranking_identity_sha256",
            "fault_receipt_sha256",
            "arm",
            "epoch2_present",
        }
        <= set(child)
        for child in summary["child_verdicts"]
    )
    assert all(
        child["fault_receipt_joined"] is True
        and child["native_bundles_decoded"] is True
        and child["runtime_graph_validated"] is True
        for child in summary["child_verdicts"]
    )
    assert all(
        child["epoch2_present"] is (child["arm"] == "adaptive")
        for child in summary["child_verdicts"]
    )
    assert len(summary["pair_verdicts"]) == 5
    assert [pair["effect_milli_tps"] for pair in summary["pair_verdicts"]] == [
        10_000,
        5_000,
        -2_000,
        20_000,
        -10_000,
    ]
    assert any(
        pair["scientific_outcome"] == "UNFAVORABLE"
        for pair in summary["pair_verdicts"]
    )


@pytest.mark.parametrize(
    "mutation",
    (
        "relabel",
        "fabricate",
        "source-binding",
        "commit-binding",
        "epoch-binding",
        "ranking-binding",
    ),
)
def test_source_blind_campaign_rejects_relabelled_or_unbound_results(
    mutation: str,
    tmp_path: Path,
) -> None:
    runner = _runner()
    plan = _plan(runner)
    children = _children(plan, tmp_path)
    if mutation == "relabel":
        children[0]["sealed_child_directory"], children[1]["sealed_child_directory"] = (
            children[1]["sealed_child_directory"],
            children[0]["sealed_child_directory"],
        )
    elif mutation == "fabricate":
        path = children[0]["sealed_child_directory"] / "raw/events.jsonl"  # type: ignore[operator]
        events = [json.loads(line) for line in path.read_text().splitlines()]
        commit = next(
            event for event in events if event["event_type"] == "block.committed"
        )
        commit["payload"]["transaction_count"] += 1
        _write_json_lines(path, events)

    def classify(
        run_directory: Path, *, trusted_provenance: object
    ) -> dict[str, object]:
        identity_field = {
            "source-binding": "source_inventory_sha256",
            "commit-binding": "authoritative_commit_identity_sha256",
            "epoch-binding": "epoch_identity_sha256",
            "ranking-binding": "ranking_identity_sha256",
        }.get(mutation)
        return _blind_classifier(
            run_directory,
            trusted_provenance=trusted_provenance,
            override_identity_field=identity_field,
        )

    with pytest.raises(runner.N31CrashPairCampaignError):
        runner.validate_campaign_source_blind(
            plan,
            children,
            validate_child=classify,
            trusted_provenance=TRUSTED_PROVENANCE,
        )


def test_source_blind_consumes_terminal_ledger_and_unique_child_seals(
    tmp_path: Path,
) -> None:
    runner = _runner()
    plan = _plan(runner)
    children = _children(plan, tmp_path)

    def classify(
        run_directory: Path, *, trusted_provenance: object
    ) -> dict[str, object]:
        return _blind_classifier(
            run_directory,
            trusted_provenance=trusted_provenance,
        )

    def bound_ledger(rows: list[dict[str, object]]) -> list[dict[str, object]]:
        records = _ledger(plan)
        for record, child in zip(records, rows, strict=True):
            record["child_tree_sha256"] = child["child_tree_sha256"]
            record["child_seal_sha256"] = child["child_seal_sha256"]
        _rehash_ledger(plan, records)
        return records

    ledger = bound_ledger(children)
    baseline = _document(
        runner.validate_campaign_source_blind(
            plan,
            children,
            ledger_records=ledger,
            validate_child=classify,
            trusted_provenance=TRUSTED_PROVENANCE,
        )
    )
    assert baseline["campaign_acceptance"] == "ACCEPTED"
    assert baseline["ledger_head_sha256"] == ledger[-1]["record_sha256"]
    assert len(
        {
            (child["child_tree_sha256"], child["child_seal_sha256"])
            for child in children
        }
    ) == 10

    unexpectedly_accepted: list[str] = []
    by_arm = {
        arm: next(child for child in children if child["arm"] == arm)
        for arm in ("control", "adaptive")
    }
    replayed: list[dict[str, object]] = []
    for slot in plan["slots"]:
        child = deepcopy(by_arm[str(slot["arm"])])
        child.update(
            slot_id=slot["slot_id"],
            pair_id=slot["pair_id"],
            arm=slot["arm"],
        )
        replayed.append(child)

    mutations = {
        "two-child-replay": (replayed, bound_ledger(replayed)),
        "ledger-tree-mismatch": (deepcopy(children), deepcopy(ledger)),
        "duplicate-slot": (
            [*deepcopy(children[:-1]), deepcopy(children[0])],
            deepcopy(ledger),
        ),
    }
    mutations["ledger-tree-mismatch"][1][0]["child_tree_sha256"] = "0" * 64
    _rehash_ledger(plan, mutations["ledger-tree-mismatch"][1])
    for name, (changed_children, changed_ledger) in mutations.items():
        try:
            runner.validate_campaign_source_blind(
                plan,
                changed_children,
                ledger_records=changed_ledger,
                validate_child=classify,
                trusted_provenance=TRUSTED_PROVENANCE,
            )
        except runner.N31CrashPairCampaignError:
            continue
        unexpectedly_accepted.append(name)

    assert unexpectedly_accepted == []


def test_source_blind_rejects_omitted_terminal_ledger(tmp_path: Path) -> None:
    runner = _runner()
    plan = _plan(runner)
    children = _children(plan, tmp_path)
    ledger = _ledger(plan)
    for record, child in zip(ledger, children, strict=True):
        record["child_tree_sha256"] = child["child_tree_sha256"]
        record["child_seal_sha256"] = child["child_seal_sha256"]
    _rehash_ledger(plan, ledger)

    def classify(
        run_directory: Path, *, trusted_provenance: object
    ) -> dict[str, object]:
        return _blind_classifier(
            run_directory,
            trusted_provenance=trusted_provenance,
        )

    baseline = _document(
        runner.validate_campaign_source_blind(
            plan,
            children,
            ledger_records=ledger,
            validate_child=classify,
            trusted_provenance=TRUSTED_PROVENANCE,
        )
    )
    assert baseline["campaign_acceptance"] == "ACCEPTED"
    assert baseline["figure_eligible"] is True

    try:
        summary = _document(
            runner.validate_campaign_source_blind(
                plan,
                children,
                validate_child=classify,
                trusted_provenance=TRUSTED_PROVENANCE,
            )
        )
    except runner.N31CrashPairCampaignError:
        return
    assert summary["campaign_acceptance"] != "ACCEPTED"
    assert summary["figure_eligible"] is False
