#!/usr/bin/env python3
"""Independently verify the frozen N=7 diagnosis-to-policy transfer audit."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[2]
PROFILE_RELATIVE_PATH = (
    "experiments/adaptive/profiles/n7-diagnosis-policy-transfer-v1.json"
)
PROFILE_SHA256 = (
    "e6d04e8feca12534ec2477b5d3096189354b545d18e54a71ce80392a879b9739"
)
ANALYSIS_ID = "n7-diagnosis-policy-transfer-v1"
CAMPAIGN_RELATIVE_PATH = (
    "results/n7-fault-repetition-campaign/89838f30-seed41719-five"
)
CAMPAIGN_REVISION = "89838f3056935dd76c2c999a30d725565f19f1fa"
CAMPAIGN_PLAN_SHA256 = (
    "e33fc3450871406c18046b8e083fd5ab0b5a9a959ba48df7886bcb549d972bfa"
)
CAMPAIGN_VERDICT_SHA256 = (
    "359fa52fef0a0b2e8e5b37042cee827c12bd3f055f4f46f2d3fb939e816b19b8"
)
REPLAY_RELATIVE_PATH = (
    "results/n7-reputation-policy-replay/c90a19bf-seed41719-v3/"
    "reputation-policy-replay.json"
)
REPLAY_REVISION = "c90a19bf57de68d70d866ddfe9fda90529c8810d"
REPLAY_SHA256 = (
    "7d6f4599fe2869199c2324b0107c75dd198b65f05c7f8095dd7b44244e315220"
)
REPLAY_VALIDATION_SHA256 = (
    "ea885e7bff4d1364f7c46f2d24baf6686006a9f8133a92ef7d0b8a6949f1ee14"
)
REPLAY_PROFILE_SHA256 = (
    "5cc0d7e6f0d62d871e76cb3a0767144f7d315d62121c090440b4b3ef1ce20783"
)
REPLAY_PROFILE_RELATIVE_PATH = (
    "experiments/adaptive/profiles/n7-reputation-policy-replay-v1.json"
)
MEMBERSHIP = tuple(range(7))
MECHANISMS = ("responsiveness", "latency-priority")
REPORTER_ID = 6
TARGET_ID = 1
EPOCH_NUMBER = 0
EPOCH_DIGEST = (
    "f550407e56cc54a8fd4e93d1997ebe658b75699f4f2a9e955f4cc829b52bec81"
)
FAULT_PLAN_SHA256 = (
    "9d8c546f2149a5b115b52e02cc01d355079ab3b770c2882632e303978a054ef9"
)
_HEX_256 = re.compile(r"^[0-9a-f]{64}$")


RUNS: tuple[Mapping[str, object], ...] = (
    {
        "ordinal": 2,
        "campaign_repetition": 1,
        "run_id": "20260731T153736Z-41257-dcbdfc21",
        "execution_sha256": "d7789044f06d95c7ab7bdd7580fe625b219ec6081175eecfee27d4e73b2140a4",
        "arm_verdict_sha256": "2d8ab6a4e0c26f3364a4552beb2b619e8387fab0963832a2bdfcbd3cf0e0c998",
        "manager_sha256": "19f9c054441edf30a0f1bc23dc820dbd5d3ff1ba45266b5c782d0c69a8c2ca79",
        "certificate_sha256": "a6955316a9b467a327e21c75bbdafaf1a29fc79a87c2ce09ea29f944eebf9a1d",
        "accepted_observation_count": 549,
        "evidence_cutoff": 549,
    },
    {
        "ordinal": 4,
        "campaign_repetition": 2,
        "run_id": "20260731T153812Z-41433-1555eded",
        "execution_sha256": "6d3a92a7a82c348378929c0d932970ec83876943134c8502d0ca3121830ed144",
        "arm_verdict_sha256": "da6e0f37ffaa86938aed587f0bdcd417f46ead3d57e7256333082354c4fe7028",
        "manager_sha256": "9f9341591186fdf0749493a97963a878c49b3dbfce9bd9e22cd3f0f4e251f84c",
        "certificate_sha256": "41fd3cc3164b2a5c0a38d26a195739992e66df2744ef60225a58d7e1a74589ca",
        "accepted_observation_count": 538,
        "evidence_cutoff": 538,
    },
    {
        "ordinal": 9,
        "campaign_repetition": 3,
        "run_id": "20260731T153945Z-41783-ae90a44d",
        "execution_sha256": "ba09a07f8a1d7f370f2125ab2b7f4ebb89831390b18738b7fd985694a8a48dc8",
        "arm_verdict_sha256": "90f0842eea850ac94598b827d26c3b235d961adc246323c210b8c38596a65dac",
        "manager_sha256": "15da86a7c030b165ffad0505af1e8efe15e5ce40b3f67c0cb0c081f19c1914bf",
        "certificate_sha256": "c2d17b8512e345d68797ec8bedf01981c9879c472a74dcc99d1ba6aeeb5adef6",
        "accepted_observation_count": 508,
        "evidence_cutoff": 508,
    },
    {
        "ordinal": 12,
        "campaign_repetition": 4,
        "run_id": "20260731T154040Z-41955-dca0b34d",
        "execution_sha256": "34835729330c16375594b0f55f39c8d163a3f59b1a42437513c2a0eb6969c10a",
        "arm_verdict_sha256": "a141764638bcef523854dc296669f1fa0c8d1c018184a5df33ebdf723db9925a",
        "manager_sha256": "44f82daf0b91989273d9c0167661a078f93a07bffc1402814be51eb8f758f0a0",
        "certificate_sha256": "10c924b2f9acd343af4cb17751d941765222736afeb9919651d1c9ce91e99645",
        "accepted_observation_count": 550,
        "evidence_cutoff": 550,
    },
    {
        "ordinal": 14,
        "campaign_repetition": 5,
        "run_id": "20260731T154118Z-42074-add984a3",
        "execution_sha256": "19aea0b0886f8a72a7ebef269917051abf6b06730b9ecbede70c25771e3bcd37",
        "arm_verdict_sha256": "bf64f20da5154126b866ff975805f7626c751cf9a44d252e749114cb5164b3a4",
        "manager_sha256": "93cb95b56eef326df16e338bd18e74090a66c55652a4a94f0922f30485b64934",
        "certificate_sha256": "ef1d129541656651597b2b062d7ae902dfd6732df59d0494c3d25fd1cda3c037",
        "accepted_observation_count": 502,
        "evidence_cutoff": 502,
    },
)


class VerificationError(ValueError):
    """The producer artifact or one of its sources is not exactly accepted."""


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise VerificationError("value is not canonical-JSON encodable") from exc


def _sha_bytes(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise VerificationError(f"{path.name} must be a regular file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise VerificationError(f"{label} must be a regular file")
    try:
        value = json.loads(path.read_bytes())
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise VerificationError(f"{label} is invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise VerificationError(f"{label} must be an object")
    return value


def _read_canonical_artifact(path: Path) -> Mapping[str, Any]:
    value = _read_json(path, "analysis artifact")
    encoded = (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")
    if path.read_bytes() != encoded:
        raise VerificationError("analysis artifact is not canonical JSON")
    return value


def _object(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise VerificationError(f"{label} must be an object")
    return value


def _array(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise VerificationError(f"{label} must be an array")
    return value


def _integer(value: object, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise VerificationError(f"{label} is invalid")
    return value


def _source_file(relative: str, expected: str) -> Path:
    if relative != expected:
        raise VerificationError("source path differs from the frozen contract")
    value = Path(relative)
    if value.is_absolute() or ".." in value.parts:
        raise VerificationError("source path escapes the repository")
    candidate = ROOT / value
    current = ROOT
    for part in value.parts:
        current = current / part
        if current.is_symlink():
            raise VerificationError("source path contains a symbolic link")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise VerificationError("source path does not resolve") from exc
    if not resolved.is_relative_to(ROOT.resolve()) or not resolved.is_file():
        raise VerificationError("source path escapes the repository")
    return resolved


def _expected_profile() -> Mapping[str, object]:
    return {
        "schema_version": 1,
        "frozen": True,
        "analysis_id": ANALYSIS_ID,
        "analysis_timing": "retrospective_post_hoc",
        "source_campaign": {
            "relative_path": CAMPAIGN_RELATIVE_PATH,
            "kauri_revision": CAMPAIGN_REVISION,
            "plan_sha256": CAMPAIGN_PLAN_SHA256,
            "verdict_sha256": CAMPAIGN_VERDICT_SHA256,
        },
        "source_replay": {
            "relative_path": REPLAY_RELATIVE_PATH,
            "kauri_revision": REPLAY_REVISION,
            "artifact_sha256": REPLAY_SHA256,
            "validation_sha256": REPLAY_VALIDATION_SHA256,
            "profile_sha256": REPLAY_PROFILE_SHA256,
        },
        "fixed_contract": {
            "arm": "static_authenticated_false_report",
            "ordinals": [2, 4, 9, 12, 14],
            "membership": list(MEMBERSHIP),
            "reporter_id": REPORTER_ID,
            "target_id": TARGET_ID,
            "mechanisms": list(MECHANISMS),
            "influential_count": 3,
            "evidence_cutoff": "terminal_complete_accepted_manager_prefix",
            "analysis_timing": "retrospective_post_hoc",
        },
        "claims_not_made": [
            "no live epoch or counterfactual tree is activated by this audit",
            "no throughput or latency effect follows from the offline projection",
            "no general Byzantine attribution or policy superiority follows from five N=7 runs",
            "no consensus-safety claim follows from this retrospective join",
            "the terminal ranking is not a ranking captured at diagnostic settlement",
            "the passive certificate is not an input to the two scalar replay policies",
            "a correct eligible target need not be promoted into the projected top three",
        ],
    }


@dataclass
class _Attempt:
    first_sequence: int
    replica_id: int
    state: str
    latency_us: int
    deadline_us: int


def _accepted(path: Path) -> tuple[list[dict[str, Any]], str]:
    accepted: list[dict[str, Any]] = []
    last_sequence = 0
    epoch_digest: str | None = None
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise VerificationError(
                    f"manager line {line_number} is invalid JSON"
                ) from exc
            if not isinstance(event, Mapping):
                raise VerificationError("manager event must be an object")
            if event.get("event_type") != "evidence.observation_accepted":
                continue
            payload = _object(event.get("payload"), "accepted payload")
            sequence = _integer(
                payload.get("ingestion_sequence"), "ingestion sequence", 1
            )
            if sequence <= last_sequence:
                raise VerificationError("accepted sequence is not strictly increasing")
            last_sequence = sequence
            observation = dict(_object(payload.get("observation"), "observation"))
            if observation.get("schema_version") != 1:
                raise VerificationError("observation schema differs")
            identity = observation.get("observation_id")
            if not isinstance(identity, str) or _HEX_256.fullmatch(identity) is None:
                raise VerificationError("observation id is invalid")
            reporter = _integer(observation.get("reporter_id"), "reporter id")
            observed = _integer(
                observation.get("observed_replica_id"), "observed replica id"
            )
            if reporter not in MEMBERSHIP or observed not in MEMBERSHIP:
                raise VerificationError("observation names a nonmember")
            configuration = _object(
                observation.get("configuration"), "observation configuration"
            )
            if configuration.get("epoch_number") != EPOCH_NUMBER:
                raise VerificationError("observation is outside the frozen epoch")
            digest = configuration.get("epoch_digest")
            if not isinstance(digest, str) or _HEX_256.fullmatch(digest) is None:
                raise VerificationError("observation epoch digest is invalid")
            if epoch_digest is None:
                epoch_digest = digest
            elif digest != epoch_digest:
                raise VerificationError("accepted stream mixes epoch digests")
            observation["_sequence"] = sequence
            accepted.append(observation)
    if not accepted or epoch_digest is None:
        raise VerificationError("accepted stream is empty")
    if epoch_digest != EPOCH_DIGEST:
        raise VerificationError("accepted stream epoch digest differs")
    return accepted, epoch_digest


def _attempts(observations: Sequence[Mapping[str, Any]]) -> list[_Attempt]:
    attempts: dict[str, _Attempt] = {}
    for observation in observations:
        identity = str(observation["observation_id"])
        observed = _integer(
            observation.get("observed_replica_id"), "observed replica id"
        )
        if observation.get("expected_message_type") not in {
            "aggregate_relay",
            "direct_vote",
        }:
            raise VerificationError("message type is outside the frozen domain")
        outcome = observation.get("outcome")
        if outcome not in {"on_time", "timeout", "late"}:
            raise VerificationError("observation outcome is invalid")
        latency = _integer(
            observation.get("response_duration_us"), "response duration"
        )
        deadline = _integer(
            observation.get("deadline_duration_us"), "deadline duration", 1
        )
        sequence = _integer(observation.get("_sequence"), "sequence", 1)
        prior = attempts.get(identity)
        if prior is None:
            if outcome == "late":
                raise VerificationError("attempt starts with a late response")
            if outcome == "timeout" and latency != 0:
                raise VerificationError("timeout has a response duration")
            attempts[identity] = _Attempt(
                first_sequence=sequence,
                replica_id=observed,
                state="timeout_only" if outcome == "timeout" else "on_time",
                latency_us=latency,
                deadline_us=deadline,
            )
            continue
        if (
            prior.replica_id != observed
            or prior.state != "timeout_only"
            or outcome != "late"
            or prior.deadline_us != deadline
            or latency < deadline
        ):
            raise VerificationError("timeout-to-late transition is invalid")
        prior.state = "late"
        prior.latency_us = latency
    return list(attempts.values())


def _ranking(
    observations: Sequence[Mapping[str, Any]],
    replay_profile: Mapping[str, Any],
    mechanism: str,
) -> list[dict[str, Any]]:
    policy = _object(replay_profile.get("responsiveness_policy"), "replay policy")
    grouped: dict[int, list[_Attempt]] = defaultdict(list)
    for attempt in _attempts(observations):
        grouped[attempt.replica_id].append(attempt)
    rows: list[dict[str, Any]] = []
    for replica in MEMBERSHIP:
        window = _integer(policy.get("attempt_window"), "attempt window", 1)
        values = sorted(
            grouped[replica], key=lambda item: item.first_sequence
        )[-window:]
        counts = Counter(item.state for item in values)
        total = len(values)
        responses = counts["on_time"] + counts["late"]
        timeouts = counts["timeout_only"] + counts["late"]
        response_rate = responses * 1_000_000 // total if total else 0
        timeout_rate = timeouts * 1_000_000 // total if total else 0
        trailing = 0
        for item in reversed(values):
            if item.state != "timeout_only":
                break
            trailing += 1
        latency_values = sorted(
            item.latency_us
            for item in values
            if item.state in {"on_time", "late"}
        )
        percentile = _integer(
            policy.get("latency_percentile_basis_points"),
            "latency percentile",
            1,
        )
        latency = (
            latency_values[
                math.ceil(percentile * len(latency_values) / 10_000) - 1
            ]
            if latency_values
            else None
        )
        reasons: list[str] = []
        if total < _integer(policy.get("minimum_attempts"), "minimum attempts", 1):
            classification = "insufficient_evidence"
            reasons.append("insufficient_attempts")
        else:
            if response_rate < _integer(
                policy.get("minimum_response_rate_ppm"), "response-rate floor"
            ):
                reasons.append("response_rate_below_minimum")
            if timeout_rate > _integer(
                policy.get("maximum_timeout_rate_ppm"), "timeout-rate ceiling"
            ):
                reasons.append("timeout_rate_above_maximum")
            if trailing >= _integer(
                policy.get("trailing_timeout_streak"), "timeout streak", 2
            ):
                reasons.append("persistent_timeout_streak")
            classification = "nonresponsive" if reasons else "responsive"
        rows.append(
            {
                "replica_id": replica,
                "attempt_count": total,
                "on_time_count": counts["on_time"],
                "timeout_only_count": counts["timeout_only"],
                "late_count": counts["late"],
                "response_count": responses,
                "timeout_count": timeouts,
                "trailing_timeout_count": trailing,
                "response_rate_ppm": response_rate,
                "timeout_rate_ppm": timeout_rate,
                "latency_percentile_us": latency,
                "classification": classification,
                "eligible": classification == "responsive",
                "reasons": reasons,
            }
        )

    def key(row: Mapping[str, Any]) -> tuple[Any, ...]:
        latency = row["latency_percentile_us"]
        availability = (0 if latency is not None else 1, latency or 0)
        if mechanism == "responsiveness":
            return (
                0 if row["eligible"] else 1,
                -int(row["response_rate_ppm"]),
                int(row["timeout_rate_ppm"]),
                *availability,
                -int(row["attempt_count"]),
                int(row["replica_id"]),
            )
        if mechanism != "latency-priority":
            raise VerificationError("mechanism is outside the frozen contract")
        return (
            0 if row["eligible"] else 1,
            *availability,
            -int(row["response_rate_ppm"]),
            int(row["timeout_rate_ppm"]),
            -int(row["attempt_count"]),
            int(row["replica_id"]),
        )

    rows.sort(key=key)
    for rank, row in enumerate(rows):
        row["rank"] = rank
    return rows


def _paths(run: Mapping[str, object]) -> Mapping[str, str]:
    ordinal = int(run["ordinal"])
    run_id = str(run["run_id"])
    base = (
        f"{CAMPAIGN_RELATIVE_PATH}/attempt-{ordinal:02d}-results/"
        f"static_authenticated_false_report/{run_id}"
    )
    return {
        "execution": f"{CAMPAIGN_RELATIVE_PATH}/attempt-{ordinal:02d}-execution.json",
        "arm_verdict": f"{base}/arm-verdict.json",
        "fault_plan": f"{base}/fault-plan.json",
        "manager": f"{base}/raw/adaptive-manager.jsonl",
    }


def _source_binding(path: str, sha256: str) -> Mapping[str, str]:
    return {"path": path, "sha256": sha256}


def _check_execution(
    execution: Mapping[str, Any], run: Mapping[str, object]
) -> None:
    if (
        execution.get("schema_version") != 1
        or execution.get("ordinal") != run["ordinal"]
        or execution.get("campaign_repetition") != run["campaign_repetition"]
        or execution.get("arm") != "static_authenticated_false_report"
    ):
        raise VerificationError("execution record identity differs")
    binding = _object(execution.get("arm_verdict"), "execution arm verdict")
    if (
        binding.get("run_id") != run["run_id"]
        or binding.get("raw_sha256") != run["arm_verdict_sha256"]
    ):
        raise VerificationError("execution record does not bind the arm verdict")


def _check_fault_plan(plan: Mapping[str, Any]) -> None:
    expected_action = {
        "diagnostic_window": "n7-epoch0-tree6-tree0-static-v1",
        "fault_id": "false-report-6-to-1",
        "kind": "static_authenticated_false_report",
        "reported_outcome": "timeout",
        "reporter_id": REPORTER_ID,
        "target_id": TARGET_ID,
    }
    actions = _array(plan.get("actions"), "fault actions")
    if len(actions) != 1 or actions[0] != expected_action:
        raise VerificationError("fault plan identity differs")
    scenario = _object(plan.get("scenario"), "fault scenario")
    if (
        plan.get("schema_version") != 1
        or scenario.get("replica_ids") != list(MEMBERSHIP)
        or scenario.get("quorum") != 5
        or scenario.get("diagnostic_fault_bound") != 1
    ):
        raise VerificationError("fault-plan scenario differs")


def _check_certificate(
    verdict: Mapping[str, Any],
    run: Mapping[str, object],
    observations: Sequence[Mapping[str, Any]],
) -> None:
    if (
        verdict.get("schema_version") != 2
        or verdict.get("arm") != "static_authenticated_false_report"
        or verdict.get("run_id") != run["run_id"]
        or verdict.get("verdict") != "PASS"
        or verdict.get("kauri_revision") != CAMPAIGN_REVISION
        or verdict.get("fault_plan_sha256") != FAULT_PLAN_SHA256
    ):
        raise VerificationError("arm verdict identity differs")
    certificate = dict(
        _object(verdict.get("diagnostic_certificate"), "diagnostic certificate")
    )
    recorded_digest = certificate.pop("certificate_sha256", None)
    if (
        recorded_digest != run["certificate_sha256"]
        or recorded_digest != _sha_bytes(certificate)
    ):
        raise VerificationError("diagnostic certificate digest differs")
    scope = {
        "diagnostic_fault_bound": 1,
        "epoch_digest": EPOCH_DIGEST,
        "epoch_number": EPOCH_NUMBER,
        "expected_message_type": "aggregate_relay",
        "phases": [
            {"reporter_id": REPORTER_ID, "tree_id": 6},
            {"reporter_id": 0, "tree_id": 0},
        ],
        "target_id": TARGET_ID,
    }
    expected_static = {
        "schema_version": 1,
        "certificate_kind": "passive_aggregate_relay_crosscheck",
        "ordering_basis": "manager_receipt_order",
        "scope": scope,
        "status": "settled",
        "compatible_hypothesis_count": 1,
        "settled_hypothesis": {
            "false_reporters": [REPORTER_ID],
            "persistent_omitters": [],
        },
        "durable_role_exclusions": [REPORTER_ID],
        "pending_endpoints": [],
    }
    for key, value in expected_static.items():
        if certificate.get(key) != value:
            raise VerificationError("diagnostic certificate semantics differ")
    certificate_observations = _array(
        certificate.get("observations"), "certificate observations"
    )
    if len(certificate_observations) != 2:
        raise VerificationError("diagnostic certificate is not two-phase")
    accepted_by_id = {
        observation["observation_id"]: observation
        for observation in observations
    }
    receipt_sequences: list[int] = []
    for index, value in enumerate(certificate_observations):
        item = _object(value, "certificate observation")
        source = accepted_by_id.get(item.get("observation_id"))
        if source is None:
            raise VerificationError("certificate observation is absent from manager")
        expected_outcome = "timeout" if source.get("outcome") == "timeout" else "response"
        configuration = _object(source.get("configuration"), "configuration")
        expected = {
            "observation_id": source["observation_id"],
            "reporter_id": source["reporter_id"],
            "target_id": source["observed_replica_id"],
            "epoch_number": configuration["epoch_number"],
            "tree_id": configuration["tree_id"],
            "epoch_digest": configuration["epoch_digest"],
            "expected_message_type": source["expected_message_type"],
            "outcome": expected_outcome,
        }
        if dict(item) != expected:
            raise VerificationError("certificate observation differs from manager")
        if index == 0 and expected_outcome != "timeout":
            raise VerificationError("first diagnostic observation is not a timeout")
        if index == 1 and expected_outcome != "response":
            raise VerificationError("second diagnostic observation is not a response")
        receipt_sequences.append(int(source["_sequence"]))
    if receipt_sequences != sorted(receipt_sequences) or len(set(receipt_sequences)) != 2:
        raise VerificationError("certificate observations are not in receipt order")


def _replay_profile() -> Mapping[str, Any]:
    path = _source_file(
        REPLAY_PROFILE_RELATIVE_PATH, REPLAY_PROFILE_RELATIVE_PATH
    )
    if _sha_file(path) != REPLAY_PROFILE_SHA256:
        raise VerificationError("replay profile differs from accepted bytes")
    profile = _read_json(path, "replay profile")
    if (
        profile.get("frozen") is not True
        or profile.get("membership") != list(MEMBERSHIP)
        or profile.get("reputation_mechanisms") != list(MECHANISMS)
        or _object(profile.get("role_projection"), "role projection").get(
            "root_count"
        )
        != 1
        or _object(profile.get("role_projection"), "role projection").get(
            "internal_count"
        )
        != 2
        or _object(profile.get("evidence_window"), "evidence window").get(
            "ground_truth_available_to_policy"
        )
        is not False
    ):
        raise VerificationError("replay profile contract differs")
    return profile


def _git_head() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise VerificationError("cannot resolve analysis revision") from exc


def _require_committed_source(relative: str) -> None:
    path = _source_file(relative, relative)
    committed = subprocess.run(
        ["git", "show", f"HEAD:{relative}"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if committed.returncode != 0 or committed.stdout != path.read_bytes():
        raise VerificationError(f"analysis source is not committed at HEAD: {relative}")


def _expected_artifact(profile: Mapping[str, Any]) -> Mapping[str, object]:
    replay_path = _source_file(REPLAY_RELATIVE_PATH, REPLAY_RELATIVE_PATH)
    replay_validation_path = _source_file(
        str(Path(REPLAY_RELATIVE_PATH).with_name("validation.json")),
        str(Path(REPLAY_RELATIVE_PATH).with_name("validation.json")),
    )
    if _sha_file(replay_path) != REPLAY_SHA256:
        raise VerificationError("accepted replay artifact hash differs")
    if _sha_file(replay_validation_path) != REPLAY_VALIDATION_SHA256:
        raise VerificationError("accepted replay validation hash differs")
    replay_validation = _read_json(replay_validation_path, "replay validation")
    if replay_validation.get("verdict") != "PASS":
        raise VerificationError("accepted replay validation is not PASS")
    campaign_plan_path = _source_file(
        f"{CAMPAIGN_RELATIVE_PATH}/campaign-plan.json",
        f"{CAMPAIGN_RELATIVE_PATH}/campaign-plan.json",
    )
    campaign_verdict_path = _source_file(
        f"{CAMPAIGN_RELATIVE_PATH}/n7-fault-repetition-campaign.json",
        f"{CAMPAIGN_RELATIVE_PATH}/n7-fault-repetition-campaign.json",
    )
    if _sha_file(campaign_plan_path) != CAMPAIGN_PLAN_SHA256:
        raise VerificationError("campaign plan hash differs")
    if _sha_file(campaign_verdict_path) != CAMPAIGN_VERDICT_SHA256:
        raise VerificationError("campaign verdict hash differs")
    campaign_plan = _read_json(campaign_plan_path, "campaign plan")
    campaign_verdict = _read_json(campaign_verdict_path, "campaign verdict")
    if campaign_plan.get("retry_policy") != "none":
        raise VerificationError("campaign plan allowed retries")
    if campaign_verdict.get("verdict") != "PASS":
        raise VerificationError("campaign verdict is not PASS")
    schedule = {
        slot.get("ordinal"): slot
        for slot in (
            _object(item, "scheduled attempt")
            for item in _array(
                campaign_plan.get("scheduled_attempts"), "scheduled attempts"
            )
        )
    }
    replay_profile = _replay_profile()

    reconstructed: list[dict[str, object]] = []
    ranking_by_ordinal: dict[int, Mapping[str, list[dict[str, Any]]]] = {}
    observations_by_ordinal: dict[int, Sequence[Mapping[str, Any]]] = {}
    paths_by_ordinal: dict[int, Mapping[str, str]] = {}
    for run in RUNS:
        ordinal = int(run["ordinal"])
        slot = _object(schedule.get(ordinal), "fixed scheduled attempt")
        if (
            slot.get("arm") != "static_authenticated_false_report"
            or slot.get("campaign_repetition") != run["campaign_repetition"]
        ):
            raise VerificationError("fixed scheduled attempt differs")
        paths = _paths(run)
        paths_by_ordinal[ordinal] = paths
        expected_hashes = {
            "execution": str(run["execution_sha256"]),
            "arm_verdict": str(run["arm_verdict_sha256"]),
            "fault_plan": FAULT_PLAN_SHA256,
            "manager": str(run["manager_sha256"]),
        }
        resolved: dict[str, Path] = {}
        for name, relative in paths.items():
            resolved[name] = _source_file(relative, relative)
            if _sha_file(resolved[name]) != expected_hashes[name]:
                raise VerificationError(f"{name} source hash differs in attempt {ordinal}")
        _check_execution(_read_json(resolved["execution"], "execution"), run)
        observations, _digest = _accepted(resolved["manager"])
        if (
            len(observations) != run["accepted_observation_count"]
            or observations[-1]["_sequence"] != run["evidence_cutoff"]
        ):
            raise VerificationError("terminal manager prefix differs")
        observations_by_ordinal[ordinal] = observations

        # Ranking reconstruction is deliberately completed before either the
        # fault plan or diagnostic certificate is parsed.
        mechanism_rows: dict[str, list[dict[str, Any]]] = {}
        for mechanism in MECHANISMS:
            mechanism_rows[mechanism] = _ranking(
                observations, replay_profile, mechanism
            )
        ranking_by_ordinal[ordinal] = mechanism_rows

    # Only after every ranking is fixed may the verifier expose fault identity.
    for run in RUNS:
        ordinal = int(run["ordinal"])
        paths = paths_by_ordinal[ordinal]
        fault_path = _source_file(paths["fault_plan"], paths["fault_plan"])
        verdict_path = _source_file(paths["arm_verdict"], paths["arm_verdict"])
        _check_fault_plan(_read_json(fault_path, "fault plan"))
        _check_certificate(
            _read_json(verdict_path, "arm verdict"),
            run,
            observations_by_ordinal[ordinal],
        )

    # C-026 is loaded only as a post-reconstruction equality cross-check.
    replay = _read_json(replay_path, "accepted replay")
    replay_runs = {
        item.get("ordinal"): item
        for item in (
            _object(value, "replay run")
            for value in _array(replay.get("runs"), "replay runs")
        )
    }
    summary: dict[str, dict[str, int]] = {
        mechanism: {
            "run_count": len(RUNS),
            "reporter_excluded_count": 0,
            "reporter_scalar_eligible_count": 0,
            "target_retained_count": 0,
        }
        for mechanism in MECHANISMS
    }
    for run in RUNS:
        ordinal = int(run["ordinal"])
        replay_run = _object(replay_runs.get(ordinal), "accepted replay run")
        if (
            replay_run.get("run_id") != run["run_id"]
            or replay_run.get("campaign_repetition")
            != run["campaign_repetition"]
            or replay_run.get("accepted_observation_count")
            != run["accepted_observation_count"]
            or replay_run.get("evidence_cutoff") != run["evidence_cutoff"]
            or replay_run.get("source_execution_sha256")
            != run["execution_sha256"]
            or replay_run.get("source_manager_sha256") != run["manager_sha256"]
            or replay_run.get("source_fault_plan_sha256") != FAULT_PLAN_SHA256
        ):
            raise VerificationError("accepted replay run identity differs")
        mechanism_output: dict[str, object] = {}
        replay_results = _object(replay_run.get("policy_results"), "policy results")
        for mechanism in MECHANISMS:
            rows = ranking_by_ordinal[ordinal][mechanism]
            replay_result = _object(
                replay_results.get(mechanism), "accepted mechanism result"
            )
            if replay_result.get("ranking") != rows:
                raise VerificationError("accepted replay ranking differs")
            endpoint_rows = {
                int(row["replica_id"]): row
                for row in rows
                if int(row["replica_id"]) in {REPORTER_ID, TARGET_ID}
            }
            if set(endpoint_rows) != {REPORTER_ID, TARGET_ID} or any(
                row.get("eligible") is not True for row in endpoint_rows.values()
            ):
                raise VerificationError(
                    "diagnostic endpoints do not retain scalar eligibility"
                )
            eligible = [
                int(row["replica_id"]) for row in rows if row["eligible"]
            ]
            if len(eligible) < 3:
                raise VerificationError("ranking cannot fill influential roles")
            influential = eligible[:3]
            expected_projection = {
                "root_id": influential[0],
                "internal_ids": influential[1:],
                "influential_ids": influential,
            }
            if replay_result.get("role_projection") != expected_projection:
                raise VerificationError("accepted replay projection differs")
            ranks = {int(row["replica_id"]): int(row["rank"]) for row in rows}
            reporter_excluded = REPORTER_ID not in influential
            target_retained = TARGET_ID in influential
            # A correct target may rationally remain eligible but outside the
            # three projected roles. Its top-three status is descriptive, not
            # a correctness requirement or evidence of punishment.
            mechanism_output[mechanism] = {
                "ranking_sha256": _sha_bytes(rows),
                "influential_ids": influential,
                "reporter_rank": ranks[REPORTER_ID],
                "target_rank": ranks[TARGET_ID],
                "reporter_scalar_eligible": True,
                "target_scalar_eligible": True,
                "reporter_excluded": reporter_excluded,
                "target_retained": target_retained,
            }
            summary[mechanism]["reporter_excluded_count"] += int(
                reporter_excluded
            )
            summary[mechanism]["reporter_scalar_eligible_count"] += 1
            summary[mechanism]["target_retained_count"] += int(target_retained)
        paths = paths_by_ordinal[ordinal]
        reconstructed.append(
            {
                "ordinal": ordinal,
                "campaign_repetition": run["campaign_repetition"],
                "run_id": run["run_id"],
                "sources": {
                    "execution": _source_binding(
                        paths["execution"], str(run["execution_sha256"])
                    ),
                    "arm_verdict": _source_binding(
                        paths["arm_verdict"], str(run["arm_verdict_sha256"])
                    ),
                    "fault_plan": _source_binding(
                        paths["fault_plan"], FAULT_PLAN_SHA256
                    ),
                    "manager": _source_binding(
                        paths["manager"], str(run["manager_sha256"])
                    ),
                },
                "accepted_observation_count": run["accepted_observation_count"],
                "evidence_cutoff": run["evidence_cutoff"],
                "diagnosis": {
                    "reporter_id": REPORTER_ID,
                    "target_id": TARGET_ID,
                    "certificate_sha256": run["certificate_sha256"],
                    "durable_role_exclusions": [REPORTER_ID],
                },
                "mechanisms": mechanism_output,
            }
        )
    return {
        "schema_version": 1,
        "analysis_id": ANALYSIS_ID,
        "verdict": "PASS",
        "analysis_revision": _git_head(),
        "profile_relative_path": PROFILE_RELATIVE_PATH,
        "profile_sha256": PROFILE_SHA256,
        "source_campaign": dict(_object(profile["source_campaign"], "campaign")),
        "source_replay": dict(_object(profile["source_replay"], "replay")),
        "fixed_contract": dict(_object(profile["fixed_contract"], "contract")),
        "runs": reconstructed,
        "summary": summary,
        "claims_not_made": list(_array(profile["claims_not_made"], "claims")),
    }


def verify(artifact_path: Path, profile_path: Path) -> Mapping[str, object]:
    for relative in (
        PROFILE_RELATIVE_PATH,
        "experiments/adaptive/run_diagnosis_policy_transfer.py",
        "experiments/adaptive/validate_diagnosis_policy_transfer.py",
    ):
        _require_committed_source(relative)
    artifact = _read_canonical_artifact(artifact_path)
    expected_profile_path = _source_file(PROFILE_RELATIVE_PATH, PROFILE_RELATIVE_PATH)
    resolved_profile = profile_path.resolve(strict=True)
    if resolved_profile != expected_profile_path:
        raise VerificationError("profile path differs from the frozen contract")
    if _sha_file(resolved_profile) != PROFILE_SHA256:
        raise VerificationError("analysis profile hash differs")
    profile = _read_json(resolved_profile, "analysis profile")
    if dict(profile) != _expected_profile():
        raise VerificationError("analysis profile differs from the frozen contract")
    expected = _expected_artifact(profile)
    if dict(artifact) != expected:
        raise VerificationError("artifact differs from independent exact reconstruction")
    return {
        "schema_version": 1,
        "verifier_id": "n7-diagnosis-policy-transfer-independent-v1",
        "verdict": "PASS",
        "artifact_sha256": _sha_file(artifact_path),
        "profile_sha256": PROFILE_SHA256,
        "independently_reconstructed_runs": len(RUNS),
        "accepted_observations_replayed": sum(
            int(run["accepted_observation_count"]) for run in RUNS
        ),
        "analysis_timing": "retrospective_post_hoc",
        "evidence_cutoff": "terminal_complete_accepted_manager_prefix",
        "checks": [
            "all source paths and bytes match the frozen provenance contract",
            "all five terminal accepted-manager prefixes reconstruct independently",
            "fault identity remains hidden until both scalar rankings are complete",
            "settled certificates recompute and bind receipt-ordered raw observations",
            "C-026 rankings are used only as a post-reconstruction equality check",
            "both diagnostic endpoints remain scalar-eligible in every ranking; top-three is ordinal placement",
            "all per-run transfer metrics and fixed-denominator counts reconstruct",
        ],
    }


def write_receipt(
    artifact_path: Path, profile_path: Path, output_path: Path
) -> Mapping[str, object]:
    artifact = artifact_path.resolve(strict=True)
    output = output_path.resolve(strict=False)
    if output.parent != artifact.parent:
        raise VerificationError("receipt output must share the artifact directory")
    protected_roots = (
        (ROOT / CAMPAIGN_RELATIVE_PATH).resolve(strict=True),
        (ROOT / REPLAY_RELATIVE_PATH).resolve(strict=True).parent,
    )
    if any(output.is_relative_to(root) for root in protected_roots):
        raise VerificationError("receipt output must be outside accepted evidence")
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise VerificationError("receipt output must be a new regular file")
    receipt = verify(artifact, profile_path)
    output.write_bytes(_canonical_bytes(receipt) + b"\n")
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument(
        "--profile",
        type=Path,
        default=ROOT / PROFILE_RELATIVE_PATH,
    )
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = (
            write_receipt(
                arguments.artifact,
                arguments.profile,
                arguments.output,
            )
            if arguments.output is not None
            else verify(arguments.artifact.resolve(strict=True), arguments.profile)
        )
        print(json.dumps(result, sort_keys=True))
    except (OSError, VerificationError) as exc:
        print(f"verification rejected: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
