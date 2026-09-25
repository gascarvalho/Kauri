#!/usr/bin/env python3
"""Join accepted N=7 diagnostic certificates to terminal offline role projections.

This is a retrospective descriptive analysis. It does not activate a tree or
measure the effect of a diagnosis-aware placement policy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any


REPOSITORY = Path(__file__).resolve().parents[2]
PROFILE = Path("experiments/adaptive/profiles/n7-diagnosis-policy-transfer-v1.json")
ARTIFACT_NAME = "diagnosis-policy-transfer.json"


class TransferError(ValueError):
    """The frozen source or analysis contract was violated."""


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TransferError(f"{path} is not an object")
    return value


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_file(path: Path) -> str:
    return _hash_bytes(path.read_bytes())


def _canonical_hash(value: Any) -> str:
    return _hash_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    )


def _source(relative: str) -> Path:
    path = (REPOSITORY / relative).resolve()
    if not path.is_relative_to(REPOSITORY.resolve()) or not path.is_file():
        raise TransferError(f"source is not a repository-contained file: {relative}")
    return path


def _binding(path: Path) -> dict[str, str]:
    return {"path": str(path.relative_to(REPOSITORY)), "sha256": _hash_file(path)}


def _require_committed_source(relative: str) -> None:
    path = _source(relative)
    committed = subprocess.run(
        ["git", "show", f"HEAD:{relative}"],
        cwd=REPOSITORY,
        capture_output=True,
        check=False,
    )
    if committed.returncode != 0 or committed.stdout != path.read_bytes():
        raise TransferError(f"analysis source is not committed at HEAD: {relative}")


def _run_path(campaign: Path, ordinal: int, arm: str) -> Path:
    root = campaign / f"attempt-{ordinal:02d}-results" / arm
    candidates = sorted(path for path in root.iterdir() if path.is_dir())
    if len(candidates) != 1 or not candidates[0].resolve().is_relative_to(REPOSITORY):
        raise TransferError(f"attempt {ordinal} does not bind exactly one source run")
    return candidates[0]


def build() -> dict[str, Any]:
    _require_committed_source(str(PROFILE))
    _require_committed_source(
        "experiments/adaptive/run_diagnosis_policy_transfer.py"
    )
    _require_committed_source(
        "experiments/adaptive/validate_diagnosis_policy_transfer.py"
    )
    profile_path = _source(str(PROFILE))
    profile = _load(profile_path)
    if (
        profile.get("schema_version") != 1
        or profile.get("frozen") is not True
        or profile.get("analysis_id") != "n7-diagnosis-policy-transfer-v1"
    ):
        raise TransferError("analysis profile is not frozen v1")
    fixed = profile["fixed_contract"]
    if fixed != {
        "arm": "static_authenticated_false_report",
        "ordinals": [2, 4, 9, 12, 14],
        "membership": list(range(7)),
        "reporter_id": 6,
        "target_id": 1,
        "mechanisms": ["responsiveness", "latency-priority"],
        "influential_count": 3,
        "evidence_cutoff": "terminal_complete_accepted_manager_prefix",
        "analysis_timing": "retrospective_post_hoc",
    }:
        raise TransferError("fixed analysis contract drifted")
    campaign_source = profile["source_campaign"]
    replay_source = profile["source_replay"]
    campaign = (REPOSITORY / campaign_source["relative_path"]).resolve()
    if not campaign.is_relative_to(REPOSITORY.resolve()):
        raise TransferError("campaign path escapes the repository")
    plan_path = _source(str(campaign_source["relative_path"]) + "/campaign-plan.json")
    verdict_path = _source(
        str(campaign_source["relative_path"]) + "/n7-fault-repetition-campaign.json"
    )
    if (
        _hash_file(plan_path) != campaign_source["plan_sha256"]
        or _hash_file(verdict_path) != campaign_source["verdict_sha256"]
    ):
        raise TransferError("campaign source hash drifted")
    plan = _load(plan_path)
    if plan.get("retry_policy") != "none" or _load(verdict_path).get("verdict") != "PASS":
        raise TransferError("campaign is not a no-retry PASS")
    replay_path = _source(str(replay_source["relative_path"]))
    validation_path = replay_path.with_name("validation.json")
    original_profile = _source("experiments/adaptive/profiles/n7-reputation-policy-replay-v1.json")
    if (
        _hash_file(replay_path) != replay_source["artifact_sha256"]
        or _hash_file(validation_path) != replay_source["validation_sha256"]
        or _hash_file(original_profile) != replay_source["profile_sha256"]
    ):
        raise TransferError("replay source hash drifted")
    replay = _load(replay_path)
    validation = _load(validation_path)
    if (
        replay.get("verdict") != "PASS"
        or replay.get("analysis_revision") != replay_source["kauri_revision"]
        or validation.get("verdict") != "PASS"
        or validation.get("artifact_sha256") != replay_source["artifact_sha256"]
    ):
        raise TransferError("source replay is not independently accepted")
    scheduled = {
        slot["ordinal"]: slot
        for slot in plan["scheduled_attempts"]
        if slot.get("arm") == fixed["arm"]
    }
    replayed = {run["ordinal"]: run for run in replay["runs"]}
    if sorted(scheduled) != fixed["ordinals"] or len(replayed) != 15:
        raise TransferError("frozen five-run map drifted")
    output_runs: list[dict[str, Any]] = []
    for ordinal in fixed["ordinals"]:
        slot = scheduled[ordinal]
        run = replayed[ordinal]
        run_path = _run_path(campaign, ordinal, fixed["arm"])
        files = {
            "execution": campaign / f"attempt-{ordinal:02d}-execution.json",
            "arm_verdict": run_path / "arm-verdict.json",
            "fault_plan": run_path / "fault-plan.json",
            "manager": run_path / "raw" / "adaptive-manager.jsonl",
        }
        if any(
            not path.resolve().is_relative_to(REPOSITORY) or not path.is_file()
            for path in files.values()
        ):
            raise TransferError(f"attempt {ordinal} has an invalid source path")
        execution = _load(files["execution"])
        verdict = _load(files["arm_verdict"])
        fault = _load(files["fault_plan"])
        repetition = slot["campaign_repetition"]
        if (
            execution.get("returncode") != 0
            or execution.get("arm") != fixed["arm"]
            or execution.get("campaign_repetition") != repetition
            or verdict.get("verdict") != "PASS"
            or verdict.get("run_id") != run.get("run_id")
            or run.get("campaign_repetition") != repetition
            or run.get("arm") != fixed["arm"]
            or execution.get("arm_verdict", {}).get("raw_sha256")
            != _hash_file(files["arm_verdict"])
            or run.get("source_execution_sha256") != _hash_file(files["execution"])
            or run.get("source_manager_sha256") != _hash_file(files["manager"])
            or run.get("source_fault_plan_sha256") != _hash_file(files["fault_plan"])
        ):
            raise TransferError(f"attempt {ordinal} source binding failed")
        action = fault.get("actions", [])
        if len(action) != 1 or action[0].get("kind") != fixed["arm"] or (
            action[0].get("reporter_id"), action[0].get("target_id")
        ) != (fixed["reporter_id"], fixed["target_id"]):
            raise TransferError(f"attempt {ordinal} fault identity drifted")
        certificate = verdict.get("diagnostic_certificate")
        if not isinstance(certificate, dict):
            raise TransferError(f"attempt {ordinal} lacks a certificate")
        certificate_payload = dict(certificate)
        certificate_hash = certificate_payload.pop("certificate_sha256", None)
        if (
            certificate_hash != _canonical_hash(certificate_payload)
            or certificate.get("status") != "settled"
            or certificate.get("compatible_hypothesis_count") != 1
            or certificate.get("certificate_kind") != "passive_aggregate_relay_crosscheck"
            or certificate.get("settled_hypothesis")
            != {"false_reporters": [6], "persistent_omitters": []}
            or certificate.get("durable_role_exclusions") != [6]
            or certificate.get("scope", {}).get("target_id") != 1
        ):
            raise TransferError(f"attempt {ordinal} diagnosis drifted")
        mechanisms: dict[str, Any] = {}
        for name in fixed["mechanisms"]:
            result = run["policy_results"][name]
            influential = result["role_projection"]["influential_ids"]
            ranking = result["ranking"]
            positions = {row["replica_id"]: row["rank"] for row in ranking}
            eligible = {row["replica_id"]: row["eligible"] for row in ranking}
            if (
                len(influential) != 3
                or len(set(influential)) != 3
                or set(positions) != set(range(7))
                or eligible.get(6) is not True
                or eligible.get(1) is not True
            ):
                raise TransferError(f"attempt {ordinal} {name} projection is invalid")
            reporter_excluded = 6 not in influential
            target_retained = 1 in influential
            mechanisms[name] = {
                "ranking_sha256": _canonical_hash(ranking),
                "influential_ids": influential,
                "reporter_rank": positions[6],
                "target_rank": positions[1],
                "reporter_scalar_eligible": eligible[6],
                "target_scalar_eligible": eligible[1],
                "reporter_excluded": reporter_excluded,
                "target_retained": target_retained,
            }
        output_runs.append(
            {
                "ordinal": ordinal,
                "campaign_repetition": repetition,
                "run_id": run["run_id"],
                "sources": {name: _binding(path) for name, path in files.items()},
                "accepted_observation_count": run["accepted_observation_count"],
                "evidence_cutoff": run["evidence_cutoff"],
                "diagnosis": {
                    "reporter_id": 6,
                    "target_id": 1,
                    "certificate_sha256": certificate_hash,
                    "durable_role_exclusions": [6],
                },
                "mechanisms": mechanisms,
            }
        )
    summary = {
        name: {
            "run_count": len(output_runs),
            "reporter_excluded_count": sum(
                run["mechanisms"][name]["reporter_excluded"] for run in output_runs
            ),
            "reporter_scalar_eligible_count": sum(
                run["mechanisms"][name]["reporter_scalar_eligible"]
                for run in output_runs
            ),
            "target_retained_count": sum(
                run["mechanisms"][name]["target_retained"] for run in output_runs
            ),
        }
        for name in fixed["mechanisms"]
    }
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPOSITORY, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    return {
        "schema_version": 1,
        "analysis_id": profile["analysis_id"],
        "verdict": "PASS",
        "analysis_revision": revision,
        "profile_relative_path": str(PROFILE),
        "profile_sha256": _hash_file(profile_path),
        "source_campaign": campaign_source,
        "source_replay": replay_source,
        "fixed_contract": fixed,
        "runs": output_runs,
        "summary": summary,
        "claims_not_made": profile["claims_not_made"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        artifact = build()
        if arguments.output.exists():
            raise TransferError("output already exists; never overwrite an audit")
        arguments.output.mkdir(parents=True)
        target = arguments.output / ARTIFACT_NAME
        target.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, KeyError, TypeError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(2, f"diagnosis-policy transfer rejected: {error}\n")
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
