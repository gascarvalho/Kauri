#!/usr/bin/env python3
"""Fail-closed validation helpers for the prospective N=7 omission study.

The original command-line validator proves only the manager evidence prefix.
``validate_known_raw_events`` checks the existing structured event schemas
but intentionally returns ``PARTIAL_ONLY``. ``validate_raw_bundle`` requires
the producer's hash-bound receipt, approved arm and gate, signed E1 bundle,
and complete cleanup. The new manager-owned selection decision is checked
against the same evidence snapshot and signed E1 bundle, so an observation
about replica 1 alone is never misrepresented as its selection proof.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


HERE = Path(__file__).resolve().parent
RUNNER_PATH = HERE / "runner.py"
spec = importlib.util.spec_from_file_location("n7_three_reporter_runner", RUNNER_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError(f"cannot load runner: {RUNNER_PATH}")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
KAURI_ROOT = HERE.parents[2]
if str(KAURI_ROOT) not in sys.path:
    sys.path.insert(0, str(KAURI_ROOT))
from experiments.adaptive.kauri_experiment import factorial_validation


class ValidationError(ValueError):
    pass


_MAX_RAW_BYTES = 16 * 1024 * 1024
_MAX_SMALL_BYTES = 256 * 1024
_E1_TREE_IDS = tuple(range(5))  # N=7 factory emits Q=5 dissemination trees.
# The profile is a study input, not a caller-selected label.  Keep this pinned
# independently of the mutable execution-plan artifact.
PROFILE_V4_SHA256 = "3e2b2af834279168199db31bd5ee47e0abdef480d1d5327a17cbcc1b57efc244"


def _strict_json(raw: bytes, label: str) -> Any:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValidationError(f"{label} has duplicate JSON key {key!r}")
            result[key] = value
        return result
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{label} is not strict UTF-8 JSON") from exc


def _read_artifact(root: Path, descriptor: object, label: str, limit: int) -> bytes:
    if not isinstance(descriptor, Mapping) or set(descriptor) != {"path", "sha256"}:
        raise ValidationError(f"{label} descriptor has schema drift")
    path, digest = descriptor["path"], descriptor["sha256"]
    if not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts:
        raise ValidationError(f"{label} path is not a safe relative path")
    _hex64(digest, f"{label} sha256")
    candidate = root / path
    if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size > limit:
        raise ValidationError(f"{label} is not a bounded regular file")
    raw = candidate.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValidationError(f"{label} SHA-256 does not match receipt")
    return raw


_HEX = frozenset("0123456789abcdef")
_COMMAND_FIELDS = frozenset(
    {
        "command_block_height",
        "command_block_hash",
        "payload_digest",
        "predecessor_epoch_number",
        "predecessor_epoch_digest",
        "successor_epoch_number",
        "successor_epoch_digest",
        "activation_delay_blocks",
        "activation_height",
    }
)
_ACTIVATION_FIELDS = frozenset(
    {"epoch_number", "tree_id", "epoch_digest", "activation_height"}
)
_COMMIT_FIELDS = frozenset(
    {
        "block_height",
        "block_hash",
        "parent_hash",
        "transaction_count",
        "designated_observer",
        "decision_proof",
        "view_generation",
        "commit_batch_index",
    }
)
_COMMIT_OBSERVED_FIELDS = frozenset(
    {"block_height", "block_hash", "parent_hash", "transaction_count", "commit_batch_index"}
)
_DECISION_PROOF_FIELDS = frozenset(
    {"epoch_number", "tree_id", "epoch_digest", "block_hash"}
)
_EVIDENCE_SNAPSHOT_FIELDS = frozenset(
    {
        "schema_version",
        "cycle_ordinal",
        "policy_intent",
        "transition_artifact_id",
        "predecessor_epoch_number",
        "predecessor_epoch_digest",
        "activation_generation",
        "baseline_cutoff",
        "current_cutoff",
        "full_prefix_snapshot_id",
        "evidence_snapshot_id",
        "accepted_prefix_count",
        "eligible_ranking",
    }
)
_SELECTION_DECIDED_FIELDS = frozenset(
    {
        "schema_version",
        "cycle_ordinal",
        "predecessor_epoch_number",
        "predecessor_epoch_digest",
        "baseline_cutoff",
        "evidence_cutoff",
        "evidence_snapshot_id",
        "snapshot_evidence_basis",
        "selection_cardinality_policy",
        "selected_replicas",
    }
)
_PATH_TIMEOUT_EVIDENCE_BASIS = "exact_post_fault_path_timeout_quorum_v1"
_PATH_TIMEOUT_SELECTION_POLICY = "all_guarded_up_to_fault_bound_v1"


def _hex64(value: object, label: str, *, nonzero: bool = False) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX for character in value)
        or (nonzero and value == "0" * 64)
    ):
        raise ValidationError(f"{label} must be a canonical SHA-256 digest")
    return value


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValidationError(f"{label} must be a positive integer")
    return value


def _attempt_identity(observation: Mapping[str, Any]) -> tuple[object, ...]:
    configuration = observation.get("configuration")
    if not isinstance(configuration, Mapping):
        return (None,)
    return (
        observation.get("reporter_id"),
        observation.get("observed_replica_id"),
        configuration.get("epoch_number"),
        configuration.get("tree_id"),
        configuration.get("epoch_digest"),
        observation.get("block_hash"),
        observation.get("expected_message_type"),
        observation.get("deadline_duration_us"),
    )


def _require_replica_event(value: object, *, run_id: str, replica_id: int) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "event_schema_version", "run_id", "source_kind", "source_id",
        "source_instance", "source_sequence", "source_monotonic_ns",
        "event_type", "payload",
    }:
        raise ValidationError("replica event has schema drift")
    if (
        value["event_schema_version"] != 1
        or value["run_id"] != run_id
        or value["source_kind"] != "replica"
        or value["source_id"] != f"replica-{replica_id}"
        or not isinstance(value["source_instance"], str)
        or not value["source_instance"]
        or type(value["source_sequence"]) is not int
        or value["source_sequence"] <= 0
        or type(value["source_monotonic_ns"]) is not int
        or value["source_monotonic_ns"] <= 0
        or not isinstance(value["event_type"], str)
    ):
        raise ValidationError("replica event has an invalid envelope")
    return value


def _require_exact_event(value: object, *, run_id: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "event_schema_version", "run_id", "source_kind", "source_id",
        "source_instance", "source_sequence", "source_monotonic_ns",
        "event_type", "payload",
    }:
        raise ValidationError("manager event has schema drift")
    if (value["event_schema_version"] != 1 or value["run_id"] != run_id or
            value["source_kind"] != "adaptation_manager" or
            value["source_id"] != "adaptive-manager" or
            not isinstance(value["source_instance"], str) or not value["source_instance"] or
            type(value["source_sequence"]) is not int or value["source_sequence"] <= 0 or
            type(value["source_monotonic_ns"]) is not int or value["source_monotonic_ns"] < 0):
        raise ValidationError("manager event has an invalid envelope")
    if value["event_type"] != "evidence.observation_accepted":
        return value
    payload = value["payload"]
    if not isinstance(payload, Mapping) or set(payload) != {"ingestion_sequence", "observation"}:
        raise ValidationError("accepted observation payload has schema drift")
    observation = payload["observation"]
    required = {
        "schema_version", "observation_id", "reporter_id", "observed_replica_id",
        "configuration", "block_hash", "expected_message_type", "outcome",
        "response_duration_us", "deadline_duration_us", "reporter_monotonic_ns",
        "reporter_sequence", "signer_set", "attempt_start_monotonic_ns",
        "reporter_local_commit_monotonic_ns",
    }
    if not isinstance(observation, Mapping) or set(observation) != required:
        raise ValidationError("observation has schema drift")
    return value


def validate(preflight_record: Mapping[str, Any], events: Sequence[object], *, run_id: str,
             tree_override: Path | None = None,
             selection_cutoff: int | None = None) -> dict[str, Any]:
    if (preflight_record.get("scenario") != runner.SCENARIO or
            preflight_record.get("status") != "PREFLIGHT_ONLY"):
        raise ValidationError("preflight record has the wrong scenario or status")
    relay = preflight_record.get("relay_omission")
    if not isinstance(relay, Mapping):
        raise ValidationError("preflight record lacks relay contract")
    required_digest = relay.get("epoch_digest")
    if not isinstance(required_digest, str) or len(required_digest) != 64:
        raise ValidationError("preflight record lacks an exact epoch digest")
    if (
        relay.get("tree_ids") != list(runner.TREE_IDS)
        or relay.get("parent_reporters") != list(runner.EXPECTED_REPORTERS)
        or relay.get("required_qualifying_reporters") != 3
        or relay.get("required_timeouts_per_reporter") != 2
        or relay.get("total_omission_contexts") != 9
        or relay.get("expected_message_type") != "aggregate_relay"
    ):
        raise ValidationError("preflight record lacks the frozen nine-opportunity contract")
    if selection_cutoff is not None and (
        type(selection_cutoff) is not int or selection_cutoff <= 0
    ):
        raise ValidationError("selection cutoff must be a positive integer")
    tree_file = preflight_record.get("tree_file")
    if (not isinstance(tree_file, Mapping) or
            not isinstance(tree_file.get("path"), str) or
            not isinstance(tree_file.get("sha256"), str)):
        raise ValidationError("preflight record lacks an archived tree file")
    archived_tree = tree_override if tree_override is not None else Path(tree_file["path"])
    if not archived_tree.is_file() or runner.sha256_file(archived_tree) != tree_file["sha256"]:
        raise ValidationError("archived tree file is missing or differs from preflight")
    parsed_trees = runner.parse_tree_file(archived_tree)
    if runner.relay_parent_reporters(parsed_trees) != runner.EXPECTED_REPORTERS:
        raise ValidationError("archived tree file does not have the frozen parent roles")
    if relay.get("argv_overlay") != list(runner.omission_overlay(required_digest)):
        raise ValidationError("preflight overlay does not bind the declared path opportunities")

    expected_paths = set(zip(runner.TREE_IDS, runner.EXPECTED_REPORTERS))
    expected_digest = None
    matched: dict[tuple[int, int], list[Mapping[str, Any]]] = {}
    candidate_non_timeouts: list[Mapping[str, Any]] = []
    source_instance: str | None = None
    observation_ids: set[str] = set()
    previous_source_sequence = 0
    full_source_instance: str | None = None
    for raw in events:
        event = _require_exact_event(raw, run_id=run_id)
        if event["source_sequence"] <= previous_source_sequence:
            raise ValidationError("manager stream is not strictly source-sequence ordered")
        previous_source_sequence = event["source_sequence"]
        if full_source_instance is None:
            full_source_instance = event["source_instance"]
        elif full_source_instance != event["source_instance"]:
            raise ValidationError("manager stream crosses a restart")
        if event["event_type"] != "evidence.observation_accepted":
            continue
        ingestion_sequence = event["payload"].get("ingestion_sequence")
        if type(ingestion_sequence) is not int or ingestion_sequence <= 0:
            raise ValidationError("accepted observation has invalid ingestion sequence")
        if selection_cutoff is not None and ingestion_sequence > selection_cutoff:
            continue
        observation = event["payload"]["observation"]
        configuration = observation["configuration"]
        if (not isinstance(configuration, Mapping) or
                set(configuration) != {"epoch_number", "tree_id", "epoch_digest"}):
            raise ValidationError("observation configuration has schema drift")
        candidate_context = (
            observation["expected_message_type"] == "aggregate_relay"
            and observation["observed_replica_id"] == runner.OMITTING_REPLICA
            and configuration.get("epoch_number") == 0
            and (configuration.get("tree_id"), observation.get("reporter_id")) in expected_paths
        )
        if not candidate_context:
            continue
        if observation["outcome"] in {"late", "on_time"}:
            candidate_non_timeouts.append(event)
            continue
        if not (observation["outcome"] == "timeout"
                and observation["response_duration_us"] == 0
                and observation["signer_set"] == []):
            raise ValidationError("required reporter context is not a finalized aggregate timeout")
        if observation["schema_version"] != 3:
            raise ValidationError("required reporter timeout must use observation schema v3")
        digest = configuration["epoch_digest"]
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValidationError("candidate timeout has invalid epoch digest")
        expected_digest = expected_digest or digest
        if digest != required_digest or digest != expected_digest:
            raise ValidationError("candidate timeouts cross or differ from the frozen epoch digest")
        tree_id, reporter_id = configuration["tree_id"], observation["reporter_id"]
        if source_instance is None:
            source_instance = event["source_instance"]
        elif source_instance != event["source_instance"]:
            raise ValidationError("candidate timeouts cross a manager restart")
        observation_id = observation["observation_id"]
        if (not isinstance(observation_id, str) or len(observation_id) != 64
                or observation_id in observation_ids):
            raise ValidationError("candidate timeout has an invalid or duplicate observation ID")
        if not isinstance(observation["block_hash"], str) or len(observation["block_hash"]) != 64:
            raise ValidationError("candidate timeout has an invalid block hash")
        observation_ids.add(observation_id)
        matched.setdefault((tree_id, reporter_id), []).append(event)

    if set(matched) != expected_paths:
        raise ValidationError("missing one or more exact T4/T5/T6 parent timeout contexts")
    if any(not 2 <= len(context) <= 3 for context in matched.values()):
        raise ValidationError("each required parent must contribute two or three accepted timeouts")
    ordered = sorted(
        (event for context in matched.values() for event in context),
        key=lambda event: (event["source_sequence"], event["payload"]["ingestion_sequence"]),
    )
    if any(left["source_sequence"] >= right["source_sequence"] or
           left["payload"]["ingestion_sequence"] >= right["payload"]["ingestion_sequence"]
           for left, right in zip(ordered, ordered[1:])):
        raise ValidationError("manager receipt order is not strictly increasing")
    for key, context in matched.items():
        if len({event["payload"]["observation"]["block_hash"] for event in context}) != len(context):
            raise ValidationError(f"T{key[0]} reporter {key[1]} reuses one proposal block hash")
    counted_by_observation_id = {
        event["payload"]["observation"]["observation_id"]: event["payload"]["observation"]
        for event in ordered
    }
    counted_attempts = {
        _attempt_identity(timeout)
        for timeout in counted_by_observation_id.values()
    }
    for non_timeout_event in candidate_non_timeouts:
        response = non_timeout_event["payload"]["observation"]
        timeout = counted_by_observation_id.get(response.get("observation_id"))
        response_identity = _attempt_identity(response)
        if response["outcome"] == "on_time":
            if timeout is None and response_identity not in counted_attempts:
                continue
            raise ValidationError("on-time response duplicates a counted timeout attempt")
        if timeout is None:
            raise ValidationError("pre-cutoff late response does not match a counted timeout")
        timeout_identity = _attempt_identity(timeout)
        if response_identity != timeout_identity:
            raise ValidationError("pre-cutoff late response has an identity mismatch")
        raise ValidationError("counted timeout has late compensation before selection cutoff")
    return {
        "schema_version": 1,
        "scenario": runner.SCENARIO,
        "run_id": run_id,
        "verdict": "EVIDENCE_PREFIX_PASS",
        "claim_boundary": (
            "at least six timeout observations across the three frozen paths; "
            "no epoch activation or throughput claim"
        ),
        "epoch_digest": expected_digest,
        "reporters": list(runner.EXPECTED_REPORTERS),
        "tree_ids": list(runner.TREE_IDS),
        "observation_ids": [event["payload"]["observation"]["observation_id"] for event in ordered],
        "ingestion_sequences": [event["payload"]["ingestion_sequence"] for event in ordered],
    }


def _parse_command(payload: object, *, predecessor_digest: str) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != _COMMAND_FIELDS:
        raise ValidationError("epoch.command_committed payload has schema drift")
    command = dict(payload)
    for field in ("command_block_height", "activation_delay_blocks", "activation_height"):
        _positive_int(command[field], field)
    for field in ("predecessor_epoch_number", "successor_epoch_number"):
        if type(command[field]) is not int or command[field] < 0:
            raise ValidationError(f"{field} must be a non-negative integer")
    for field in (
        "command_block_hash", "payload_digest", "predecessor_epoch_digest", "successor_epoch_digest"
    ):
        _hex64(command[field], field, nonzero=True)
    if (
        command["predecessor_epoch_number"] != 0
        or command["successor_epoch_number"] != 1
        or command["predecessor_epoch_digest"] != predecessor_digest
        or command["activation_height"] != command["command_block_height"] + command["activation_delay_blocks"]
    ):
        raise ValidationError("E1 command does not bind the frozen E0 predecessor")
    return command


def _parse_activation(payload: object, *, command: Mapping[str, Any]) -> None:
    if not isinstance(payload, Mapping) or set(payload) != _ACTIVATION_FIELDS:
        raise ValidationError("epoch.activated payload has schema drift")
    if (
        payload["epoch_number"] != 1
        or type(payload["tree_id"]) is not int
        or payload["tree_id"] not in _E1_TREE_IDS
        or _hex64(payload["epoch_digest"], "activation epoch_digest", nonzero=True)
        != command["successor_epoch_digest"]
        or payload["activation_height"] != command["activation_height"]
    ):
        raise ValidationError("activation does not bind the exact E1 command")


def _common_commit_after_activation(
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
    activation_bounds: Mapping[int, tuple[int, int]],
    *,
    predecessor_digest: str,
    successor_digest: str,
) -> tuple[int, str]:
    """Find a common E1 commit, allowing only a valid in-flight E0 predecessor."""

    def is_after_activation(event: Mapping[str, Any], replica_id: int) -> bool:
        activation_sequence, activation_ns = activation_bounds[replica_id]
        return (
            event["source_sequence"] > activation_sequence
            and event["source_monotonic_ns"] > activation_ns
        )

    def audit_successor_commit_chronology() -> None:
        """Make an E1 proof invalid if its local activation has not occurred."""
        for replica_id in runner.REPLICA_IDS:
            for event in streams[f"replica-{replica_id}"]:
                if event["event_type"] != "block.committed":
                    continue
                payload = event["payload"]
                if not isinstance(payload, Mapping) or set(payload) != _COMMIT_FIELDS:
                    raise ValidationError("block.committed payload has schema drift")
                proof = payload["decision_proof"]
                if not isinstance(proof, Mapping) or set(proof) != _DECISION_PROOF_FIELDS:
                    raise ValidationError("committed decision proof has schema drift")
                if type(proof["epoch_number"]) is int and proof["epoch_number"] == 0:
                    # E0 can legitimately complete after local E1 activation.
                    continue
                block_hash = _hex64(payload["block_hash"], "committed block hash", nonzero=True)
                if (
                    type(proof["epoch_number"]) is not int
                    or proof["epoch_number"] != 1
                    or type(proof["tree_id"]) is not int
                    or proof["tree_id"] not in _E1_TREE_IDS
                    or proof["epoch_digest"] != successor_digest
                    or proof["block_hash"] != block_hash
                ):
                    raise ValidationError("successor E1 commit has the wrong decision proof")
                if not is_after_activation(event, replica_id):
                    raise ValidationError("successor E1 commit precedes exact activation")

    audit_successor_commit_chronology()

    def validate_all_designated_commits(
        successor_height: int, successor_hash: str, successor_parent_hash: object,
    ) -> None:
        """Audit the complete post-activation designated commit chain.

        An in-flight E0 commit is permitted after activation, but it must still
        be one coherent chain.  In particular, accepting the later common E1
        commit must not hide an earlier same-height conflict or a broken local
        E0 parent link.
        """
        commits_by_height: dict[int, tuple[object, ...]] = {}
        for replica_id in runner.REPLICA_IDS:
            previous_local_height: int | None = None
            for event in streams[f"replica-{replica_id}"]:
                if (event["event_type"] != "block.committed" or
                        not is_after_activation(event, replica_id)):
                    continue
                payload = event["payload"]
                if not isinstance(payload, Mapping) or set(payload) != _COMMIT_FIELDS:
                    raise ValidationError("block.committed payload has schema drift")
                if payload["designated_observer"] is not True:
                    continue
                height = _positive_int(payload["block_height"], "committed block height")
                block_hash = _hex64(payload["block_hash"], "committed block hash", nonzero=True)
                parent_hash = payload["parent_hash"]
                if parent_hash is not None:
                    _hex64(parent_hash, "committed parent hash", nonzero=True)
                if (type(payload["transaction_count"]) is not int or
                        payload["transaction_count"] < 0 or
                        type(payload["commit_batch_index"]) is not int or
                        payload["commit_batch_index"] < 0):
                    raise ValidationError("designated commit counters are invalid")
                proof = payload["decision_proof"]
                if not isinstance(proof, Mapping) or set(proof) != _DECISION_PROOF_FIELDS:
                    raise ValidationError("committed decision proof has schema drift")
                is_successor = (
                    type(proof["epoch_number"]) is int and proof["epoch_number"] == 1 and
                    type(proof["tree_id"]) is int and proof["tree_id"] in _E1_TREE_IDS and
                    proof["epoch_digest"] == successor_digest and proof["block_hash"] == block_hash
                )
                is_predecessor = (
                    type(proof["epoch_number"]) is int and proof["epoch_number"] == 0 and
                    type(proof["tree_id"]) is int and proof["tree_id"] in runner.REPLICA_IDS and
                    proof["epoch_digest"] == predecessor_digest and proof["block_hash"] == block_hash
                )
                if not is_successor and not is_predecessor:
                    raise ValidationError("authoritative E1 commit has the wrong decision proof")
                metadata = (
                    block_hash,
                    parent_hash,
                    payload["transaction_count"],
                    payload["commit_batch_index"],
                    proof["epoch_number"],
                    proof["tree_id"],
                    proof["epoch_digest"],
                )
                if previous_local_height is not None and height <= previous_local_height:
                    raise ValidationError("designated commit heights are not strictly increasing per source")
                previous_local_height = height
                prior = commits_by_height.setdefault(height, metadata)
                if prior != metadata:
                    raise ValidationError("designated commits conflict at one post-activation height")
                if is_predecessor:
                    if height >= successor_height:
                        raise ValidationError("in-flight E0 commit does not precede the common E1 commit")
                    if height == successor_height - 1 and successor_parent_hash != block_hash:
                        raise ValidationError("contiguous in-flight E0 commit does not bind the common E1 parent")
                elif height == successor_height and block_hash != successor_hash:
                    raise ValidationError("designated E1 commit conflicts with the common E1 result")
        previous_height: int | None = None
        previous_metadata: tuple[object, ...] | None = None
        for height in sorted(commits_by_height):
            metadata = commits_by_height[height]
            if previous_height is not None and height == previous_height + 1:
                # metadata[1] is the child parent hash and previous_metadata[0]
                # is the immediately preceding designated block hash.
                assert previous_metadata is not None
                if metadata[1] != previous_metadata[0]:
                    raise ValidationError("adjacent designated commits have a broken parent link")
            previous_height, previous_metadata = height, metadata

    def validate_all_peer_observations(
        successor_height: int,
        successor_hash: str,
        successor_parent_hash: object,
        successor_transaction_count: int,
        successor_batch_index: int,
    ) -> None:
        """Reject a same-height peer observation that disagrees with the result."""
        for replica_id in runner.REPLICA_IDS:
            for event in streams[f"replica-{replica_id}"]:
                if (event["event_type"] != "block.commit_observed" or
                        not is_after_activation(event, replica_id)):
                    continue
                payload = event["payload"]
                if not isinstance(payload, Mapping) or set(payload) != _COMMIT_OBSERVED_FIELDS:
                    raise ValidationError("block.commit_observed payload has schema drift")
                height = _positive_int(payload["block_height"], "observed block height")
                if height != successor_height:
                    continue
                block_hash = _hex64(payload["block_hash"], "observed block hash", nonzero=True)
                parent_hash = payload["parent_hash"]
                if parent_hash is not None:
                    _hex64(parent_hash, "observed parent hash", nonzero=True)
                if (type(payload["transaction_count"]) is not int or
                        payload["transaction_count"] < 0 or
                        type(payload["commit_batch_index"]) is not int or
                        payload["commit_batch_index"] < 0):
                    raise ValidationError("block.commit_observed counters are invalid")
                if (block_hash != successor_hash or
                        parent_hash != successor_parent_hash or
                        payload["transaction_count"] != successor_transaction_count or
                        payload["commit_batch_index"] != successor_batch_index):
                    raise ValidationError("post-activation common commit observation conflicts with the result")

    for authoritative_replica in runner.REPLICA_IDS:
        for event in streams[f"replica-{authoritative_replica}"]:
            if (event["event_type"] != "block.committed" or
                    not is_after_activation(event, authoritative_replica)):
                continue
            payload = event["payload"]
            if not isinstance(payload, Mapping) or set(payload) != _COMMIT_FIELDS:
                raise ValidationError("block.committed payload has schema drift")
            if payload["designated_observer"] is not True:
                continue
            height = _positive_int(payload["block_height"], "committed block height")
            block_hash = _hex64(payload["block_hash"], "committed block hash", nonzero=True)
            if payload["parent_hash"] is not None:
                _hex64(payload["parent_hash"], "committed parent hash", nonzero=True)
            if type(payload["transaction_count"]) is not int or payload["transaction_count"] < 0:
                raise ValidationError("committed transaction_count is invalid")
            if type(payload["commit_batch_index"]) is not int or payload["commit_batch_index"] < 0:
                raise ValidationError("committed batch index is invalid")
            proof = payload["decision_proof"]
            if not isinstance(proof, Mapping) or set(proof) != _DECISION_PROOF_FIELDS:
                raise ValidationError("committed decision proof has schema drift")
            is_successor_proof = (
                type(proof["epoch_number"]) is int
                and proof["epoch_number"] == 1
                and type(proof["tree_id"]) is int
                and proof["tree_id"] in _E1_TREE_IDS
                and proof["epoch_digest"] == successor_digest
                and proof["block_hash"] == block_hash
            )
            is_predecessor_proof = (
                type(proof["epoch_number"]) is int
                and proof["epoch_number"] == 0
                and type(proof["tree_id"]) is int
                and proof["tree_id"] in runner.REPLICA_IDS
                and proof["epoch_digest"] == predecessor_digest
                and proof["block_hash"] == block_hash
            )
            if is_predecessor_proof:
                continue
            if not is_successor_proof:
                raise ValidationError("authoritative E1 commit has the wrong decision proof")
            witnesses_match = True
            for replica_id in runner.REPLICA_IDS:
                if replica_id == authoritative_replica:
                    continue
                witness = next(
                    (
                        candidate for candidate in streams[f"replica-{replica_id}"]
                        if candidate["event_type"] == "block.commit_observed"
                        and is_after_activation(candidate, replica_id)
                        and isinstance(candidate["payload"], Mapping)
                        and candidate["payload"].get("block_height") == height
                        and candidate["payload"].get("block_hash") == block_hash
                    ),
                    None,
                )
                if witness is None:
                    witnesses_match = False
                    break
                witness_payload = witness["payload"]
                if set(witness_payload) != _COMMIT_OBSERVED_FIELDS:
                    raise ValidationError("block.commit_observed payload has schema drift")
            if witnesses_match:
                validate_all_designated_commits(height, block_hash, payload["parent_hash"])
                validate_all_peer_observations(
                    height,
                    block_hash,
                    payload["parent_hash"],
                    payload["transaction_count"],
                    payload["commit_batch_index"],
                )
                return height, block_hash
    raise ValidationError("all-seven post-E1 common commit is missing")


def _common_e0_commit_before_arm(
    streams: Mapping[str, Sequence[Mapping[str, Any]]], *, epoch_digest: str,
    arm_start_ns: int,
) -> tuple[int, str]:
    """Require an authoritative E0 commit and all six peer witnesses before arm."""
    for authoritative_replica in runner.REPLICA_IDS:
        for event in streams[f"replica-{authoritative_replica}"]:
            if event["event_type"] != "block.committed" or event["source_monotonic_ns"] >= arm_start_ns:
                continue
            payload = event["payload"]
            if not isinstance(payload, Mapping) or set(payload) != _COMMIT_FIELDS:
                raise ValidationError("E0 block.committed payload has schema drift")
            if type(payload["designated_observer"]) is not bool:
                raise ValidationError("E0 block.committed observer flag is invalid")
            if payload["designated_observer"] is False:
                continue
            proof = payload["decision_proof"]
            if (not isinstance(proof, Mapping) or
                    set(proof) != _DECISION_PROOF_FIELDS or proof.get("epoch_number") != 0 or
                    proof.get("epoch_digest") != epoch_digest or proof.get("block_hash") != payload.get("block_hash")):
                raise ValidationError("authoritative pre-arm E0 commit has wrong decision proof")
            height = _positive_int(payload["block_height"], "E0 committed block height")
            block_hash = _hex64(payload["block_hash"], "E0 committed block hash", nonzero=True)
            for replica_id in runner.REPLICA_IDS:
                if replica_id == authoritative_replica:
                    continue
                witness = next((candidate for candidate in streams[f"replica-{replica_id}"]
                                if candidate["event_type"] == "block.commit_observed" and
                                candidate["source_monotonic_ns"] < arm_start_ns and
                                isinstance(candidate["payload"], Mapping) and
                                candidate["payload"].get("block_height") == height and
                                candidate["payload"].get("block_hash") == block_hash), None)
                if witness is None:
                    break
                if set(witness["payload"]) != _COMMIT_OBSERVED_FIELDS:
                    raise ValidationError("pre-arm E0 peer commit payload has schema drift")
            else:
                return height, block_hash
    raise ValidationError("all-seven pre-arm common E0 commit is missing")


def _fault_window_start(
    value: object, *, run_id: str, epoch_digest: str
) -> tuple[int, int]:
    """Accept the known event envelope while the receipt schema remains frozen elsewhere."""
    event = _require_exact_event(value, run_id=run_id)
    if event["event_type"] != "fault_window_armed":
        raise ValidationError("explicit fault-window evidence is not fault_window_armed")
    payload = event["payload"]
    if not isinstance(payload, Mapping):
        raise ValidationError("fault_window_armed payload is not an object")
    if (
        payload.get("epoch_number") != 0
        or payload.get("epoch_digest") != epoch_digest
        or type(payload.get("evidence_start_monotonic_ns")) is not int
        or payload["evidence_start_monotonic_ns"] <= 0
    ):
        raise ValidationError("fault-window arm does not bind the frozen E0 evidence start")
    return int(payload["evidence_start_monotonic_ns"]), int(event["source_sequence"])


def validate_known_raw_events(
    preflight_record: Mapping[str, Any],
    manager_events: Sequence[object],
    replica_streams: Mapping[str, Sequence[object]],
    fault_window_arm: object,
    *,
    run_id: str, tree_override: Path | None = None,
) -> dict[str, Any]:
    """Validate only known raw event schemas; never produce a file-level PASS.

    This cannot bind raw file bytes, a signed bundle, or cleanup.  It does
    require the manager-owned path-timeout selection decision, but callers
    still must not turn its partial result into a thesis claim or figure.
    """
    selections = [
        _require_exact_event(event, run_id=run_id)
        for event in manager_events
        if isinstance(event, Mapping)
        and event.get("event_type") == "adaptive_v2.selection_decided"
    ]
    if len(selections) != 1:
        raise ValidationError("exactly one path-timeout selection decision is required")
    selection = selections[0]
    selection_payload = selection["payload"]
    if (not isinstance(selection_payload, Mapping) or
            set(selection_payload) != _SELECTION_DECIDED_FIELDS or
            type(selection_payload.get("evidence_cutoff")) is not int or
            selection_payload["evidence_cutoff"] < 6):
        raise ValidationError("path-timeout selection decision has schema or binding drift")
    prefix = validate(
        preflight_record,
        manager_events,
        run_id=run_id,
        tree_override=tree_override,
        selection_cutoff=selection_payload["evidence_cutoff"],
    )
    expected_sources = {f"replica-{replica_id}" for replica_id in runner.REPLICA_IDS}
    if set(replica_streams) != expected_sources:
        raise ValidationError("raw replica streams must name exactly replicas 0 through 6")

    expected_digest = prefix["epoch_digest"]
    arm_start_ns, arm_sequence = _fault_window_start(
        fault_window_arm, run_id=run_id, epoch_digest=expected_digest
    )
    arm_envelope_ns = fault_window_arm["source_monotonic_ns"]
    candidates = [
        _require_exact_event(event, run_id=run_id)
        for event in manager_events
        if isinstance(event, Mapping) and event.get("event_type") == "evidence.observation_accepted"
    ]
    matched = {
        event["payload"]["observation"]["observation_id"]: event
        for event in candidates
        if isinstance(event.get("payload"), Mapping)
        and isinstance(event["payload"].get("observation"), Mapping)
        and event["payload"]["observation"].get("observation_id") in prefix["observation_ids"]
    }
    if set(matched) != set(prefix["observation_ids"]):
        raise ValidationError("accepted parent omissions cannot be recovered from manager events")
    if any(
        event["source_sequence"] <= arm_sequence
        or event["payload"]["observation"]["attempt_start_monotonic_ns"] <= arm_start_ns
        or event["payload"]["observation"]["attempt_start_monotonic_ns"] <= arm_envelope_ns
        for event in matched.values()
    ):
        raise ValidationError("a required omission was not accepted after the fault-window arm")
    snapshots = [
        _require_exact_event(event, run_id=run_id)
        for event in manager_events
        if isinstance(event, Mapping) and event.get("event_type") == "adaptive_v2_evidence_snapshot"
    ]
    if len(snapshots) != 1:
        raise ValidationError("exactly one E1 evidence snapshot is required")
    snapshot = snapshots[0]
    snapshot_payload = snapshot["payload"]
    if not isinstance(snapshot_payload, Mapping) or set(snapshot_payload) != _EVIDENCE_SNAPSHOT_FIELDS:
        raise ValidationError("adaptive_v2_evidence_snapshot payload has schema drift")
    if (
        snapshot_payload["schema_version"] != 2
        or snapshot_payload["predecessor_epoch_number"] != 0
        or snapshot_payload["predecessor_epoch_digest"] != expected_digest
        or snapshot_payload["policy_intent"] != "fault_containment"
        or type(snapshot_payload["current_cutoff"]) is not int
        or snapshot_payload["current_cutoff"] < max(
            event["payload"]["ingestion_sequence"] for event in matched.values()
        )
        or type(snapshot_payload["accepted_prefix_count"]) is not int
        or snapshot_payload["accepted_prefix_count"] < len(matched)
        or snapshot_payload["accepted_prefix_count"] > snapshot_payload["current_cutoff"]
        or not isinstance(snapshot_payload["eligible_ranking"], list)
    ):
        raise ValidationError("E1 evidence snapshot does not bind the required omission prefix")
    if any(
        event["source_sequence"] >= snapshot["source_sequence"]
        or event["payload"]["ingestion_sequence"] > snapshot_payload["current_cutoff"]
        for event in matched.values()
    ):
        raise ValidationError("a required parent omission is not accepted before the E1 decision cutoff")
    if (
        not isinstance(selection_payload, Mapping)
        or set(selection_payload) != _SELECTION_DECIDED_FIELDS
        or selection_payload.get("schema_version") != 1
        or selection_payload.get("cycle_ordinal") != snapshot_payload["cycle_ordinal"]
        or selection_payload.get("predecessor_epoch_number") != 0
        or selection_payload.get("predecessor_epoch_digest") != expected_digest
        or selection_payload.get("baseline_cutoff") != snapshot_payload["baseline_cutoff"]
        or selection_payload.get("evidence_cutoff") != snapshot_payload["current_cutoff"]
        or selection_payload.get("evidence_snapshot_id") != snapshot_payload["evidence_snapshot_id"]
        or selection_payload.get("snapshot_evidence_basis")
        != _PATH_TIMEOUT_EVIDENCE_BASIS
        or selection_payload.get("selection_cardinality_policy")
        != _PATH_TIMEOUT_SELECTION_POLICY
        or selection_payload.get("selected_replicas") != [runner.OMITTING_REPLICA]
    ):
        raise ValidationError("path-timeout selection decision has schema or binding drift")
    if (
        type(selection_payload["baseline_cutoff"]) is not int
        or selection_payload["baseline_cutoff"] <= 0
        or type(selection_payload["evidence_cutoff"]) is not int
        or selection_payload["evidence_cutoff"] <= selection_payload["baseline_cutoff"]
        or not isinstance(selection_payload["evidence_snapshot_id"], str)
        or _hex64(
            selection_payload["evidence_snapshot_id"],
            "selection decision evidence_snapshot_id",
            nonzero=True,
        ) != selection_payload["evidence_snapshot_id"]
    ):
        raise ValidationError("path-timeout selection decision has invalid cutoffs or snapshot ID")
    if (
        selection["source_sequence"] <= max(event["source_sequence"] for event in matched.values())
        or selection["source_sequence"] >= snapshot["source_sequence"]
    ):
        raise ValidationError("path-timeout selection decision is not ordered between evidence and snapshot")

    streams: dict[str, list[Mapping[str, Any]]] = {}
    command: dict[str, Any] | None = None
    activation_bounds: dict[int, tuple[int, int]] = {}
    for replica_id in runner.REPLICA_IDS:
        source_id = f"replica-{replica_id}"
        raw_events = replica_streams[source_id]
        if not raw_events:
            raise ValidationError(f"{source_id} raw stream is empty")
        previous_sequence = 0
        source_instance: str | None = None
        events: list[Mapping[str, Any]] = []
        for raw_event in raw_events:
            event = _require_replica_event(raw_event, run_id=run_id, replica_id=replica_id)
            if event["source_sequence"] <= previous_sequence:
                raise ValidationError(f"{source_id} source sequence is not strictly increasing")
            if source_instance is None:
                source_instance = event["source_instance"]
            elif source_instance != event["source_instance"]:
                raise ValidationError(f"{source_id} crosses a process restart")
            previous_sequence = event["source_sequence"]
            events.append(event)
        streams[source_id] = events
        commands = [event for event in events if event["event_type"] == "epoch.command_committed"]
        if len(commands) != 1:
            raise ValidationError(f"{source_id} must emit exactly one E1 command")
        parsed_command = _parse_command(commands[0]["payload"], predecessor_digest=expected_digest)
        if command is None:
            command = parsed_command
        elif parsed_command != command:
            raise ValidationError("replicas disagree about the exact E1 command")
        activations = [event for event in events if event["event_type"] == "epoch.activated"]
        if len(activations) != 1:
            raise ValidationError(f"{source_id} must emit exactly one E1 activation")
        _parse_activation(activations[0]["payload"], command=parsed_command)
        if activations[0]["source_sequence"] <= commands[0]["source_sequence"]:
            raise ValidationError("E1 activation precedes its local command")
        activation_bounds[replica_id] = (
            activations[0]["source_sequence"],
            activations[0]["source_monotonic_ns"],
        )

    assert command is not None
    e0_height, e0_block_hash = _common_e0_commit_before_arm(
        streams, epoch_digest=expected_digest, arm_start_ns=arm_start_ns)
    height, block_hash = _common_commit_after_activation(
        streams,
        activation_bounds,
        predecessor_digest=expected_digest,
        successor_digest=command["successor_epoch_digest"],
    )
    return {
        "schema_version": 1,
        "scenario": runner.SCENARIO,
        "run_id": run_id,
        "verdict": "PARTIAL_ONLY",
        "claim_boundary": (
            "known raw event schemas only; no source-bound raw hashes, signed E1 bundle "
            "verification, or cleanup receipt"
        ),
        "epoch0_digest": expected_digest,
        "e1_command_payload_digest": command["payload_digest"],
        "e1_successor_epoch_digest": command["successor_epoch_digest"],
        "e1_activation_height": command["activation_height"],
        "accepted_parent_omission_ids": prefix["observation_ids"],
        "selection_cutoff": snapshot_payload["current_cutoff"],
        "selection_evidence_snapshot_id": selection_payload["evidence_snapshot_id"],
        "selection_replicas": list(selection_payload["selected_replicas"]),
        "observed_target_id": runner.OMITTING_REPLICA,
        "selected_replica_proof": "SOURCE_BOUND_PATH_TIMEOUT_SELECTION_DECISION",
        "baseline_common_commit": {"block_height": e0_height, "block_hash": e0_block_hash},
        "common_commit": {"block_height": height, "block_hash": block_hash},
    }


def _validate_native_injection(
    event: Mapping[str, Any], *, manager_arm_event: Mapping[str, Any],
    manager_arm_line_sha256: str,
) -> str:
    """Validate the native payload; envelope time, not gate-file time, orders it."""
    payload = event.get("payload")
    fields = {"actor", "gate_sha256", "manager_fault_window_arm_event_sha256", "profile_sha256", "tree_file_sha256", "launch_argv_sha256", "activation_monotonic_ns"}
    if (event.get("event_type") != "fault.injection_armed" or not isinstance(payload, Mapping) or
            set(payload) != fields or payload.get("actor") != 1 or
            payload.get("manager_fault_window_arm_event_sha256") != manager_arm_line_sha256 or
            type(payload.get("activation_monotonic_ns")) is not int or payload["activation_monotonic_ns"] <= 0):
        raise ValidationError("native replica-1 fault injection payload has schema drift")
    # The gate timestamp is supplied by the activation file before emit_audit;
    # StructuredEventSink samples the envelope timestamp later.  Equality is
    # neither causal nor guaranteed.  The source envelope is the trusted order.
    return _hex64(payload["gate_sha256"], "fault injection gate_sha256")


def validate_raw_bundle(run_root: Path, receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a sealed N7 v4 local bundle; reject any incomplete producer contract."""
    expected_receipt = {
        "schema_version", "scenario", "run_id", "artifacts", "fault_window_arm", "fault_injection_arm",
    }
    if set(receipt) != expected_receipt or receipt["schema_version"] != 1:
        raise ValidationError("raw-bundle receipt has schema drift")
    if receipt["scenario"] != runner.SCENARIO or not isinstance(receipt["run_id"], str):
        raise ValidationError("raw-bundle receipt has wrong scenario or run ID")
    run_id = receipt["run_id"]
    if run_root.is_symlink() or not run_root.is_dir():
        raise ValidationError("run root is not a directory")
    artifacts = receipt["artifacts"]
    required = {"preflight", "epoch0_tree", "execution_plan", "authorization_request", "plan_authorization", "fault_window_arm", "omission_gate", "manager_events", "replica_streams", "e1_bundle", "issuer_public_key", "cleanup"}
    if not isinstance(artifacts, Mapping) or set(artifacts) != required:
        raise ValidationError("raw-bundle artifacts have schema drift")
    preflight_bytes = _read_artifact(run_root, artifacts["preflight"], "preflight", _MAX_SMALL_BYTES)
    preflight = _strict_json(preflight_bytes, "preflight")
    if not isinstance(preflight, Mapping):
        raise ValidationError("preflight is not an object")
    causal_basis = runner.PHYSICAL_OMISSION_CAUSALITY_BASIS
    expected_causal_contract = {
        "timeout_evidence_basis": "exact_timeout_attempt_id_v1",
        "snapshot_evidence_basis": "exact_post_fault_path_timeout_quorum_v1",
        "physical_omission_causality_basis": causal_basis,
    }
    if preflight.get("fault_window_arm") != expected_causal_contract:
        raise ValidationError("preflight lacks the exact v4 physical-omission causal contract")
    epoch0_tree_bytes = _read_artifact(run_root, artifacts["epoch0_tree"], "epoch0 tree", _MAX_SMALL_BYTES)
    epoch0_tree_path = run_root / artifacts["epoch0_tree"]["path"]
    tree_record = preflight.get("tree_file")
    if (not isinstance(tree_record, Mapping) or tree_record.get("sha256") != hashlib.sha256(epoch0_tree_bytes).hexdigest()):
        raise ValidationError("archived epoch0 tree does not match approved preflight")
    # These must be frozen in the independently approved preflight, rather
    # than supplied by this receipt alongside a replacement signing key.
    _hex64(preflight.get("approved_issuer_public_key_sha256"), "approved issuer key fingerprint")
    _hex64(preflight.get("approved_plan_authorization_sha256"), "approved plan authorization fingerprint")
    _hex64(preflight.get("approved_plan_request_sha256"), "approved plan request fingerprint")
    authorization_bytes = _read_artifact(run_root, artifacts["plan_authorization"], "authorization receipt", _MAX_SMALL_BYTES)
    request_bytes = _read_artifact(run_root, artifacts["authorization_request"], "authorization request", _MAX_SMALL_BYTES)
    execution_plan_bytes = _read_artifact(run_root, artifacts["execution_plan"], "execution plan", _MAX_SMALL_BYTES)
    if artifacts["execution_plan"].get("path") != "runtime/n7-local-execution-plan.json":
        raise ValidationError("execution plan is not the final runtime launch plan")
    execution_plan = _strict_json(execution_plan_bytes, "execution plan")
    if (not isinstance(execution_plan, Mapping) or not isinstance(execution_plan.get("plan_sha256"), str) or
            execution_plan.get("scenario") != runner.SCENARIO or execution_plan.get("run_id") != run_id):
        raise ValidationError("execution plan lacks semantic plan digest")
    plan_semantic = {key: value for key, value in execution_plan.items() if key != "plan_sha256"}
    plan_digest = hashlib.sha256(json.dumps(plan_semantic, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if execution_plan["plan_sha256"] != plan_digest:
        raise ValidationError("execution plan semantic digest does not recompute")
    bindings = execution_plan.get("bindings")
    expected_bindings = {"run_id", "manager_source_instance", "epoch_digest", "tree_file_sha256", "topology_proof_sha256", "transition_request_sha256", "replica_1_launch_argv_sha256", "replica_1_launch_argv_sha256_domain"}
    if (not isinstance(bindings, Mapping) or set(bindings) != expected_bindings or
            bindings.get("run_id") != run_id or
            bindings.get("epoch_digest") != preflight.get("relay_omission", {}).get("epoch_digest") or
            bindings.get("tree_file_sha256") != hashlib.sha256(epoch0_tree_bytes).hexdigest() or
            bindings.get("topology_proof_sha256") != bindings.get("tree_file_sha256") or
            bindings.get("replica_1_launch_argv_sha256_domain") != "kauri-n7-replica-argv-without-self-hash-v1" or
            execution_plan.get("no_retry") is not True):
        raise ValidationError("execution plan bindings do not match approved N7 inputs")
    if execution_plan.get("physical_omission_causality_basis") != causal_basis:
        raise ValidationError("execution plan lacks the exact v4 physical-omission causal contract")
    if execution_plan.get("profile_sha256") != PROFILE_V4_SHA256:
        raise ValidationError("execution plan does not bind the canonical v4 profile")
    for field in ("profile_sha256", "epoch_digest", "tree_file_sha256", "topology_proof_sha256", "transition_request_sha256", "replica_1_launch_argv_sha256"):
        _hex64(execution_plan.get(field) if field == "profile_sha256" else bindings.get(field), f"execution plan {field}")
    request = _strict_json(request_bytes, "authorization request")
    request_fields = {
        "schema_version", "kind", "scenario", "execution_plan_sha256",
        "base_plan_sha256", "repository_revision", "final_launch_arguments_sha256",
        "issuer_public_key_sha256", "replica_1_launch_argv_sha256",
        "physical_omission_causality_basis", "hard_timeout_seconds", "no_retry",
    }
    if (not isinstance(request, Mapping) or set(request) != request_fields or
            request.get("schema_version") != 1 or
            request.get("kind") != "kauri-n7-local-execution-authorization-request-v1" or
            request.get("scenario") != runner.SCENARIO or
            request.get("execution_plan_sha256") != plan_digest or
            request.get("base_plan_sha256") != execution_plan.get("base_plan_sha256") or
            request.get("repository_revision") != execution_plan.get("repository_revision") or
            request.get("final_launch_arguments_sha256") != execution_plan.get("final_launch_arguments_sha256") or
            request.get("issuer_public_key_sha256") != execution_plan.get("issuer_public_key_sha256") or
            request.get("replica_1_launch_argv_sha256") != bindings.get("replica_1_launch_argv_sha256") or
            request.get("physical_omission_causality_basis") != causal_basis or
            request.get("hard_timeout_seconds") != execution_plan.get("hard_timeout_seconds") or
            request.get("no_retry") is not True or
            hashlib.sha256(request_bytes).hexdigest() != preflight["approved_plan_request_sha256"]):
        raise ValidationError("authorization request does not bind the approved execution plan")
    if hashlib.sha256(authorization_bytes).hexdigest() != preflight["approved_plan_authorization_sha256"]:
        raise ValidationError("authorization receipt is not pinned by approved preflight")
    authorization = _strict_json(authorization_bytes, "authorization receipt")
    authorization_fields = {"schema_version", "kind", "request_sha256", "execution_plan_sha256", "approval_reference", "approved_utc", "no_retry"}
    if (not isinstance(authorization, Mapping) or set(authorization) != authorization_fields or
            authorization.get("schema_version") != 1 or authorization.get("kind") != "kauri-n7-local-execution-authorization-v1" or
            authorization.get("request_sha256") != preflight["approved_plan_request_sha256"] or
            authorization.get("execution_plan_sha256") != plan_digest or not isinstance(authorization.get("approval_reference"), str) or not authorization["approval_reference"] or
            not isinstance(authorization.get("approved_utc"), str) or not authorization["approved_utc"].endswith("Z") or authorization.get("no_retry") is not True):
        raise ValidationError("authorization receipt has schema or preflight binding drift")
    arm_file_bytes = _read_artifact(run_root, artifacts["fault_window_arm"], "fault-window arm file", _MAX_SMALL_BYTES)
    arm_file = _strict_json(arm_file_bytes, "fault-window arm file")
    if (not isinstance(arm_file, Mapping) or arm_file_bytes !=
            (json.dumps(arm_file, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n")):
        raise ValidationError("fault-window arm file is not canonical")
    gate_file_bytes = _read_artifact(run_root, artifacts["omission_gate"], "omission gate file", _MAX_SMALL_BYTES)
    gate_file = _strict_json(gate_file_bytes, "omission gate file")
    gate_fields = ("schema_version", "kind", "profile_sha256", "tree_file_sha256", "epoch_digest", "replica_id", "launch_argv_sha256", "manager_run_id", "manager_source_instance", "manager_source_sequence", "fault_window_arm_event_sha256", "activation_monotonic_ns")
    if (not isinstance(gate_file, Mapping) or tuple(gate_file) != gate_fields or gate_file_bytes !=
            (json.dumps(gate_file, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n")):
        raise ValidationError("omission gate file is not native canonical bytes")
    manager_raw = _read_artifact(run_root, artifacts["manager_events"], "manager events", _MAX_RAW_BYTES)
    streams_desc = artifacts["replica_streams"]
    expected_sources = {f"replica-{replica_id}" for replica_id in runner.REPLICA_IDS}
    if not isinstance(streams_desc, Mapping) or set(streams_desc) != expected_sources:
        raise ValidationError("receipt must hash exactly seven replica streams")

    def jsonl(raw: bytes, label: str) -> tuple[list[object], dict[int, str]]:
        if not raw or raw[-1:] != b"\n":
            raise ValidationError(f"{label} is not newline-terminated JSONL")
        events, lines = [], {}
        for line in raw.splitlines():
            if not line or len(line) > _MAX_SMALL_BYTES:
                raise ValidationError(f"{label} has an empty or oversized JSONL line")
            event = _strict_json(line, label)
            if not isinstance(event, Mapping) or type(event.get("source_sequence")) is not int:
                raise ValidationError(f"{label} event lacks source sequence")
            if event["source_sequence"] in lines:
                raise ValidationError(f"{label} repeats a source sequence")
            lines[event["source_sequence"]] = hashlib.sha256(line).hexdigest()
            events.append(event)
        return events, lines

    manager_events, manager_lines = jsonl(manager_raw, "manager events")
    replica_streams: dict[str, list[object]] = {}
    replica_lines: dict[str, dict[int, str]] = {}
    for source in sorted(expected_sources):
        replica_streams[source], replica_lines[source] = jsonl(
            _read_artifact(run_root, streams_desc[source], source, _MAX_RAW_BYTES), source
        )
    arm = receipt["fault_window_arm"]
    if not isinstance(arm, Mapping) or set(arm) != {"source_sequence", "line_sha256", "clock_domain"}:
        raise ValidationError("fault-window arm binding has schema drift")
    if (arm.get("clock_domain") != "host-raw" or type(arm["source_sequence"]) is not int or
            _hex64(arm["line_sha256"], "arm line_sha256") != manager_lines.get(arm["source_sequence"])):
        raise ValidationError("fault-window arm is not bound to the hashed manager source")
    arm_event = next(event for event in manager_events if event["source_sequence"] == arm["source_sequence"])
    if arm_event.get("source_instance") != bindings["manager_source_instance"]:
        raise ValidationError("manager fault-window arm source instance differs from approved plan")
    arm_payload = arm_event.get("payload")
    expected_arm = {"schema_version", "kind", "run_id", "profile_id", "profile_sha256", "topology_proof_sha256", "request_sha256", "epoch_number", "epoch_digest", "fault_receipt_sha256", "evidence_start_monotonic_ns", "prefault_tree_id", "required_tree_positions", "required_tree_ids", "clock_domain", "required_observation_schema", "snapshot_evidence_basis", "selection_cardinality_policy", "timeout_evidence_basis", "fault_window_arm_sha256"}
    if (not isinstance(arm_payload, Mapping) or set(arm_payload) != expected_arm or arm_payload.get("schema_version") != 4 or
            arm_payload.get("kind") != "kauri-focused-fault-window-arm-v4" or arm_payload.get("run_id") != run_id or
            arm_payload.get("epoch_number") != 0 or arm_payload.get("epoch_digest") != preflight.get("relay_omission", {}).get("epoch_digest") or
            arm_payload.get("prefault_tree_id") != 4 or arm_payload.get("required_tree_positions") != 3 or
            arm_payload.get("required_tree_ids") != [4, 5, 6] or arm_payload.get("clock_domain") != "same_host_clock_monotonic_raw" or
            arm_payload.get("required_observation_schema") != 3 or arm_payload.get("snapshot_evidence_basis") != "exact_post_fault_path_timeout_quorum_v1" or
            arm_payload.get("selection_cardinality_policy") != "all_guarded_up_to_fault_bound_v1" or arm_payload.get("timeout_evidence_basis") != "exact_timeout_attempt_id_v1"):
        raise ValidationError("manager fault-window arm does not bind exact T4/T5/T6 v4 selection window")
    arm_without_hash = dict(arm_payload)
    if (arm_without_hash.pop("fault_window_arm_sha256") != hashlib.sha256(arm_file_bytes).hexdigest() or
            arm_without_hash != arm_file or
            arm_file.get("profile_sha256") != execution_plan["profile_sha256"] or
            arm_file.get("topology_proof_sha256") != bindings["topology_proof_sha256"] or
            arm_file.get("request_sha256") != bindings["transition_request_sha256"] or
            arm_file.get("fault_receipt_sha256") != hashlib.sha256(authorization_bytes).hexdigest()):
        raise ValidationError("manager fault-window arm differs from approved arm file or plan")
    partial = validate_known_raw_events(preflight, manager_events, replica_streams, arm_event, run_id=run_id, tree_override=epoch0_tree_path)
    injection = receipt["fault_injection_arm"]
    if not isinstance(injection, Mapping) or set(injection) != {"source_sequence", "line_sha256", "clock_domain"}:
        raise ValidationError("fault-injection arm binding has schema drift")
    if (injection.get("clock_domain") != "host-raw" or type(injection["source_sequence"]) is not int or
            _hex64(injection["line_sha256"], "injection arm line_sha256") != replica_lines["replica-1"].get(injection["source_sequence"])):
        raise ValidationError("fault-injection arm is not bound to hashed replica-1 source")
    injection_event = next(event for event in replica_streams["replica-1"] if event["source_sequence"] == injection["source_sequence"])
    gate_sha = _validate_native_injection(
        injection_event, manager_arm_event=arm_event, manager_arm_line_sha256=arm["line_sha256"])
    if injection_event["source_monotonic_ns"] <= arm_event["source_monotonic_ns"]:
        raise ValidationError("replica-1 fault injection was not armed after manager window arm")
    injection_payload = injection_event["payload"]
    if (gate_sha != hashlib.sha256(gate_file_bytes).hexdigest() or
            gate_file.get("schema_version") != 1 or
            gate_file.get("kind") != "kauri-n7-static-aggregate-omission-gate-v1" or
            gate_file.get("profile_sha256") != execution_plan["profile_sha256"] or
            gate_file.get("tree_file_sha256") != bindings["tree_file_sha256"] or
            gate_file.get("epoch_digest") != bindings["epoch_digest"] or
            gate_file.get("replica_id") != 1 or
            gate_file.get("launch_argv_sha256") != bindings["replica_1_launch_argv_sha256"] or
            gate_file.get("manager_run_id") != run_id or
            gate_file.get("manager_source_instance") != arm_event["source_instance"] or
            gate_file.get("manager_source_sequence") != arm_event["source_sequence"] or
            gate_file.get("fault_window_arm_event_sha256") != arm["line_sha256"] or
            type(gate_file.get("activation_monotonic_ns")) is not int or
            not arm_event["source_monotonic_ns"] < gate_file["activation_monotonic_ns"] <= injection_event["source_monotonic_ns"] or
            injection_payload.get("profile_sha256") != gate_file["profile_sha256"] or
            injection_payload.get("tree_file_sha256") != gate_file["tree_file_sha256"] or
            injection_payload.get("launch_argv_sha256") != gate_file["launch_argv_sha256"] or
            injection_payload.get("activation_monotonic_ns") != gate_file["activation_monotonic_ns"]):
        raise ValidationError("native injection differs from approved omission gate or plan")
    def accepted_context(event: Mapping[str, Any]) -> tuple[object, ...]:
        observation = event["payload"]["observation"]
        configuration = observation["configuration"]
        return (
            configuration["epoch_number"], configuration["epoch_digest"],
            configuration["tree_id"], observation["reporter_id"],
            observation["observed_replica_id"], observation["block_hash"],
            observation["expected_message_type"],
        )

    wanted_events: dict[tuple[object, ...], Mapping[str, Any]] = {}
    for event in manager_events:
        if (not isinstance(event, Mapping) or
                event.get("event_type") != "evidence.observation_accepted" or
                not isinstance(event.get("payload"), Mapping) or
                not isinstance(event["payload"].get("observation"), Mapping) or
                event["payload"]["observation"].get("observation_id") not in
                partial["accepted_parent_omission_ids"]):
            continue
        context = accepted_context(event)
        if context in wanted_events:
            raise ValidationError("accepted timeout contexts must be unique")
        wanted_events[context] = event
    wanted = set(wanted_events)
    if not 6 <= len(wanted_events) <= 9:
        raise ValidationError("accepted timeout context count is outside the declared range")
    selection_event = next(
        event for event in manager_events
        if isinstance(event, Mapping) and
        event.get("event_type") == "adaptive_v2.selection_decided"
    )
    selection_ns = selection_event["source_monotonic_ns"]
    arm_ns = arm_event["source_monotonic_ns"]
    gate_activation_ns = gate_file["activation_monotonic_ns"]
    injection_ns = injection_event["source_monotonic_ns"]
    observed: set[tuple[object, ...]] = set()
    first_drops: dict[tuple[object, ...], Mapping[str, Any]] = {}
    physical_opportunities_by_tree: dict[int, int] = {
        tree_id: 0 for tree_id in runner.TREE_IDS
    }
    repeat_count = 0
    drops = [event for event in replica_streams["replica-1"] if event.get("event_type") == "fault.aggregate_omitted"]
    for drop in drops:
        payload = drop.get("payload")
        fields = {"actor", "parent_replica", "epoch_number", "tree_id", "epoch_digest", "block_hash", "gate_sha256", "first_for_context"}
        if (not isinstance(payload, Mapping) or set(payload) != fields or payload.get("actor") != 1 or
                payload.get("epoch_number") != 0 or payload.get("epoch_digest") != partial["epoch0_digest"] or
                payload.get("gate_sha256") != gate_sha or type(payload.get("first_for_context")) is not bool or
                type(payload.get("tree_id")) is not int or payload.get("parent_replica") != payload.get("tree_id") or
                drop["source_sequence"] <= injection_event["source_sequence"] or
                drop["source_monotonic_ns"] <= injection_event["source_monotonic_ns"]):
            raise ValidationError("native replica-1 aggregate omission has schema or chronology drift")
        context = (
            payload["epoch_number"], payload["epoch_digest"], payload["tree_id"],
            payload["parent_replica"], payload["actor"],
            _hex64(payload["block_hash"], "native omission block_hash"),
            "aggregate_relay",
        )
        if payload["first_for_context"]:
            if payload["tree_id"] not in physical_opportunities_by_tree:
                raise ValidationError("native first omission is outside the declared path set")
            if context in observed:
                raise ValidationError("native omission repeats a first-for-context proposal")
            physical_opportunities_by_tree[payload["tree_id"]] += 1
            if physical_opportunities_by_tree[payload["tree_id"]] > 3:
                raise ValidationError("native first omissions exceed the declared three-per-tree cap")
            observed.add(context)
            first_drops[context] = drop
        else:
            # Native emits the audit record before returning early for a
            # repeated physical send. It is evidence of no new context.
            if context not in wanted:
                raise ValidationError("native repeated omission is outside accepted context")
            if context not in observed:
                raise ValidationError("native repeated omission appears before its first-for-context record")
            repeat_count += 1
    if repeat_count > 64:
        raise ValidationError("native omission repeat bound exceeded")
    if not wanted.issubset(observed):
        raise ValidationError("native omission does not explain each accepted timeout")
    for context, accepted_event in wanted_events.items():
        drop = first_drops[context]
        observation = accepted_event["payload"]["observation"]
        attempt_start_ns = observation["attempt_start_monotonic_ns"]
        deadline_ns = observation["deadline_duration_us"] * 1_000
        timeout_ns = observation["reporter_monotonic_ns"]
        manager_acceptance_ns = accepted_event["source_monotonic_ns"]
        drop_ns = drop["source_monotonic_ns"]
        # The reporter can start waiting before replica 1 arms the fault.
        # What matters is that its exact attempted relay is physically omitted
        # after the native gate has activated, before its deadline, and that
        # this same timeout is accepted before the selection decision.
        if not (
            arm_ns < gate_activation_ns <= attempt_start_ns and
            gate_activation_ns <= injection_ns < drop_ns and
            attempt_start_ns < drop_ns < attempt_start_ns + deadline_ns <= timeout_ns <
            manager_acceptance_ns < selection_ns
        ):
            raise ValidationError("accepted timeout lacks the v4 causal chronology")

    issuer = _read_artifact(run_root, artifacts["issuer_public_key"], "issuer public key", _MAX_SMALL_BYTES)
    try:
        issuer_key = issuer.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ValidationError("issuer public key is not ASCII") from exc
    if issuer != (issuer_key + "\n").encode("ascii"):
        raise ValidationError("issuer public key is not canonical newline text")
    if hashlib.sha256(issuer).hexdigest() != preflight["approved_issuer_public_key_sha256"]:
        raise ValidationError("issuer key is not pinned by the approved preflight")
    wire = _read_artifact(run_root, artifacts["e1_bundle"], "E1 bundle", _MAX_SMALL_BYTES)
    try:
        bundle = factorial_validation.decode_epoch_change_bundle(wire, issuer_public_key=issuer_key)
    except ValueError as exc:
        raise ValidationError(f"E1 bundle is not a valid signed native bundle: {exc}") from exc
    if (bundle.command.predecessor_epoch_digest != partial["epoch0_digest"] or
            bundle.epoch_digest != partial["e1_successor_epoch_digest"] or
            bundle.command.successor_epoch_number != 1 or
            bundle.command.payload_digest != partial["e1_command_payload_digest"] or
            bundle.evidence_snapshot_id != partial["selection_evidence_snapshot_id"] or
            bundle.evidence_cutoff != partial["selection_cutoff"]):
        raise ValidationError("signed E1 bundle differs from replica command")
    if len(bundle.trees) != len(_E1_TREE_IDS) or tuple(tree.tree_id for tree in bundle.trees) != _E1_TREE_IDS:
        raise ValidationError("signed E1 bundle does not contain the exact factory five-tree configuration")
    for tree in bundle.trees:
        if tuple(sorted(tree.members)) != tuple(runner.REPLICA_IDS):
            raise ValidationError("signed E1 bundle membership differs from frozen N7 membership")
        first_leaf = (len(tree.members) - 2) // tree.fanout + 1
        selected = tuple(partial["selection_replicas"])
        if (tuple(tree.wait_exempt) != selected or
                any(tree.members.index(replica) < first_leaf for replica in selected)):
            raise ValidationError("signed E1 bundle does not place the selected replica at a leaf")

    cleanup = _strict_json(_read_artifact(run_root, artifacts["cleanup"], "cleanup", _MAX_SMALL_BYTES), "cleanup")
    if not isinstance(cleanup, Mapping) or set(cleanup) != {"schema_version", "run_id", "complete", "processes"}:
        raise ValidationError("cleanup receipt has schema drift")
    if cleanup["schema_version"] != 1 or cleanup["run_id"] != run_id or cleanup["complete"] is not True:
        raise ValidationError("cleanup receipt is incomplete")
    processes = cleanup["processes"]
    if not isinstance(processes, list) or len(processes) != 8:
        raise ValidationError("cleanup receipt does not cover all eight processes")
    expected_cleanup = ["adaptive-manager", *sorted(expected_sources)]
    for expected_source, process in zip(expected_cleanup, processes):
        if (not isinstance(process, Mapping) or set(process) != {"source_id", "pid", "pgid", "returncode", "termination"} or
                process["source_id"] != expected_source or type(process["pid"]) is not int or process["pid"] <= 0 or
                type(process["pgid"]) is not int or process["pgid"] <= 0 or type(process["returncode"]) is not int or
                process["termination"] not in {"clean-exit", "terminated"}):
            raise ValidationError("cleanup process record has schema drift")
    return {
        **partial,
        "verdict": "RAW_BUNDLE_VALIDATED",
        "claim_boundary": "source-bound N7 v4 local-run evidence; external campaign replication remains required",
        "physical_omission_causality_basis": causal_basis,
        "e1_bundle_sha256": hashlib.sha256(wire).hexdigest(),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True, help="JSON array of manager events")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        preflight_record = json.loads(args.preflight.read_text(encoding="utf-8"))
        payload = args.events.read_text(encoding="utf-8").strip()
        events = json.loads(payload) if payload.startswith("[") else [json.loads(line) for line in payload.splitlines() if line]
        if not isinstance(events, list):
            raise ValidationError("events input must be a JSON array or JSONL")
        verdict = validate(preflight_record, events, run_id=args.run_id)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(verdict, stream, sort_keys=True, indent=2)
            stream.write("\n")
    except (OSError, json.JSONDecodeError, ValidationError, runner.PreflightError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
