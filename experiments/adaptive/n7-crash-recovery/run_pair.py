#!/usr/bin/env python3
"""Run and bind one frozen N=7 containment-control/adaptive pair.

Pair validity and performance direction are deliberately separate.  Both arms
must independently produce canonical PASS artifacts and share their frozen
execution inputs before this module reports the ratio-of-ratios.  A valid but
unfavourable observation remains a PASS with ``hypothesis_observed=false``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence
import uuid

import run as campaign
import validator


PAIR_SCENARIO = "n7-matched-containment-adaptive-pair"
PAIR_VERDICT_SCHEMA_VERSION = 2
CONTROL_PROFILE = Path(__file__).resolve().with_name(
    "profile-paired-control.json"
)
ADAPTIVE_PROFILE = Path(__file__).resolve().with_name(
    "profile-paired-adaptive.json"
)
EXPECTED_PHASES = {
    "control": (
        ("baseline", 0),
        ("degraded", 0),
        ("containment", 1),
        ("control_late", 1),
    ),
    "adaptive": (
        ("baseline", 0),
        ("degraded", 0),
        ("containment", 1),
        ("optimized", 2),
    ),
}
EXPECTED_PROFILES = {
    "control": campaign.PAIRED_CONTROL_PROFILE_ID,
    "adaptive": campaign.PAIRED_ADAPTIVE_PROFILE_ID,
}
EXPECTED_PROFILE_SHA256 = {
    "control": campaign.PAIRED_CONTROL_PROFILE_SHA256,
    "adaptive": campaign.PAIRED_ADAPTIVE_PROFILE_SHA256,
}
MATCHED_RUNTIME_FIELDS = (
    "snapshot_seed",
    "epoch0_roots",
    "fanout",
    "pipeline_depth",
    "tree_switch_period_blocks",
    "block_size",
    "activation_delay_blocks",
    "aggregation_timeout_ms",
    "leader_progress_timeout_ms",
    "leader_activation_grace_ms",
)


class PairError(RuntimeError):
    """The two arm artifacts do not form one valid matched pair."""


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PairError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise PairError(f"{label} must be a JSON object")
    return value


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _hash(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise PairError(f"{label} must be a lowercase SHA-256")
    return value


def _positive_metric(value: Any, label: str) -> float:
    if type(value) not in (int, float):
        raise PairError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise PairError(f"{label} must be finite and positive")
    return result


def _expected_window_specs(arm: str) -> list[dict[str, Any]]:
    return [
        {"phase": phase, "epoch_number": epoch, "bucket_count": 7}
        for phase, epoch in EXPECTED_PHASES[arm]
    ]


def _evaluation_phase_medians(
    evaluation: validator.Evaluation,
    arm: str,
) -> dict[str, float]:
    values = evaluation.throughput.medians
    by_phase = {
        "baseline": values.baseline_tps,
        "degraded": values.degraded_tps,
        "containment": values.containment_tps,
        "control_late": values.control_late_tps,
        "optimized": values.optimized_tps,
    }
    return {
        phase: _positive_metric(
            by_phase[phase],
            f"{arm} canonical {phase} median TPS",
        )
        for phase, _ in EXPECTED_PHASES[arm]
    }


def _containment_successor(
    epochs: validator.EpochDocument,
    arm: str,
) -> dict[str, Any]:
    matches = [
        epoch
        for epoch in epochs.epochs
        if epoch.epoch_number == 1
    ]
    if len(matches) != 1:
        raise PairError(
            f"{arm} epochs require exactly one containment epoch 1"
        )
    epoch = matches[0]
    digest = _hash(
        epoch.epoch_digest,
        f"{arm} containment epoch digest",
    )
    if not epoch.trees:
        raise PairError(f"{arm} containment epoch trees are invalid")
    return {
        "epoch_number": 1,
        "epoch_digest": digest,
        "trees": [
            {
                "tree_id": tree.tree_id,
                "fanout": tree.fanout,
                "members_breadth_first": list(tree.members),
                "wait_exempt": list(tree.wait_exempt),
            }
            for tree in epoch.trees
        ],
    }


def _validated_arm(
    pair_id: str,
    run_directory: Path,
    arm: str,
) -> dict[str, Any]:
    run_directory = run_directory.resolve()
    validated_directory = run_directory / "validated"
    validation_path = validated_directory / "validation.json"
    try:
        validation_bytes = validation_path.read_bytes()
    except OSError as exc:
        raise PairError(f"cannot read {arm} validation: {exc}") from exc
    validation = _load_json(
        validation_path,
        f"{arm} validation",
    )
    if validation.get("verdict") != "PASS":
        raise PairError(f"{arm} arm does not have a PASS verdict")
    if validation.get("run_complete") is not True:
        raise PairError(f"{arm} PASS artifact is not a complete run")

    artifact = validation.get("artifacts", {}).get("manifest")
    if not isinstance(artifact, dict):
        raise PairError(f"{arm} validation omits its manifest artifact")
    relative = artifact.get("path")
    if not isinstance(relative, str) or Path(relative) != Path("manifest.json"):
        raise PairError(f"{arm} validation manifest path is not canonical")
    canonical_path = validated_directory / relative
    root_manifest_path = run_directory / "manifest.json"
    try:
        canonical_bytes = canonical_path.read_bytes()
    except OSError as exc:
        raise PairError(f"cannot read {arm} manifest: {exc}") from exc
    expected_digest = _hash(
        artifact.get("sha256"),
        f"{arm} validation manifest sha256",
    )
    if _sha256(canonical_bytes) != expected_digest:
        raise PairError(f"{arm} validation manifest hash does not match")
    manifest = _load_json(root_manifest_path, f"{arm} root manifest")
    canonical_manifest = _load_json(
        canonical_path,
        f"{arm} validated manifest",
    )
    if manifest != canonical_manifest:
        raise PairError(
            f"{arm} root manifest differs semantically from the validated manifest"
        )

    if manifest.get("pair_id") != pair_id:
        raise PairError(f"{arm} manifest pair_id differs from the pair")
    if manifest.get("pair_arm") != arm:
        raise PairError(f"{arm} manifest pair_arm is invalid")
    if manifest.get("scenario") not in {
        campaign.SCENARIO,
        PAIR_SCENARIO,
    }:
        raise PairError(f"{arm} manifest scenario is invalid")
    profile = manifest.get("profile")
    if (
        not isinstance(profile, dict)
        or profile.get("identity") != EXPECTED_PROFILES[arm]
    ):
        raise PairError(f"{arm} profile identity is invalid")
    profile_sha256 = _hash(
        profile.get("sha256"),
        f"{arm} profile sha256",
    )
    if profile_sha256 != EXPECTED_PROFILE_SHA256[arm]:
        raise PairError(
            f"{arm} profile sha256 differs from the exact frozen profile"
        )

    run_id = manifest.get("run_id")
    revision = manifest.get("kauri_revision")
    if not isinstance(run_id, str) or not run_id:
        raise PairError(f"{arm} manifest run_id is invalid")
    if (
        not isinstance(revision, str)
        or len(revision) != 40
        or any(character not in "0123456789abcdef" for character in revision)
    ):
        raise PairError(f"{arm} revision is invalid")
    if validation.get("run_id") != run_id:
        raise PairError(f"{arm} validation run identity differs from its manifest")
    if validation.get("kauri_revision") != revision:
        raise PairError(f"{arm} validation revision differs from its manifest")
    if validation.get("profile_identity") != EXPECTED_PROFILES[arm]:
        raise PairError(f"{arm} validation profile differs from its manifest")

    completion = manifest.get("run_completion")
    if (
        not isinstance(completion, dict)
        or completion.get("complete") is not True
        or completion.get("interrupted") is not False
        or completion.get("runtime_error") is not None
        or completion.get("unexpected_survivor_exits") != []
    ):
        raise PairError(f"{arm} manifest is not a successful complete run")
    if (
        manifest.get("replica_count") != 7
        or manifest.get("fault_threshold") != 2
        or manifest.get("quorum") != 5
        or manifest.get("membership") != list(range(7))
        or manifest.get("authoritative_observer")
        not in (2, campaign.AUTHORITATIVE_SOURCE_ID)
    ):
        raise PairError(f"{arm} manifest differs from frozen N=7/f=2/Q=5")
    if "crash_targets" in manifest and manifest["crash_targets"] != [0, 1]:
        raise PairError(f"{arm} crash targets are invalid")

    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict):
        raise PairError(f"{arm} runtime is invalid")
    if runtime.get("snapshot_seed") != campaign.SNAPSHOT_SEED:
        raise PairError(f"{arm} runtime seed is not the frozen snapshot seed")
    if runtime.get("throughput_windows") != _expected_window_specs(arm):
        raise PairError(f"{arm} runtime phase shape is invalid")
    executables = runtime.get("executables")
    if not isinstance(executables, dict):
        raise PairError(f"{arm} runtime omits executable provenance")
    executable_hashes: dict[str, str] = {}
    for name in ("hotstuff_app", "adaptation_manager"):
        descriptor = executables.get(name)
        if not isinstance(descriptor, dict):
            raise PairError(f"{arm} executable provenance is invalid")
        executable_hashes[name] = _hash(
            descriptor.get("sha256"),
            f"{arm} {name} sha256",
        )
    frozen_runtime: dict[str, Any] = {}
    for field in MATCHED_RUNTIME_FIELDS:
        if field not in runtime:
            raise PairError(f"{arm} runtime omits matched field {field}")
        frozen_runtime[field] = runtime[field]

    try:
        evaluation = validator.evaluate(
            root_manifest_path,
            run_directory / "epochs.json",
        )
    except validator.ValidationError as exc:
        raise PairError(
            f"{arm} canonical evidence re-evaluation failed: {exc}"
        ) from exc
    phase_medians = _evaluation_phase_medians(evaluation, arm)
    metrics = validation.get("metrics")
    if not isinstance(metrics, dict):
        raise PairError(f"{arm} validation metrics are invalid")
    persisted_medians = metrics.get("phase_median_tps")
    if not isinstance(persisted_medians, dict):
        raise PairError(f"{arm} validation omits phase median TPS")
    if persisted_medians != phase_medians:
        raise PairError(
            f"{arm} validation metrics differ from canonical raw-evidence replay"
        )
    containment_successor = _containment_successor(
        evaluation.epochs,
        arm,
    )
    return {
        "run_id": run_id,
        "revision": revision,
        "profile_sha256": profile_sha256,
        "runtime": frozen_runtime,
        "executables": executable_hashes,
        "phase_medians": phase_medians,
        "containment_successor": containment_successor,
        "validation_sha256": _sha256(validation_bytes),
        "manifest_artifact_sha256": expected_digest,
    }


def build_pair_verdict(
    pair_id: str,
    control_run_directory: Path,
    adaptive_run_directory: Path,
) -> dict[str, Any]:
    """Return a PASS verdict for two independently valid, matched arm runs."""
    if not isinstance(pair_id, str) or not pair_id:
        raise PairError("pair_id must be non-empty")
    control = _validated_arm(pair_id, control_run_directory, "control")
    adaptive = _validated_arm(pair_id, adaptive_run_directory, "adaptive")

    if control["revision"] != adaptive["revision"]:
        raise PairError("pair arms use different Kauri revisions")
    if control["executables"] != adaptive["executables"]:
        raise PairError("pair arms use different executable binary sha256 values")
    for field in MATCHED_RUNTIME_FIELDS:
        if control["runtime"][field] != adaptive["runtime"][field]:
            label = "seed" if field == "snapshot_seed" else field
            raise PairError(f"pair arms use different matched {label}")
    if (
        control["containment_successor"]
        != adaptive["containment_successor"]
    ):
        raise PairError(
            "pair arms use different containment epoch 1 successors"
        )

    control_medians = control["phase_medians"]
    adaptive_medians = adaptive["phase_medians"]
    adaptive_ratio = (
        adaptive_medians["optimized"]
        / adaptive_medians["containment"]
    )
    control_drift_ratio = (
        control_medians["control_late"]
        / control_medians["containment"]
    )
    paired_effect_ratio = adaptive_ratio / control_drift_ratio
    normalized_late_to_baseline_ratio = (
        adaptive_medians["optimized"] / adaptive_medians["baseline"]
    ) / (
        control_medians["control_late"] / control_medians["baseline"]
    )
    hypothesis_observed = (
        adaptive_ratio > 1.0 and paired_effect_ratio > 1.0
    )
    return {
        "schema_version": PAIR_VERDICT_SCHEMA_VERSION,
        "scenario": PAIR_SCENARIO,
        "pair_id": pair_id,
        "verdict": "PASS",
        "control_run_id": control["run_id"],
        "adaptive_run_id": adaptive["run_id"],
        "kauri_revision": control["revision"],
        "shared_containment_successor": {
            "epoch_number": 1,
            "epoch_digest": control["containment_successor"][
                "epoch_digest"
            ],
            "trees_sha256": _sha256(
                json.dumps(
                    control["containment_successor"]["trees"],
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ),
        },
        "profiles": {
            "control": {
                "identity": EXPECTED_PROFILES["control"],
                "sha256": control["profile_sha256"],
            },
            "adaptive": {
                "identity": EXPECTED_PROFILES["adaptive"],
                "sha256": adaptive["profile_sha256"],
            },
        },
        "arm_artifacts": {
            "control": {
                "validation_sha256": control["validation_sha256"],
                "manifest_sha256": control["manifest_artifact_sha256"],
            },
            "adaptive": {
                "validation_sha256": adaptive["validation_sha256"],
                "manifest_sha256": adaptive["manifest_artifact_sha256"],
            },
        },
        "metrics": {
            "control_phase_median_tps": control_medians,
            "adaptive_phase_median_tps": adaptive_medians,
            "adaptive_optimized_to_containment_ratio": adaptive_ratio,
            "control_late_to_containment_ratio": control_drift_ratio,
            "paired_effect_ratio": paired_effect_ratio,
            "normalized_late_to_baseline_ratio": (
                normalized_late_to_baseline_ratio
            ),
            "hypothesis_observed": hypothesis_observed,
        },
        "hypothesis_observed": hypothesis_observed,
    }


def _safe_pair_id(value: str) -> str:
    if (
        not value
        or len(value) > 128
        or Path(value).name != value
        or any(character not in "abcdefghijklmnopqrstuvwxyz"
               "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in value)
    ):
        raise PairError(
            "pair_id must contain only letters, digits, hyphens, or underscores"
        )
    return value


def _new_pair_id() -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _new_arm_run(
    *,
    pair_id: str,
    arm: str,
    pair_directory: Path,
    common_arguments: Sequence[str],
) -> Path:
    arm_root = pair_directory / arm
    arm_root.mkdir(mode=0o700)
    before = set(arm_root.iterdir())
    status = campaign.run(
        [
            *common_arguments,
            "--profile",
            str(CONTROL_PROFILE if arm == "control" else ADAPTIVE_PROFILE),
            "--results-root",
            str(arm_root),
            "--pair-id",
            pair_id,
            "--pair-arm",
            arm,
        ]
    )
    created = [
        path
        for path in arm_root.iterdir()
        if path not in before and path.is_dir()
    ]
    if len(created) != 1:
        raise PairError(
            f"{arm} arm did not preserve exactly one run directory"
        )
    run_directory = created[0]
    if status != 0:
        raise PairError(
            f"{arm} arm did not produce a canonical PASS: {run_directory}"
        )
    return run_directory


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    scenario_directory = Path(__file__).resolve().parent
    repository = scenario_directory.parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pair-id")
    parser.add_argument("--repository", type=Path, default=repository)
    parser.add_argument(
        "--results-root",
        type=Path,
        default=repository / "results/n7-crash-recovery-pairs",
    )
    parser.add_argument(
        "--app-binary",
        type=Path,
        default=repository / "build-adaptive/examples/hotstuff-app",
    )
    parser.add_argument(
        "--manager-binary",
        type=Path,
        default=repository / "build-adaptive/examples/adaptation-manager",
    )
    parser.add_argument(
        "--keygen-binary",
        type=Path,
        default=repository / "build-adaptive/hotstuff-keygen",
    )
    parser.add_argument(
        "--tls-keygen-binary",
        type=Path,
        default=repository / "build-adaptive/hotstuff-tls-keygen",
    )
    parser.add_argument("--peer-port", type=int, default=25100)
    parser.add_argument("--client-port", type=int, default=26100)
    parser.add_argument("--manager-port", type=int, default=27100)
    parser.add_argument("--startup-timeout", type=float, default=90.0)
    parser.add_argument("--phase-timeout", type=float, default=240.0)
    parser.add_argument("--crash-confirm-timeout", type=float, default=5.0)
    parser.add_argument(
        "--plot",
        action="store_true",
        help="generate each arm's optional PASS-only figure",
    )
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    pair_id = _safe_pair_id(args.pair_id or _new_pair_id())
    results_root = args.results_root.resolve()
    results_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    pair_directory = results_root / pair_id
    pair_directory.mkdir(mode=0o700)

    common_arguments = [
        "--repository",
        str(args.repository.resolve()),
        "--app-binary",
        str(args.app_binary.resolve()),
        "--manager-binary",
        str(args.manager_binary.resolve()),
        "--keygen-binary",
        str(args.keygen_binary.resolve()),
        "--tls-keygen-binary",
        str(args.tls_keygen_binary.resolve()),
        "--peer-port",
        str(args.peer_port),
        "--client-port",
        str(args.client_port),
        "--manager-port",
        str(args.manager_port),
        "--startup-timeout",
        str(args.startup_timeout),
        "--phase-timeout",
        str(args.phase_timeout),
        "--crash-confirm-timeout",
        str(args.crash_confirm_timeout),
        *(("--plot",) if args.plot else ()),
    ]
    control = _new_arm_run(
        pair_id=pair_id,
        arm="control",
        pair_directory=pair_directory,
        common_arguments=common_arguments,
    )
    adaptive = _new_arm_run(
        pair_id=pair_id,
        arm="adaptive",
        pair_directory=pair_directory,
        common_arguments=common_arguments,
    )
    verdict = build_pair_verdict(pair_id, control, adaptive)
    campaign._write_json_exclusive(
        pair_directory / "pair-verdict.json",
        verdict,
    )
    print(f"PASS: {pair_directory / 'pair-verdict.json'}")
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except (PairError, campaign.RunnerError) as exc:
        print(f"pair runner error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
