"""Executable specification for the prospective W16 v8 marker exception.

This file deliberately contains no production validator.  It fixes the
*narrow* admission predicate that a future, separately-versioned v8 validator
would have to implement after the stopped v7 cell-5 evidence.  In particular,
the observer's ``block.committed`` event remains the sole source of throughput
timing and transaction count; the marker and non-observer rich commits only
establish whether one reporter-local identity gap is admissible.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib
import json
from pathlib import Path

import pytest

from experiments.adaptive.kauri_experiment.w16_output_validator import (
    validate_w16_output_v7,
)
from experiments.adaptive.kauri_experiment import n31_static_e0_feasibility as feasibility
from experiments.adaptive.kauri_experiment import static_e0_cpu_contract
from experiments.adaptive.kauri_experiment import w16_output_validator
from experiments.adaptive.tests.test_w16_cpu_campaign import _campaign_root_v3
from experiments.adaptive.tests.test_w16_output_validator import _reseal


_OBSERVER = 27
_REPLICAS = frozenset(range(31))
_DIGEST = "a" * 64
_PHYSICAL_KEYS = frozenset({
    "block_height", "block_hash", "parent_hash", "transaction_count", "commit_batch_index",
})
_COMMITTED_KEYS = _PHYSICAL_KEYS | frozenset({
    "designated_observer", "decision_proof", "view_generation",
})


def _physical() -> dict[str, object]:
    return {
        "block_height": 700,
        "block_hash": hashlib.sha256(b"v8-marker-block").hexdigest(),
        "parent_hash": hashlib.sha256(b"v8-marker-parent").hexdigest(),
        "transaction_count": 1000,
        "commit_batch_index": 0,
    }


def _proof(physical: dict[str, object]) -> dict[str, object]:
    return {
        "epoch_number": 0,
        "tree_id": 7,
        "epoch_digest": _DIGEST,
        "block_hash": physical["block_hash"],
    }


def _event(replica: int, sequence: int, event_type: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "event_schema_version": 1,
        "source_kind": "replica",
        "source_id": f"replica-{replica}",
        "source_sequence": sequence,
        "event_type": event_type,
        "payload": payload,
    }


def _traces() -> dict[int, list[dict[str, object]]]:
    """One exact physical commit: r0 lacks local identity, all others have it."""

    physical = _physical()
    proof = _proof(physical)
    traces: dict[int, list[dict[str, object]]] = {}
    for replica in range(31):
        events = [_event(replica, 1, "block.commit_observed", deepcopy(physical))]
        if replica == _OBSERVER:
            events.append(_event(replica, 2, "block.committed", {
                **deepcopy(physical),
                "designated_observer": True,
                "decision_proof": deepcopy(proof),
                "view_generation": 701,
            }))
        elif replica == 0:
            events.append(_event(replica, 2, "block.commit_identity_unavailable", {
                **deepcopy(physical),
                "reason": "no_authenticated_exact_identity_source",
                "convergence_identity_pending": False,
            }))
        else:
            events.append(_event(replica, 2, "block.committed", {
                **deepcopy(physical),
                "designated_observer": False,
                "decision_proof": deepcopy(proof),
                "view_generation": 701,
            }))
        traces[replica] = events
    return traces


def _physical_key(payload: object) -> tuple[object, ...] | None:
    if not isinstance(payload, dict) or not _PHYSICAL_KEYS.issubset(payload):
        return None
    return tuple(payload[key] for key in (
        "block_height", "block_hash", "parent_hash", "transaction_count", "commit_batch_index",
    ))


def _admit_one_marker(traces: dict[int, list[dict[str, object]]], *, digest: str) -> bool:
    """Reference-only v8 predicate; this is not wired into any validator."""

    if set(traces) != _REPLICAS:
        return False
    markers = [
        (replica, index, event)
        for replica, events in traces.items()
        for index, event in enumerate(events)
        if event.get("event_type") == "block.commit_identity_unavailable"
    ]
    if len(markers) != 1:
        return False
    reporter, marker_index, marker = markers[0]
    marker_payload = marker.get("payload")
    physical = _physical_key(marker_payload)
    if (
        reporter == _OBSERVER
        or not isinstance(marker_payload, dict)
        or set(marker_payload) != {
            "block_height", "block_hash", "parent_hash", "transaction_count", "commit_batch_index",
            "reason", "convergence_identity_pending",
        }
        or marker_payload.get("reason") != "no_authenticated_exact_identity_source"
        or marker_payload.get("convergence_identity_pending") is not False
        or physical is None
        or marker_index == 0
        or traces[reporter][marker_index - 1].get("event_type") != "block.commit_observed"
        or _physical_key(traces[reporter][marker_index - 1].get("payload")) != physical
    ):
        return False

    observations: dict[int, int] = {}
    for replica, events in traces.items():
        observations[replica] = sum(
            event.get("event_type") == "block.commit_observed"
            and isinstance(event.get("payload"), dict)
            and set(event["payload"]) == _PHYSICAL_KEYS
            and _physical_key(event["payload"]) == physical
            for event in events
        )
    if set(observations) != _REPLICAS or any(count != 1 for count in observations.values()):
        return False

    observer_commits = [
        event.get("payload") for event in traces[_OBSERVER]
        if event.get("event_type") == "block.committed"
        and _physical_key(event.get("payload")) == physical
    ]
    if len(observer_commits) != 1 or not isinstance(observer_commits[0], dict):
        return False
    observer_commit = observer_commits[0]
    proof = observer_commit.get("decision_proof")
    if (
        set(observer_commit) != _COMMITTED_KEYS
        or observer_commit.get("designated_observer") is not True
        or type(observer_commit.get("view_generation")) is not int
        or observer_commit["view_generation"] <= 0
        or not isinstance(proof, dict)
        or set(proof) != {"epoch_number", "tree_id", "epoch_digest", "block_hash"}
        or proof.get("epoch_number") != 0
        or type(proof.get("tree_id")) is not int or not 0 <= proof["tree_id"] < 21
        or proof.get("epoch_digest") != digest
        or proof.get("block_hash") != physical[1]
    ):
        return False

    rich_replicas: set[int] = {_OBSERVER}
    for replica, events in traces.items():
        rich_commits = [
            event.get("payload") for event in events
            if event.get("event_type") == "block.committed"
            and _physical_key(event.get("payload")) == physical
        ]
        if replica == reporter:
            if rich_commits:
                return False
            continue
        if replica == _OBSERVER:
            continue
        if len(rich_commits) != 1 or not isinstance(rich_commits[0], dict):
            return False
        rich = rich_commits[0]
        if (
            set(rich) != _COMMITTED_KEYS
            or rich.get("designated_observer") is not False
            or rich.get("decision_proof") != proof
            or rich.get("view_generation") != observer_commit["view_generation"]
        ):
            return False
        rich_replicas.add(replica)
    return rich_replicas == _REPLICAS - {reporter}


def _v8(root: Path) -> dict[str, object]:
    """Call the public v8 entry point; absence is a test failure."""

    module = importlib.import_module(
        "experiments.adaptive.kauri_experiment.w16_output_validator"
    )
    validator = getattr(module, "validate_w16_output_v8")
    assert callable(validator)
    result = validator(root)
    assert isinstance(result, dict)
    return result


def _reseal_after_raw_mutation(root: Path) -> None:
    _reseal(root)


def _bind_v8_authorization(root: Path) -> None:
    """Bind the unchanged v3 receipt to a distinct v8 authorization byte string."""

    authorization_path = root / "authorization.json"
    authorization = json.loads(authorization_path.read_text(encoding="utf-8"))
    authorization["cell_validator_version"] = 8
    authorization_path.write_text(
        json.dumps(authorization, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    receipt_path = root / "feasibility-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["authorization_sha256"] = hashlib.sha256(authorization_path.read_bytes()).hexdigest()
    receipt_path.write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    _reseal_after_raw_mutation(root)


def _move_to_fresh_v8_campaign_slot(root: Path) -> Path:
    """Relocate a synthetic v3 cell into the v8-only campaign namespace."""

    old_root = root
    campaign_id = "w16-cpu-repeat-v8-marker-spec"
    new_root = (
        old_root.parent / campaign_id / "block-01" / "slow-roots-heterogeneous"
    )
    new_root.parent.mkdir(parents=True)
    old_root.rename(new_root)
    old_text = str(old_root)
    new_text = str(new_root)
    for path in (new_root / "config").glob("*"):
        if path.is_file():
            path.write_text(path.read_text(encoding="utf-8").replace(old_text, new_text), encoding="utf-8")

    authorization_path = new_root / "authorization.json"
    authorization = json.loads(authorization_path.read_text(encoding="utf-8"))
    authorization.update({
        "campaign_id": campaign_id,
        "block_id": f"{campaign_id}-block-01",
        "block_index": 1,
        "block_order": [
            "slow-roots:homogeneous", "fast-roots:homogeneous",
            "slow-roots:heterogeneous", "fast-roots:heterogeneous",
        ],
        "cell_ordinal": 3,
        "output_root": str(new_root),
        "cell_validator_version": 8,
        "authoritative_observer": _OBSERVER,
    })
    authorization_path.write_text(
        json.dumps(authorization, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    receipt_path = new_root / "feasibility-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    arm = str(receipt["preflight"]["arm"])
    plan = feasibility.frozen_plan(arm=arm, profile_version=8)
    receipt["preflight"].update({
        "schema_version": 8,
        "kind": feasibility.SCHEMA_V8,
        "profile_id": plan.profile.profile_id,
        "profile_sha256": plan.profile.sha256,
    })
    authorization["profile_sha256"] = plan.profile.sha256
    authorization["preflight_sha256"] = hashlib.sha256(
        feasibility.canonical_json(receipt["preflight"])
    ).hexdigest()
    authorization_path.write_text(
        json.dumps(authorization, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    contract = static_e0_cpu_contract.frozen_contract(plan, str(authorization["quota_mode"]))
    contract_path = new_root / "runtime/cpu-quota-contract.json"
    contract_path.write_text(
        json.dumps(w16_output_validator._contract_document(contract), sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    receipt["quota_contract_sha256"] = contract.contract_sha256
    launch_path = new_root / "runtime/cpu-quota-launch.json"
    launch = json.loads(launch_path.read_text(encoding="utf-8"))
    launch["contract_sha256"] = contract.contract_sha256
    launch_path.write_text(json.dumps(launch, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    # Copy the complete old observer trace to replica 27.  All committed
    # records retained on replica 2 become non-authoritative; the profile and
    # every replica config now bind only replica 27 as throughput observer.
    old_observer = new_root / "raw/replica-2.jsonl"
    new_observer = new_root / f"raw/replica-{_OBSERVER}.jsonl"
    old_rows = [json.loads(line) for line in old_observer.read_text().splitlines()]
    clone_rows = deepcopy(old_rows)
    run_id = str(receipt["run_id"])
    for rows, replica, authoritative in ((old_rows, 2, False), (clone_rows, _OBSERVER, True)):
        for row in rows:
            row["source_id"] = f"replica-{replica}"
            row["source_instance"] = f"{run_id}-replica-{replica}"
            if row["event_type"] == "adaptive.configuration_active":
                row["payload"]["observer_replica"] = replica
            if row["event_type"] == "block.committed":
                row["payload"]["designated_observer"] = authoritative
        for sequence, row in enumerate(rows, 1):
            row["source_sequence"] = sequence
    old_observer.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in old_rows), encoding="utf-8")
    new_observer.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in clone_rows), encoding="utf-8")
    for path in (new_root / "config").glob("replica-*.conf"):
        replica = int(path.stem.removeprefix("replica-"))
        rewritten = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("structured-event-source-instance = "):
                line = f"structured-event-source-instance = {run_id}-replica-{replica}"
            elif line.startswith("structured-event-commit-observer-id = "):
                line = f"structured-event-commit-observer-id = replica-{_OBSERVER}"
            elif line.startswith("structured-event-commit-observer-instance = "):
                line = f"structured-event-commit-observer-instance = {run_id}-replica-{_OBSERVER}"
            elif line.startswith("structured-event-output = "):
                line = f"structured-event-output = {new_root / 'raw' / f'replica-{replica}.jsonl'}"
            rewritten.append(line)
        path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    receipt["authorization_sha256"] = hashlib.sha256(authorization_path.read_bytes()).hexdigest()
    receipt_path.write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    _reseal_after_raw_mutation(new_root)
    return new_root


def _pair_existing_observer_commits(root: Path) -> None:
    """Make every synthetic observer rich commit follow its own exact observation."""

    for replica in (2, _OBSERVER):
        path = root / f"raw/replica-{replica}.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        paired: list[dict[str, object]] = []
        for index, row in enumerate(rows):
            if row["event_type"] == "block.committed":
                physical = {key: row["payload"][key] for key in ("block_height", "block_hash", "parent_hash", "transaction_count", "commit_batch_index")}
                already_paired = index > 0 and rows[index - 1]["event_type"] == "block.commit_observed" and rows[index - 1]["payload"] == physical
                if not already_paired:
                    paired.append({**row, "source_sequence": 0, "source_monotonic_ns": int(row["source_monotonic_ns"]) - 1, "event_type": "block.commit_observed", "payload": physical})
            paired.append(row)
        for sequence, row in enumerate(paired, 1): row["source_sequence"] = sequence
        path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in paired), encoding="utf-8")


def _append_v8_identity_graph(root: Path, *, commit_index: int = 0) -> None:
    """Add one marker graph around one real observer commit in a sealed v3 root."""

    _pair_existing_observer_commits(root)
    observer_path = root / f"raw/replica-{_OBSERVER}.jsonl"
    observer_rows = [json.loads(line) for line in observer_path.read_text().splitlines()]
    committed = [
        row["payload"] for row in observer_rows if row["event_type"] == "block.committed"
    ][commit_index]
    physical = {
        key: committed[key] for key in (
            "block_height", "block_hash", "parent_hash", "transaction_count", "commit_batch_index",
        )
    }
    proof = committed["decision_proof"]
    generation = committed["view_generation"]
    for replica in range(31):
        path = root / "raw" / f"replica-{replica}.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        if replica == 2:
            # This synthetic base already has the matching non-authoritative
            # rich record, paired above; do not duplicate its physical height.
            continue
        insert_at = next(index for index, row in enumerate(rows) if row["event_type"] == "process.stopping")
        template = rows[0]
        additions: list[dict[str, object]] = []
        if replica != _OBSERVER:
            additions.append({
                **template, "source_sequence": 0,
                "source_monotonic_ns": int(rows[insert_at - 1]["source_monotonic_ns"]) + 1,
                "event_type": "block.commit_observed", "payload": deepcopy(physical),
            })
        if replica == 0:
            additions.append({
                **template, "source_sequence": 0,
                "source_monotonic_ns": int(rows[insert_at - 1]["source_monotonic_ns"]) + 2,
                "event_type": "block.commit_identity_unavailable",
                "payload": {
                    **deepcopy(physical),
                    "reason": "no_authenticated_exact_identity_source",
                    "convergence_identity_pending": False,
                },
            })
        elif replica != _OBSERVER:
            additions.append({
                **template, "source_sequence": 0,
                "source_monotonic_ns": int(rows[insert_at - 1]["source_monotonic_ns"]) + 2,
                "event_type": "block.committed",
                "payload": {
                    **deepcopy(physical), "designated_observer": False,
                    "decision_proof": deepcopy(proof),
                    "view_generation": generation,
                },
            })
        rows[insert_at:insert_at] = additions
        for sequence, row in enumerate(rows, 1):
            row["source_sequence"] = sequence
        path.write_text(
            "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
            encoding="utf-8",
        )
    _reseal_after_raw_mutation(root)


def _v8_root(tmp_path: Path) -> Path:
    root = _campaign_root_v3(tmp_path)
    root = _move_to_fresh_v8_campaign_slot(root)
    _append_v8_identity_graph(root)
    return root


def _append_unrelated_record(root: Path, *, event_type: str, same_height: bool) -> None:
    """Append one syntactically valid record outside the admitted marker graph."""

    marker_rows = [json.loads(line) for line in (root / "raw/replica-0.jsonl").read_text().splitlines()]
    marker = next(row["payload"] for row in marker_rows if row["event_type"] == "block.commit_identity_unavailable")
    peer_path = root / "raw/replica-1.jsonl"
    rows = [json.loads(line) for line in peer_path.read_text().splitlines()]
    rich = next(row["payload"] for row in rows if row["event_type"] == "block.committed")
    physical = {
        "block_height": marker["block_height"] if same_height else marker["block_height"] + 500,
        "block_hash": hashlib.sha256(f"unrelated-{event_type}-{same_height}".encode()).hexdigest(),
        "parent_hash": hashlib.sha256(f"unrelated-parent-{event_type}-{same_height}".encode()).hexdigest(),
        "transaction_count": marker["transaction_count"],
        "commit_batch_index": marker["commit_batch_index"],
    }
    if event_type == "block.commit_observed":
        payload = physical
    else:
        proof = deepcopy(rich["decision_proof"])
        proof["block_hash"] = physical["block_hash"]
        payload = {
            **physical, "designated_observer": False,
            "decision_proof": proof, "view_generation": rich["view_generation"],
        }
    insert_at = next(index for index, row in enumerate(rows) if row["event_type"] == "process.stopping")
    rows.insert(insert_at, {
        **rows[0], "source_sequence": 0,
        "source_monotonic_ns": int(rows[insert_at - 1]["source_monotonic_ns"]) + 1,
        "event_type": event_type, "payload": payload,
    })
    for sequence, row in enumerate(rows, 1):
        row["source_sequence"] = sequence
    peer_path.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def _append_complete_peer_only_pair(root: Path, *, same_height: bool) -> None:
    """Add a syntactically complete non-observer pair absent from replica 2."""

    marker_rows = [json.loads(line) for line in (root / "raw/replica-0.jsonl").read_text().splitlines()]
    marker = next(row["payload"] for row in marker_rows if row["event_type"] == "block.commit_identity_unavailable")
    peer_path = root / "raw/replica-1.jsonl"
    rows = [json.loads(line) for line in peer_path.read_text().splitlines()]
    rich = next(row["payload"] for row in rows if row["event_type"] == "block.committed")
    physical = {
        "block_height": marker["block_height"] if same_height else marker["block_height"] + 500,
        "block_hash": hashlib.sha256(f"peer-only-{same_height}".encode()).hexdigest(),
        "parent_hash": hashlib.sha256(f"peer-only-parent-{same_height}".encode()).hexdigest(),
        "transaction_count": marker["transaction_count"],
        "commit_batch_index": marker["commit_batch_index"],
    }
    proof = deepcopy(rich["decision_proof"])
    proof["block_hash"] = physical["block_hash"]
    insert_at = next(index for index, row in enumerate(rows) if row["event_type"] == "process.stopping")
    prior_time = int(rows[insert_at - 1]["source_monotonic_ns"])
    rows[insert_at:insert_at] = [
        {
            **rows[0], "source_sequence": 0, "source_monotonic_ns": prior_time + 1,
            "event_type": "block.commit_observed", "payload": physical,
        },
        {
            **rows[0], "source_sequence": 0, "source_monotonic_ns": prior_time + 2,
            "event_type": "block.committed",
            "payload": {
                **physical, "designated_observer": False,
                "decision_proof": proof, "view_generation": rich["view_generation"],
            },
        },
    ]
    for sequence, row in enumerate(rows, 1):
        row["source_sequence"] = sequence
    peer_path.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def _append_post_measurement_peer_tail(
    root: Path, *, replicas: tuple[int, ...] = (0, 1), length: int = 1,
) -> tuple[int, int, int]:
    """Append a shared, observer-excluded A-X/B-X style tail before stopping."""

    baseline = _v8(root)
    end_ns = baseline["throughput"]["window_end_monotonic_ns"]
    observer_path = root / "raw" / f"replica-{_OBSERVER}.jsonl"
    observer_rows = [json.loads(line) for line in observer_path.read_text().splitlines()]
    final = [row["payload"] for row in observer_rows if row["event_type"] == "block.committed"][-1]
    assert isinstance(end_ns, int) and isinstance(final, dict)
    final_height, final_hash = final["block_height"], final["block_hash"]

    parent = final_hash
    for offset in range(1, length + 1):
        height = final_height + offset
        block_hash = hashlib.sha256(f"post-tail-{height}".encode()).hexdigest()
        for replica in replicas:
            path = root / "raw" / f"replica-{replica}.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            insert_at = next(index for index, row in enumerate(rows) if row["event_type"] == "process.stopping")
            stop_ns = int(rows[insert_at]["source_monotonic_ns"])
            previous_ns = int(rows[insert_at - 1]["source_monotonic_ns"])
            if stop_ns <= int(end_ns) + 2 * length + 2:
                # The base fixture's non-observer lifecycle ends before its
                # designated observer's five-cycle window.  Move only its
                # terminal lifecycle markers within the already broad RAW
                # receipt span, so this test can model a real post-window
                # peer extension without changing the observer's rate data.
                stop_ns = int(end_ns) + 2 * length + 3
                rows[insert_at]["source_monotonic_ns"] = stop_ns
                rows[insert_at + 1]["source_monotonic_ns"] = stop_ns + 1
            observed_ns = max(int(end_ns) + 1, previous_ns + 1)
            assert observed_ns + 1 < stop_ns
            physical = {
                "block_height": height, "block_hash": block_hash, "parent_hash": parent,
                "transaction_count": final["transaction_count"],
                # Batch indexes deliberately remain reporter-local.
                "commit_batch_index": final["commit_batch_index"] + replica,
            }
            proof = deepcopy(final["decision_proof"])
            proof["block_hash"] = block_hash
            rows[insert_at:insert_at] = [
                {**rows[0], "source_sequence": 0, "source_monotonic_ns": observed_ns,
                 "event_type": "block.commit_observed", "payload": deepcopy(physical)},
                {**rows[0], "source_sequence": 0, "source_monotonic_ns": observed_ns + 1,
                 "event_type": "block.committed", "payload": {
                     **physical, "designated_observer": False,
                     "decision_proof": proof, "view_generation": final["view_generation"],
                 }},
            ]
            for sequence, row in enumerate(rows, 1):
                row["source_sequence"] = sequence
            _write_peer_rows(path, rows)
        parent = block_hash
    return final_height, int(end_ns), int(baseline["throughput"]["transaction_count"])


def test_v8_marker_contract_admits_only_the_observed_single_reporter_gap() -> None:
    assert _admit_one_marker(_traces(), digest=_DIGEST)


def test_v8_observed_only_bridge_is_unanimous_pre_measurement_and_parent_bound() -> None:
    """An observed-only rich-commit height gap is disclosed, never inferred."""

    module = importlib.import_module("experiments.adaptive.kauri_experiment.w16_output_validator")
    def physical(height: int, parent: str) -> dict[str, object]:
        block_hash = hashlib.sha256(f"bridge-{height}".encode()).hexdigest()
        return {"block_height": height, "block_hash": block_hash, "parent_hash": parent,
                "transaction_count": 1000, "commit_batch_index": 0}
    h1 = physical(1, hashlib.sha256(b"genesis").hexdigest())
    h2 = physical(2, str(h1["block_hash"]))
    h3 = physical(3, str(h2["block_hash"]))
    records: list[tuple[int, dict[str, object]]] = []
    observer_events: list[dict[str, object]] = []
    for replica in range(31):
        rows: list[dict[str, object]] = []
        for sequence, kind, payload in (
            (1, "block.commit_observed", h1),
            (2, "block.committed", {**h1, "designated_observer": replica == _OBSERVER, "decision_proof": _proof(h1), "view_generation": 1}),
            (3, "block.commit_observed", h2),
            (4, "block.commit_observed", h3),
            (5, "block.committed", {**h3, "designated_observer": replica == _OBSERVER, "decision_proof": _proof(h3), "view_generation": 3}),
        ):
            event = _event(replica, sequence, kind, deepcopy(payload))
            event["source_monotonic_ns"] = sequence
            rows.append(event); records.append((replica, event))
        if replica == _OBSERVER:
            observer_events = rows
    accepted = module._validate_identity_gaps_v8(
        records, observer_events, digest=_DIGEST, start_ns=100, end_ns=200,
        observer_replica=_OBSERVER,
    )
    assert accepted["unanimous_observed_bridges"] == [{
        "block_height": 2, "block_hash": h2["block_hash"], "parent_hash": h2["parent_hash"],
        "transaction_count": 1000, "observed_replica_count": 31,
        "reason": "unknown_no_rich_disposition",
    }]

    def rejected(mutator) -> None:
        changed = deepcopy(records); changed_observer = deepcopy(observer_events)
        mutator(changed, changed_observer)
        with pytest.raises(module._InvalidEvidence):
            module._validate_identity_gaps_v8(changed, changed_observer, digest=_DIGEST,
                start_ns=100, end_ns=200, observer_replica=_OBSERVER)

    rejected(lambda rows, _observer: rows.__delitem__(next(i for i, (replica, event) in enumerate(rows) if replica == 30 and event["source_sequence"] == 3)))
    rejected(lambda rows, _observer: rows[next(i for i, (replica, event) in enumerate(rows) if replica == 30 and event["source_sequence"] == 3)][1]["payload"].__setitem__("block_hash", "f" * 64))
    rejected(lambda rows, _observer: rows[next(i for i, (replica, event) in enumerate(rows) if replica == 30 and event["source_sequence"] == 3)][1].__setitem__("source_monotonic_ns", 100))
    rejected(lambda rows, _observer: rows.insert(3, (_OBSERVER, {**deepcopy(rows[2][1]), "event_type": "block.committed", "payload": {**deepcopy(h2), "designated_observer": True, "decision_proof": _proof(h2), "view_generation": 2}})))
    rejected(lambda _rows, observer: observer[2]["payload"].__setitem__("parent_hash", "e" * 64))


def test_v8_accepts_a_premeasurement_prefix_observed_only_bridge() -> None:
    """A-X shape: height 1 is observed-only; height 2 links to it richly."""

    module = importlib.import_module("experiments.adaptive.kauri_experiment.w16_output_validator")
    first_hash = hashlib.sha256(b"prefix-bridge-1").hexdigest()
    second_hash = hashlib.sha256(b"prefix-bridge-2").hexdigest()
    first = {"block_height": 1, "block_hash": first_hash,
             "parent_hash": hashlib.sha256(b"prefix-genesis").hexdigest(),
             "transaction_count": 1000, "commit_batch_index": 0}
    second = {"block_height": 2, "block_hash": second_hash,
              "parent_hash": first_hash, "transaction_count": 1000,
              "commit_batch_index": 0}
    records: list[tuple[int, dict[str, object]]] = []
    observer_events: list[dict[str, object]] = []
    for replica in range(31):
        rows = []
        for sequence, kind, payload in (
            (1, "block.commit_observed", first),
            (2, "block.commit_observed", second),
            (3, "block.committed", {**second, "designated_observer": replica == _OBSERVER,
                                      "decision_proof": _proof(second), "view_generation": 2}),
        ):
            event = _event(replica, sequence, kind, deepcopy(payload))
            event["source_monotonic_ns"] = sequence
            rows.append(event); records.append((replica, event))
        if replica == _OBSERVER:
            observer_events = rows
    result = module._validate_identity_gaps_v8(
        records, observer_events, digest=_DIGEST, start_ns=100, end_ns=200,
        observer_replica=_OBSERVER,
    )
    assert result["unanimous_observed_bridges"] == [{
        "block_height": 1, "block_hash": first_hash, "parent_hash": first["parent_hash"],
        "transaction_count": 1000, "observed_replica_count": 31,
        "reason": "unknown_no_rich_disposition",
    }]

    bad_observer = deepcopy(observer_events)
    bad_observer[1]["payload"]["parent_hash"] = "f" * 64
    with pytest.raises(module._InvalidEvidence):
        module._validate_identity_gaps_v8(
            records, bad_observer, digest=_DIGEST, start_ns=100, end_ns=200,
            observer_replica=_OBSERVER,
        )


@pytest.mark.parametrize("mutation", ("reason", "pending", "observer", "not-immediate"))
def test_v8_marker_contract_rejects_marker_schema_or_local_observation_drift(mutation: str) -> None:
    traces = _traces()
    marker = traces[0][1]
    if mutation == "reason":
        marker["payload"]["reason"] = "anything_else"
    elif mutation == "pending":
        marker["payload"]["convergence_identity_pending"] = True
    elif mutation == "observer":
        traces[_OBSERVER][1] = marker
        del traces[0][1]
    else:
        traces[0].insert(1, _event(0, 2, "noise", {}))
    assert not _admit_one_marker(traces, digest=_DIGEST)


@pytest.mark.parametrize("mutation", ("missing-observer", "missing-observation", "missing-rich", "drifted-proof"))
def test_v8_marker_contract_rejects_missing_or_nonmatching_global_evidence(mutation: str) -> None:
    traces = _traces()
    if mutation == "missing-observer":
        traces[_OBSERVER] = [traces[_OBSERVER][0]]
    elif mutation == "missing-observation":
        traces[30] = [traces[30][1]]
    elif mutation == "missing-rich":
        traces[30] = [traces[30][0]]
    else:
        traces[30][1]["payload"]["decision_proof"]["tree_id"] = 8
    assert not _admit_one_marker(traces, digest=_DIGEST)


def test_v8_admits_exact_cell5_style_marker_without_changing_observer_throughput(tmp_path: Path) -> None:
    root = _campaign_root_v3(tmp_path)
    baseline = validate_w16_output_v7(root)
    assert baseline["verdict"] == "PASS", baseline
    root = _move_to_fresh_v8_campaign_slot(root)
    _append_v8_identity_graph(root)

    result = _v8(root)
    assert result["verdict"] == "PASS", result
    assert result["throughput"] == baseline["throughput"]


def test_v8_admits_multiple_distinct_marker_commits_when_each_has_its_own_full_graph(tmp_path: Path) -> None:
    root = _v8_root(tmp_path)
    _append_v8_identity_graph(root, commit_index=1)

    result = _v8(root)
    assert result["verdict"] == "PASS", result
    assert result["identity_unavailable"]["total_count"] == 2


@pytest.mark.parametrize(
    "mutation",
    (
        "malformed", "observer-marker", "two-reporters", "same-reporter-rich",
        "missing-rich", "wrong-proof", "generation-drift", "rich-observer-flag",
        "duplicate-observation", "mismatched-observation", "observer-chain-break",
    ),
)
def test_v8_rejects_marker_admission_drift(tmp_path: Path, mutation: str) -> None:
    root = _v8_root(tmp_path)
    path = root / "raw/replica-0.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    marker = next(row for row in rows if row["event_type"] == "block.commit_identity_unavailable")
    if mutation == "malformed":
        marker["payload"].pop("reason")
    elif mutation == "observer-marker":
        rows.remove(marker)
        observer_path = root / "raw/replica-2.jsonl"
        observer_rows = [json.loads(line) for line in observer_path.read_text().splitlines()]
        observed_at = next(
            index for index, row in enumerate(observer_rows)
            if row["event_type"] == "block.commit_observed"
            and row["payload"]["block_hash"] == marker["payload"]["block_hash"]
        )
        observer_rows.insert(observed_at + 1, {
            **observer_rows[0], "source_sequence": 0, "source_monotonic_ns": 0,
            "event_type": "block.commit_identity_unavailable", "payload": deepcopy(marker["payload"]),
        })
        for sequence, row in enumerate(observer_rows, 1):
            row["source_sequence"] = sequence
            row["source_monotonic_ns"] = 1_000_000_000 + sequence * 10_000_000
        observer_path.write_text(
            "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in observer_rows),
            encoding="utf-8",
        )
    elif mutation == "two-reporters":
        peer_path = root / "raw/replica-1.jsonl"
        peer_rows = [json.loads(line) for line in peer_path.read_text().splitlines()]
        observed_at = next(
            index for index, row in enumerate(peer_rows)
            if row["event_type"] == "block.commit_observed"
            and row["payload"]["block_hash"] == marker["payload"]["block_hash"]
        )
        peer_rows.insert(observed_at + 1, {
            **peer_rows[0], "source_sequence": 0, "source_monotonic_ns": 0,
            "event_type": "block.commit_identity_unavailable", "payload": deepcopy(marker["payload"]),
        })
        for sequence, row in enumerate(peer_rows, 1):
            row["source_sequence"] = sequence
            row["source_monotonic_ns"] = 1_000_000_000 + sequence * 10_000_000
        peer_path.write_text(
            "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in peer_rows),
            encoding="utf-8",
        )
    elif mutation == "same-reporter-rich":
        peer_rows = [json.loads(line) for line in (root / "raw/replica-1.jsonl").read_text().splitlines()]
        rich_payload = next(row["payload"] for row in peer_rows if row["event_type"] == "block.committed")
        rows.insert(rows.index(marker) + 1, {
            **rows[0], "source_sequence": 0, "source_monotonic_ns": 0,
            "event_type": "block.committed", "payload": deepcopy(rich_payload),
        })
    elif mutation == "missing-rich":
        peer_path = root / "raw/replica-1.jsonl"
        peer_rows = [json.loads(line) for line in peer_path.read_text().splitlines()]
        peer_rows = [row for row in peer_rows if row["event_type"] != "block.committed"]
        peer_path.write_text(
            "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in peer_rows),
            encoding="utf-8",
        )
    elif mutation == "wrong-proof":
        peer_path = root / "raw/replica-1.jsonl"
        peer_rows = [json.loads(line) for line in peer_path.read_text().splitlines()]
        rich = next(row for row in peer_rows if row["event_type"] == "block.committed")
        rich["payload"]["decision_proof"]["tree_id"] += 1
        peer_path.write_text(
            "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in peer_rows),
            encoding="utf-8",
        )
    elif mutation == "generation-drift":
        peer_path = root / "raw/replica-1.jsonl"
        peer_rows = [json.loads(line) for line in peer_path.read_text().splitlines()]
        rich = next(row for row in peer_rows if row["event_type"] == "block.committed")
        rich["payload"]["view_generation"] += 1
        peer_path.write_text(
            "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in peer_rows),
            encoding="utf-8",
        )
    elif mutation == "rich-observer-flag":
        peer_path = root / "raw/replica-1.jsonl"
        peer_rows = [json.loads(line) for line in peer_path.read_text().splitlines()]
        rich = next(row for row in peer_rows if row["event_type"] == "block.committed")
        rich["payload"]["designated_observer"] = True
        peer_path.write_text(
            "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in peer_rows),
            encoding="utf-8",
        )
    else:
        if mutation == "duplicate-observation":
            peer_path = root / "raw/replica-30.jsonl"
            peer_rows = [json.loads(line) for line in peer_path.read_text().splitlines()]
            observed = next(row for row in peer_rows if row["event_type"] == "block.commit_observed")
            peer_rows.insert(peer_rows.index(observed) + 1, deepcopy(observed))
            for sequence, row in enumerate(peer_rows, 1):
                row["source_sequence"] = sequence
                row["source_monotonic_ns"] = 1_000_000_000 + sequence * 10_000_000
            peer_path.write_text(
                "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in peer_rows),
                encoding="utf-8",
            )
        elif mutation == "mismatched-observation":
            peer_path = root / "raw/replica-30.jsonl"
            peer_rows = [json.loads(line) for line in peer_path.read_text().splitlines()]
            observed = next(row for row in peer_rows if row["event_type"] == "block.commit_observed")
            observed["payload"]["block_hash"] = "f" * 64
            peer_path.write_text(
                "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in peer_rows),
                encoding="utf-8",
            )
        else:
            observer_path = root / "raw/replica-2.jsonl"
            observer_rows = [json.loads(line) for line in observer_path.read_text().splitlines()]
            commits = [row for row in observer_rows if row["event_type"] == "block.committed"]
            commits[-1]["payload"]["parent_hash"] = "f" * 64
            observer_path.write_text(
                "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in observer_rows),
                encoding="utf-8",
            )
    for sequence, row in enumerate(rows, 1):
        row["source_sequence"] = sequence
        row["source_monotonic_ns"] = 1_000_000_000 + sequence * 10_000_000
    path.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    _reseal_after_raw_mutation(root)

    result = _v8(root)
    assert result["verdict"] != "PASS", result


def test_v8_rejects_the_old_v7_authorization_even_with_an_exact_marker_graph(tmp_path: Path) -> None:
    root = _campaign_root_v3(tmp_path)
    # Relabelling the old slot is insufficient: v8 requires a fresh campaign
    # namespace and the corresponding output path, not merely an auth version.
    _bind_v8_authorization(root)

    result = _v8(root)
    assert result["verdict"] != "PASS", result


@pytest.mark.parametrize(
    "target, field, value",
    (
        ("marker", "block_height", True),
        ("marker", "transaction_count", 1.0),
        ("marker", "commit_batch_index", -1),
        ("marker", "block_height", 1 << 64),
        ("rich", "view_generation", True),
        ("rich", "view_generation", 1.0),
        ("rich", "view_generation", -1),
        ("rich", "view_generation", 1 << 64),
        ("rich-proof", "tree_id", True),
        ("rich-proof", "tree_id", 1.0),
        ("rich-proof", "tree_id", -1),
        ("rich-proof", "tree_id", 1 << 64),
    ),
)
def test_v8_rejects_noncanonical_numeric_marker_graph_fields(
    tmp_path: Path, target: str, field: str, value: object,
) -> None:
    root = _v8_root(tmp_path)
    path = root / ("raw/replica-0.jsonl" if target == "marker" else "raw/replica-1.jsonl")
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    event_type = "block.commit_identity_unavailable" if target == "marker" else "block.committed"
    row = next(row for row in rows if row["event_type"] == event_type)
    payload = row["payload"]
    assert isinstance(payload, dict)
    if target == "rich-proof":
        proof = payload["decision_proof"]
        assert isinstance(proof, dict)
        proof[field] = value
    else:
        payload[field] = value
    path.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    _reseal_after_raw_mutation(root)

    result = _v8(root)
    assert result["verdict"] != "PASS", result


@pytest.mark.parametrize("field", ("block_hash", "parent_hash", "transaction_count", "commit_batch_index"))
def test_v8_rejects_marker_physical_identity_drift(tmp_path: Path, field: str) -> None:
    root = _v8_root(tmp_path)
    path = root / "raw/replica-0.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    marker = next(row for row in rows if row["event_type"] == "block.commit_identity_unavailable")
    payload = marker["payload"]
    assert isinstance(payload, dict)
    payload[field] = (
        hashlib.sha256(f"wrong-{field}".encode()).hexdigest()
        if field.endswith("hash") else int(payload[field]) + 1
    )
    path.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    _reseal_after_raw_mutation(root)

    result = _v8(root)
    assert result["verdict"] != "PASS", result


@pytest.mark.parametrize("mutation", ("bad-reason", "pending", "extra-field"))
def test_v8_rejects_marker_schema_and_semantic_drift(tmp_path: Path, mutation: str) -> None:
    root = _v8_root(tmp_path)
    path = root / "raw/replica-0.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    marker = next(row for row in rows if row["event_type"] == "block.commit_identity_unavailable")
    payload = marker["payload"]
    assert isinstance(payload, dict)
    if mutation == "bad-reason":
        payload["reason"] = "unknown"
    elif mutation == "pending":
        payload["convergence_identity_pending"] = True
    else:
        payload["unbound"] = "must-not-be-accepted"
    path.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    _reseal_after_raw_mutation(root)

    result = _v8(root)
    assert result["verdict"] != "PASS", result


def test_v8_rejects_raw_tampering_without_receipt_reseal(tmp_path: Path) -> None:
    root = _v8_root(tmp_path)
    path = root / "raw/replica-0.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    marker = next(row for row in rows if row["event_type"] == "block.commit_identity_unavailable")
    marker["payload"]["reason"] = "tampered-after-seal"
    path.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )

    result = _v8(root)
    assert result["verdict"] != "PASS", result


def test_v8_rejects_duplicate_json_keys_in_raw_event(tmp_path: Path) -> None:
    root = _v8_root(tmp_path)
    path = root / "raw/replica-0.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    marker_at = next(index for index, line in enumerate(lines) if "block.commit_identity_unavailable" in line)
    # The v8 parser is intentionally duplicate-key strict, before the raw
    # inventory binding is considered; the receipt is resealed to reach that gate.
    lines[marker_at] = lines[marker_at].replace(
        '"reason":"no_authenticated_exact_identity_source"',
        '"reason":"no_authenticated_exact_identity_source","reason":"no_authenticated_exact_identity_source"',
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _reseal_after_raw_mutation(root)

    result = _v8(root)
    assert result["verdict"] != "PASS", result


@pytest.mark.parametrize(
    "event_type, same_height",
    (
        ("block.commit_observed", False),
        ("block.committed", False),
        ("block.commit_observed", True),
        ("block.committed", True),
    ),
)
def test_v8_rejects_orphan_or_same_height_divergent_records_outside_the_marker_graph(
    tmp_path: Path, event_type: str, same_height: bool,
) -> None:
    root = _v8_root(tmp_path)
    _append_unrelated_record(root, event_type=event_type, same_height=same_height)
    _reseal_after_raw_mutation(root)

    result = _v8(root)
    assert result["verdict"] != "PASS", result


@pytest.mark.parametrize("same_height", (False, True))
def test_v8_rejects_complete_peer_only_commit_pairs_absent_from_the_observer(
    tmp_path: Path, same_height: bool,
) -> None:
    root = _v8_root(tmp_path)
    _append_complete_peer_only_pair(root, same_height=same_height)
    _reseal_after_raw_mutation(root)

    result = _v8(root)
    assert result["verdict"] != "PASS", result


def test_v8_admits_and_discloses_a_strictly_post_measurement_peer_tail(tmp_path: Path) -> None:
    root = _v8_root(tmp_path)
    final_height, _end_ns, baseline_transactions = _append_post_measurement_peer_tail(root, replicas=(0, 1), length=2)
    _reseal_after_raw_mutation(root)

    result = _v8(root)
    assert result["verdict"] == "PASS", result
    assert result["throughput"]["transaction_count"] == baseline_transactions
    tail = result["post_measurement_peer_tails"]
    assert tail["count"] == 2
    assert tail["reporters"] == [0, 1]
    assert [detail["block_height"] for detail in tail["details"]] == [final_height + 1, final_height + 2]
    assert all(detail["reporters"] == [0, 1] for detail in tail["details"])


@pytest.mark.parametrize("mutation", ("in-window", "wrong-parent", "gap", "conflicting-peer", "conflicting-proof", "unpaired"))
def test_v8_rejects_a_nonconforming_post_measurement_peer_tail(tmp_path: Path, mutation: str) -> None:
    root = _v8_root(tmp_path)
    final_height, end_ns, _baseline_transactions = _append_post_measurement_peer_tail(root, replicas=(0, 1))
    path = root / "raw/replica-0.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    observed_at = next(index for index, row in enumerate(rows)
                       if row["event_type"] == "block.commit_observed"
                       and row["payload"]["block_height"] == final_height + 1)
    if mutation == "in-window":
        rows[observed_at]["source_monotonic_ns"] = end_ns
        rows[observed_at + 1]["source_monotonic_ns"] = end_ns
    elif mutation == "wrong-parent":
        for row in rows[observed_at:observed_at + 2]:
            row["payload"]["parent_hash"] = hashlib.sha256(b"wrong-tail-parent").hexdigest()
    elif mutation == "gap":
        for row in rows[observed_at:observed_at + 2]:
            row["payload"]["block_height"] = final_height + 2
    elif mutation == "unpaired":
        rows.pop(observed_at + 1)
    elif mutation in {"conflicting-peer", "conflicting-proof"}:
        peer_path = root / "raw/replica-1.jsonl"
        peer_rows = [json.loads(line) for line in peer_path.read_text().splitlines()]
        peer_at = next(index for index, row in enumerate(peer_rows)
                       if row["event_type"] == "block.commit_observed"
                       and row["payload"]["block_height"] == final_height + 1)
        if mutation == "conflicting-peer":
            changed_hash = hashlib.sha256(b"conflicting-tail-hash").hexdigest()
            for row in peer_rows[peer_at:peer_at + 2]:
                row["payload"]["block_hash"] = changed_hash
                if row["event_type"] == "block.committed":
                    row["payload"]["decision_proof"]["block_hash"] = changed_hash
        else:
            peer_rows[peer_at + 1]["payload"]["decision_proof"]["tree_id"] = 0
        _write_peer_rows(peer_path, peer_rows)
    for sequence, row in enumerate(rows, 1):
        row["source_sequence"] = sequence
    _write_peer_rows(path, rows)
    _reseal_after_raw_mutation(root)

    result = _v8(root)
    assert result["verdict"] != "PASS", result


def _marked_peer_pair(root: Path, replica: int = 1) -> tuple[Path, list[dict[str, object]], dict[str, object], dict[str, object]]:
    """Return one peer's exact observed/rich pair for the admitted marker hash."""

    marker_rows = [json.loads(line) for line in (root / "raw/replica-0.jsonl").read_text().splitlines()]
    marker = next(row["payload"] for row in marker_rows if row["event_type"] == "block.commit_identity_unavailable")
    path = root / "raw" / f"replica-{replica}.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    observed_at = next(
        index for index, row in enumerate(rows)
        if row["event_type"] == "block.commit_observed"
        and row["payload"]["block_hash"] == marker["block_hash"]
    )
    observed = rows[observed_at]["payload"]
    rich = rows[observed_at + 1]["payload"]
    assert rows[observed_at + 1]["event_type"] == "block.committed"
    assert isinstance(observed, dict) and isinstance(rich, dict)
    return path, rows, observed, rich


def _write_peer_rows(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_v8_admits_a_peer_local_batch_index_that_differs_from_other_reporters(tmp_path: Path) -> None:
    root = _v8_root(tmp_path)
    path, rows, observed, rich = _marked_peer_pair(root)
    observed["commit_batch_index"] += 1
    rich["commit_batch_index"] += 1
    _write_peer_rows(path, rows)
    _reseal_after_raw_mutation(root)

    result = _v8(root)
    assert result["verdict"] == "PASS", result


@pytest.mark.parametrize("event_kind", ("observed", "rich"))
def test_v8_rejects_a_peer_local_batch_mismatch_within_one_adjacent_pair(
    tmp_path: Path, event_kind: str,
) -> None:
    root = _v8_root(tmp_path)
    path, rows, observed, rich = _marked_peer_pair(root)
    (observed if event_kind == "observed" else rich)["commit_batch_index"] += 1
    _write_peer_rows(path, rows)
    _reseal_after_raw_mutation(root)

    result = _v8(root)
    assert result["verdict"] != "PASS", result


@pytest.mark.parametrize("mutation", ("parent", "transactions", "proof", "generation"))
def test_v8_rejects_cross_peer_identity_or_epoch_identity_drift(
    tmp_path: Path, mutation: str,
) -> None:
    root = _v8_root(tmp_path)
    path, rows, observed, rich = _marked_peer_pair(root)
    if mutation == "parent":
        observed["parent_hash"] = hashlib.sha256(b"cross-peer-parent").hexdigest()
        rich["parent_hash"] = observed["parent_hash"]
    elif mutation == "transactions":
        observed["transaction_count"] += 1
        rich["transaction_count"] += 1
    elif mutation == "proof":
        rich["decision_proof"]["tree_id"] += 1
    else:
        rich["view_generation"] += 1
    _write_peer_rows(path, rows)
    _reseal_after_raw_mutation(root)

    result = _v8(root)
    assert result["verdict"] != "PASS", result


@pytest.mark.parametrize("placement", ("before-ready", "after-stopped"))
def test_v8_rejects_a_marker_outside_its_reporter_lifecycle(tmp_path: Path, placement: str) -> None:
    root = _v8_root(tmp_path)
    path = root / "raw/replica-0.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    marker_at = next(index for index, row in enumerate(rows) if row["event_type"] == "block.commit_identity_unavailable")
    observation = rows.pop(marker_at - 1)
    marker = rows.pop(marker_at - 1)
    if placement == "before-ready":
        insert_at = next(index for index, row in enumerate(rows) if row["event_type"] == "process.ready")
    else:
        insert_at = next(index for index, row in enumerate(rows) if row["event_type"] == "process.stopped") + 1
    rows[insert_at:insert_at] = [observation, marker]
    for sequence, row in enumerate(rows, 1):
        row["source_sequence"] = sequence
        row["source_monotonic_ns"] = 1_000_000_000 + sequence * 10_000_000
    path.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    _reseal_after_raw_mutation(root)

    result = _v8(root)
    assert result["verdict"] != "PASS", result


@pytest.mark.parametrize("placement, expected_phase", (("pre", "pre_measurement"), ("post", "post_measurement")))
def test_v8_records_valid_markers_before_and_after_the_measurement_window(
    tmp_path: Path, placement: str, expected_phase: str,
) -> None:
    root = _v8_root(tmp_path)
    baseline = _v8(root)
    path = root / "raw/replica-0.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    marker_at = next(index for index, row in enumerate(rows) if row["event_type"] == "block.commit_identity_unavailable")
    observation = rows.pop(marker_at - 1)
    marker = rows.pop(marker_at - 1)
    if placement == "pre":
        insert_at = next(index for index, row in enumerate(rows) if row["event_type"] == "process.ready") + 1
        rows[insert_at:insert_at] = [observation, marker]
        for sequence, row in enumerate(rows, 1):
            row["source_sequence"] = sequence
        ready_time = int(rows[insert_at - 1]["source_monotonic_ns"])
        rows[insert_at]["source_monotonic_ns"] = ready_time + 1
        rows[insert_at + 1]["source_monotonic_ns"] = ready_time + 2
    else:
        insert_at = next(index for index, row in enumerate(rows) if row["event_type"] == "process.stopping")
        rows[insert_at:insert_at] = [observation, marker]
        for sequence, row in enumerate(rows, 1):
            row["source_sequence"] = sequence
            row["source_monotonic_ns"] = 1_000_000_000 + sequence * 10_000_000
        post_time = int(baseline["throughput"]["window_end_monotonic_ns"]) + 10_000_000
        for row in rows[insert_at:]:
            row["source_monotonic_ns"] = post_time
            post_time += 10_000_000
    path.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    _reseal_after_raw_mutation(root)

    result = _v8(root)
    assert result["verdict"] == "PASS", result
    assert result["identity_unavailable"]["details"][0]["phase"] == expected_phase


def test_v7_remains_fail_closed_for_the_v8_marker_even_when_it_is_well_formed(tmp_path: Path) -> None:
    """The frozen v7 path must not silently inherit the prospective exception."""

    root = _campaign_root_v3(tmp_path)
    observer_rows = [json.loads(line) for line in (root / "raw/replica-2.jsonl").read_text().splitlines()]
    commit = next(row["payload"] for row in observer_rows if row["event_type"] == "block.committed")
    path = root / "raw/replica-0.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    insert_at = next(index for index, row in enumerate(rows) if row["event_type"] == "process.stopping")
    marker = {
        "block_height": commit["block_height"], "block_hash": commit["block_hash"],
        "parent_hash": commit["parent_hash"], "transaction_count": commit["transaction_count"],
        "commit_batch_index": commit["commit_batch_index"],
        "reason": "no_authenticated_exact_identity_source", "convergence_identity_pending": False,
    }
    rows.insert(insert_at, {
        **rows[0], "source_sequence": 0, "source_monotonic_ns": 0,
        "event_type": "block.commit_identity_unavailable", "payload": marker,
    })
    for sequence, row in enumerate(rows, 1):
        row["source_sequence"] = sequence
        row["source_monotonic_ns"] = 1_000_000_000 + sequence * 10_000_000
    path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")
    _reseal(root)

    result = validate_w16_output_v7(root)
    assert result["verdict"] == "FAIL", result
    assert result["reason_code"] == "unexpected_native_event"
