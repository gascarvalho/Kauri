#!/usr/bin/env python3
"""Run the frozen five-by-three N=7 fault repetition campaign.

Every scheduled slot is invoked once, sequentially.  Missing or invalid arm
verdicts remain visible in the immutable plan and per-slot execution records;
they are never replaced by a retry.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence
import uuid


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from experiments.adaptive.kauri_experiment.comparison import (  # noqa: E402
    build_n7_comparison,
)
from experiments.adaptive.kauri_experiment.n7_fault_repetition_campaign import (  # noqa: E402
    FROZEN_ATTEMPT_SCHEDULE,
    N7FaultRepetitionCampaignError,
    REPETITIONS_PER_ARM,
    build_n7_execution_binding,
    canonical_n7_fault_repetition_json,
    normalize_n7_fault_campaign_attempt,
    semantic_n7_campaign_sha256,
    summarize_n7_fault_repetition_campaign,
)
from experiments.adaptive.kauri_experiment.thesis_evaluation import (  # noqa: E402
    parse_thesis_json_object,
)


ARTIFACT_NAME = "n7-fault-repetition-campaign.json"
PLAN_NAME = "campaign-plan.json"
_REVISION = re.compile(r"^[0-9a-f]{40}$")


class CampaignRunError(RuntimeError):
    """The campaign could not be safely scheduled or persisted."""


def _json_bytes(value: Mapping[str, Any], *, compact: bool = False) -> bytes:
    separators = (",", ":") if compact else None
    return (
        json.dumps(
            value,
            allow_nan=False,
            indent=None if compact else 2,
            separators=separators,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _write_exclusive(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _comparison(revision: str) -> Any:
    return build_n7_comparison(
        kauri_revision=revision,
        seed=41_719,
        crash_replica_id=1,
        false_reporter_id=6,
        false_report_target_id=1,
        persistent_omitter_id=1,
        diagnostic_window="n7-epoch0-tree6-tree0-static-v1",
    )


def _read_verdict(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        payload = path.read_bytes()
        source = payload.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise CampaignRunError(f"cannot read arm verdict {path}: {error}") from error
    try:
        verdict = parse_thesis_json_object(source, f"arm verdict {path}")
    except ValueError as error:
        raise CampaignRunError(f"invalid arm verdict {path}: {error}") from error
    return verdict, payload


def _execution_record(
    *,
    ordinal: int,
    repetition: int,
    arm: str,
    command: Sequence[str],
    completed: subprocess.CompletedProcess[str],
    stdout_path: Path,
    stderr_path: Path,
    results_root: Path,
    verdict_path: Path | None,
    verdict_sha256: str | None,
    verdict_canonical_sha256: str | None,
    verdict_run_id: str | None,
    evidence_error: str | None,
    started_utc: str,
    finished_utc: str,
    elapsed_ms: int,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "scenario": "n7-static-fault-repetition-execution",
        "ordinal": ordinal,
        "campaign_repetition": repetition,
        "arm": arm,
        "command": list(command),
        "results_root": str(results_root),
        "returncode": completed.returncode,
        "started_utc": started_utc,
        "finished_utc": finished_utc,
        "elapsed_ms": elapsed_ms,
        "stdout": {
            "path": str(stdout_path),
            "sha256": _sha256(stdout_path.read_bytes()),
        },
        "stderr": {
            "path": str(stderr_path),
            "sha256": _sha256(stderr_path.read_bytes()),
        },
        "arm_verdict": (
            None
            if verdict_path is None
            else {
                "path": str(verdict_path),
                "raw_sha256": verdict_sha256,
                "canonical_sha256": verdict_canonical_sha256,
                "run_id": verdict_run_id,
            }
        ),
        "evidence_error": evidence_error,
    }


def run_campaign(
    repository: Path,
    output_directory: Path,
    kauri_revision: str,
) -> Path:
    """Execute all 15 frozen slots once and return the summary artifact."""

    repository = repository.resolve()
    output_directory = output_directory.resolve()
    if _REVISION.fullmatch(kauri_revision) is None:
        raise CampaignRunError(
            "Kauri revision must be 40 lowercase hexadecimal characters"
        )
    arm_runner = repository / "experiments/adaptive/run_fault_comparison.py"
    if not arm_runner.is_file():
        raise CampaignRunError(f"fault comparison runner is absent: {arm_runner}")
    try:
        output_directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    except OSError as error:
        raise CampaignRunError(
            f"cannot create campaign directory {output_directory}: {error}"
        ) from error

    planned: list[dict[str, Any]] = []
    commands: list[list[str]] = []
    for ordinal, (repetition, arm) in enumerate(
        FROZEN_ATTEMPT_SCHEDULE,
        start=1,
    ):
        results_root = output_directory / f"attempt-{ordinal:02d}-results"
        command = [
            sys.executable,
            str(arm_runner),
            "--arm",
            arm,
            "--repository",
            str(repository),
            "--results-root",
            str(results_root),
        ]
        commands.append(command)
        planned.append(
            {
                "ordinal": ordinal,
                "campaign_repetition": repetition,
                "arm": arm,
                "results_root": str(results_root),
                "command": command,
            }
        )
    plan = {
        "schema_version": 1,
        "scenario": "n7-static-fault-repetition-plan",
        "kauri_revision": kauri_revision,
        "repetitions_per_arm": REPETITIONS_PER_ARM,
        "scheduled_attempt_count": len(planned),
        "retry_policy": "none",
        "execution_order": "sequential",
        "scheduled_attempts": planned,
    }
    try:
        _write_exclusive(output_directory / PLAN_NAME, _json_bytes(plan))
    except OSError as error:
        raise CampaignRunError(f"cannot persist campaign plan: {error}") from error

    attempts: list[dict[str, Any]] = []
    execution_records: list[dict[str, Any]] = []
    comparison = _comparison(kauri_revision)
    for plan_item, command in zip(planned, commands, strict=True):
        ordinal = int(plan_item["ordinal"])
        repetition = int(plan_item["campaign_repetition"])
        arm = str(plan_item["arm"])
        print(
            f"[{ordinal}/{len(planned)}] START repetition={repetition} "
            f"arm={arm}",
            flush=True,
        )
        started_utc = dt.datetime.now(dt.timezone.utc).isoformat()
        started_ns = time.monotonic_ns()
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            completed = subprocess.CompletedProcess(
                command,
                127,
                stdout="",
                stderr=f"cannot launch scheduled attempt: {error}",
            )
        elapsed_ms = (time.monotonic_ns() - started_ns) // 1_000_000
        finished_utc = dt.datetime.now(dt.timezone.utc).isoformat()
        stdout_path = output_directory / f"attempt-{ordinal:02d}.stdout.txt"
        stderr_path = output_directory / f"attempt-{ordinal:02d}.stderr.txt"
        stdout = completed.stdout if isinstance(completed.stdout, str) else ""
        stderr = completed.stderr if isinstance(completed.stderr, str) else ""
        _write_exclusive(stdout_path, stdout.encode("utf-8"))
        _write_exclusive(stderr_path, stderr.encode("utf-8"))

        results_root = Path(str(plan_item["results_root"]))
        verdict_paths = sorted(results_root.rglob("arm-verdict.json"))
        verdict_path: Path | None = None
        verdict_sha256: str | None = None
        verdict_canonical_sha256: str | None = None
        verdict_run_id: str | None = None
        evidence_error: str | None = None
        observed_verdict = "MISSING"
        if len(verdict_paths) != 1:
            evidence_error = (
                "missing arm verdict"
                if not verdict_paths
                else "multiple arm verdicts in one scheduled slot"
            )
        else:
            verdict_path = verdict_paths[0].resolve()
            try:
                verdict_payload = verdict_path.read_bytes()
                verdict_sha256 = _sha256(verdict_payload)
                try:
                    verdict_source = verdict_payload.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise CampaignRunError(
                        f"invalid arm verdict {verdict_path}: {error}"
                    ) from error
                try:
                    verdict = parse_thesis_json_object(
                        verdict_source,
                        f"arm verdict {verdict_path}",
                    )
                except ValueError as error:
                    raise CampaignRunError(
                        f"invalid arm verdict {verdict_path}: {error}"
                    ) from error
                existing_repetition = verdict.get("campaign_repetition")
                if existing_repetition not in (None, repetition):
                    raise CampaignRunError(
                        "arm verdict carries the wrong campaign repetition"
                    )
                if verdict.get("verdict") == "PASS" and completed.returncode != 0:
                    raise CampaignRunError(
                        "PASS arm verdict has a non-zero runner exit"
                    )
                normalized = normalize_n7_fault_campaign_attempt(
                    comparison,
                    verdict,
                    campaign_repetition=repetition,
                    arm=arm,
                )
                semantic_verdict = deepcopy(normalized)
                semantic_verdict.pop("campaign_repetition", None)
                verdict_canonical_sha256 = semantic_n7_campaign_sha256(
                    semantic_verdict
                )
                verdict_run_id = str(normalized["run_id"])
                attempts.append(normalized)
                observed_verdict = str(normalized.get("verdict"))
            except (
                CampaignRunError,
                N7FaultRepetitionCampaignError,
                OSError,
            ) as error:
                evidence_error = str(error)
                observed_verdict = "INVALID"

        record = _execution_record(
            ordinal=ordinal,
            repetition=repetition,
            arm=arm,
            command=command,
            completed=completed,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            results_root=results_root,
            verdict_path=verdict_path,
            verdict_sha256=verdict_sha256,
            verdict_canonical_sha256=verdict_canonical_sha256,
            verdict_run_id=verdict_run_id,
            evidence_error=evidence_error,
            started_utc=started_utc,
            finished_utc=finished_utc,
            elapsed_ms=elapsed_ms,
        )
        record["execution_record_sha256"] = semantic_n7_campaign_sha256(record)
        execution_records.append(record)
        record_path = output_directory / f"attempt-{ordinal:02d}-execution.json"
        _write_exclusive(record_path, _json_bytes(record))
        print(
            f"[{ordinal}/{len(planned)}] END rc={completed.returncode} "
            f"verdict={observed_verdict}",
            flush=True,
        )

    try:
        summary = summarize_n7_fault_repetition_campaign(
            comparison,
            attempts,
            build_n7_execution_binding(plan, execution_records),
        )
        artifact = output_directory / ARTIFACT_NAME
        _write_exclusive(
            artifact,
            (
                canonical_n7_fault_repetition_json(summary) + "\n"
            ).encode("utf-8"),
        )
    except (N7FaultRepetitionCampaignError, OSError) as error:
        raise CampaignRunError(f"cannot finalize campaign: {error}") from error
    return artifact


def _current_revision(repository: Path) -> str:
    process = subprocess.Popen(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = process.communicate()
    revision = stdout.strip()
    if process.returncode != 0 or _REVISION.fullmatch(revision) is None:
        detail = stderr.strip() or "git did not return a full revision"
        raise CampaignRunError(f"cannot resolve Kauri revision: {detail}")
    return revision


def _default_output(repository: Path) -> Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    identifier = f"{stamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    return repository / "results/n7-fault-repetition-campaign" / identifier


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--kauri-revision")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    repository = arguments.repository.resolve()
    try:
        revision = arguments.kauri_revision or _current_revision(repository)
        output = arguments.output_dir or _default_output(repository)
        artifact = run_campaign(repository, output, revision)
        summary = parse_thesis_json_object(
            artifact.read_text(encoding="utf-8"),
            "N=7 repetition campaign",
        )
    except (CampaignRunError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(artifact)
    return 0 if summary.get("verdict") == "PASS" else 1


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
