"""RED contract tests for one matched N=7 containment/adaptive pair.

These fixtures are synthetic and are never experiment evidence.  The tests
deliberately exercise the public pair boundary from preserved, hash-bound arm
artifacts without launching replicas or mutating production files.
"""

from __future__ import annotations

import functools
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any, Callable, Mapping

import pytest

import analysis
import run as campaign
import synthetic_run
import validator


SCENARIO_DIRECTORY = Path(__file__).resolve().parents[1]
RUN_PAIR_PATH = SCENARIO_DIRECTORY / "run_pair.py"

PAIR_ID = "synthetic-pair-non-evidence"
CONTROL_PROFILE_ID = "n7-f2-q5-crash-recovery-containment-control-v1"
ADAPTIVE_PROFILE_ID = "n7-f2-q5-crash-recovery-matched-adaptive-v1"

CONTROL_PHASES = (
    ("baseline", 0),
    ("degraded", 0),
    ("containment", 1),
    ("control_late", 1),
)
ADAPTIVE_PHASES = (
    ("baseline", 0),
    ("degraded", 0),
    ("containment", 1),
    ("optimized", 2),
)


@functools.cache
def run_pair() -> Any:
    assert RUN_PAIR_PATH.is_file(), (
        "missing matched-pair entry point: "
        "experiments/adaptive/n7-crash-recovery/run_pair.py"
    )
    spec = importlib.util.spec_from_file_location(
        "n7_crash_recovery_run_pair",
        RUN_PAIR_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _phase_specs(
    phases: tuple[tuple[str, int], ...],
) -> list[dict[str, object]]:
    return [
        {
            "phase": phase,
            "epoch_number": epoch,
            "bucket_count": 7,
        }
        for phase, epoch in phases
    ]


def _profile(
    *,
    profile_id: str,
    phases: tuple[tuple[str, int], ...],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "profile_id": profile_id,
        "frozen": True,
        "throughput_windows": _phase_specs(phases),
    }


def _bucket(index: int, phase: str, tps: float) -> Any:
    return analysis.ThroughputBucket(
        bucket_index=index,
        phase=phase,
        start_ns=index * 5_000_000_000,
        end_ns=(index + 1) * 5_000_000_000,
        elapsed_seconds=5.0,
        commit_count=1,
        transaction_count=int(tps * 5),
        aggregate_tps=tps,
        leader_transactions=(int(tps * 5), 0, 0, 0, 0, 0, 0),
        leader_tps=(tps, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    )


def _create_raw_arm(root: Path, arm: str) -> tuple[Path, Path]:
    if arm == "control":
        return synthetic_run.create_paired_control_run(root)
    return synthetic_run.create_paired_adaptive_run(root)


def _create_validated_arm(root: Path, arm: str) -> Path:
    manifest, epochs = _create_raw_arm(root, arm)
    verdict = validator.validate_run(manifest, epochs, root / "validated")
    assert verdict["verdict"] == "PASS"
    return root


@pytest.fixture
def matched_arms(tmp_path: Path) -> tuple[Path, Path]:
    control = _create_validated_arm(tmp_path / "control", "control")
    adaptive = _create_validated_arm(tmp_path / "adaptive", "adaptive")
    return control, adaptive


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _save_json(path: Path, value: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _phase_medians(run_directory: Path) -> dict[str, float]:
    validation = _load_json(
        run_directory / "validated" / "validation.json"
    )
    return dict(validation["metrics"]["phase_median_tps"])


def _lower_optimized_throughput(run_directory: Path) -> None:
    for replica in validator.SURVIVING_REPLICAS:
        stream = run_directory / "raw" / f"replica-{replica}.jsonl"
        values = [
            json.loads(line)
            for line in stream.read_text(encoding="utf-8").splitlines()
            if line
        ]
        for value in values:
            payload = value.get("payload")
            if (
                value.get("event_type")
                in ("block.commit_observed", "block.committed")
                and isinstance(payload, dict)
                and int(payload.get("block_height", 0)) >= 33
            ):
                payload["transaction_count"] = 1
        stream.write_text(
            "".join(
                json.dumps(value, sort_keys=True) + "\n"
                for value in values
            ),
            encoding="utf-8",
        )


def _mutate_bound_manifest(
    run_directory: Path,
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    canonical_manifest = run_directory / "validated" / "manifest.json"
    manifest = _load_json(canonical_manifest)
    mutation(manifest)
    _save_json(canonical_manifest, manifest)
    (run_directory / "manifest.json").write_bytes(
        canonical_manifest.read_bytes()
    )
    validation_path = run_directory / "validated" / "validation.json"
    validation = _load_json(validation_path)
    validation["artifacts"]["manifest"]["sha256"] = hashlib.sha256(
        canonical_manifest.read_bytes()
    ).hexdigest()
    _save_json(validation_path, validation)


def test_profile_windows_accept_adaptive_and_containment_control_shapes() -> None:
    adaptive = campaign._profile_throughput_windows(  # type: ignore[attr-defined]
        _profile(
            profile_id=ADAPTIVE_PROFILE_ID,
            phases=ADAPTIVE_PHASES,
        )
    )
    control = campaign._profile_throughput_windows(  # type: ignore[attr-defined]
        _profile(
            profile_id=CONTROL_PROFILE_ID,
            phases=CONTROL_PHASES,
        )
    )

    assert [(item["phase"], item["epoch_number"]) for item in adaptive] == list(
        ADAPTIVE_PHASES
    )
    assert [(item["phase"], item["epoch_number"]) for item in control] == list(
        CONTROL_PHASES
    )


def test_final_measurement_delay_is_control_only() -> None:
    control = _profile(
        profile_id=CONTROL_PROFILE_ID,
        phases=CONTROL_PHASES,
    )
    adaptive = _profile(
        profile_id=ADAPTIVE_PROFILE_ID,
        phases=ADAPTIVE_PHASES,
    )

    assert campaign._profile_final_measurement_delay_ns(  # type: ignore[attr-defined]
        control
    ) == 40_000_000_000
    assert campaign._profile_final_measurement_delay_ns(  # type: ignore[attr-defined]
        adaptive
    ) == 0


def test_control_phase_medians_expose_containment_and_late_tps() -> None:
    medians = analysis.compute_phase_medians(
        (
            _bucket(0, "baseline", 10.0),
            _bucket(1, "degraded", 1.0),
            _bucket(2, "containment", 8.0),
            _bucket(3, "control_late", 10.0),
            _bucket(4, "control_late", 14.0),
        )
    )

    assert medians.baseline_tps == pytest.approx(10.0)
    assert medians.degraded_tps == pytest.approx(1.0)
    assert medians.containment_tps == pytest.approx(8.0)
    assert medians.control_late_tps == pytest.approx(12.0)
    assert medians.optimized_tps is None


def test_pair_verdict_reports_primary_and_secondary_normalized_effects(
    matched_arms: tuple[Path, Path],
) -> None:
    control, adaptive = matched_arms

    verdict = run_pair().build_pair_verdict(PAIR_ID, control, adaptive)

    assert verdict["verdict"] == "PASS"
    assert verdict["pair_id"] == PAIR_ID
    assert verdict["control_run_id"] == _load_json(
        control / "manifest.json"
    )["run_id"]
    assert verdict["adaptive_run_id"] == _load_json(
        adaptive / "manifest.json"
    )["run_id"]
    control_medians = _phase_medians(control)
    adaptive_medians = _phase_medians(adaptive)
    adaptive_ratio = (
        adaptive_medians["optimized"]
        / adaptive_medians["containment"]
    )
    control_ratio = (
        control_medians["control_late"]
        / control_medians["containment"]
    )
    metrics = verdict["metrics"]
    assert metrics["adaptive_optimized_to_containment_ratio"] == pytest.approx(
        adaptive_ratio
    )
    assert metrics["paired_effect_ratio"] == pytest.approx(
        adaptive_ratio / control_ratio
    )
    assert metrics["normalized_late_to_baseline_ratio"] == pytest.approx(
        (adaptive_medians["optimized"] / adaptive_medians["baseline"])
        / (control_medians["control_late"] / control_medians["baseline"])
    )


def test_unfavorable_valid_pair_passes_without_claiming_improvement(
    tmp_path: Path,
) -> None:
    control = _create_validated_arm(tmp_path / "control", "control")
    adaptive = tmp_path / "adaptive"
    manifest, epochs = _create_raw_arm(adaptive, "adaptive")
    _lower_optimized_throughput(adaptive)
    validation = validator.validate_run(
        manifest, epochs, adaptive / "validated"
    )
    assert validation["metrics"]["optimized_to_containment_ratio"] < 1.0

    verdict = run_pair().build_pair_verdict(PAIR_ID, control, adaptive)

    assert verdict["verdict"] == "PASS"
    metrics = verdict["metrics"]
    assert metrics["adaptive_optimized_to_containment_ratio"] <= 1.0
    assert metrics["paired_effect_ratio"] <= 1.0
    outcome_flags = [
        container[key]
        for container in (verdict, metrics)
        for key in ("hypothesis_observed", "performance_improved")
        if key in container
    ]
    assert outcome_flags, "pair verdict must expose a boolean outcome flag"
    assert all(type(value) is bool and value is False for value in outcome_flags)


def test_pair_verdict_rejects_tampered_validation_metrics(
    matched_arms: tuple[Path, Path],
) -> None:
    control, adaptive = matched_arms
    validation_path = adaptive / "validated" / "validation.json"
    validation = _load_json(validation_path)
    validation["metrics"]["phase_median_tps"]["optimized"] += 100.0
    _save_json(validation_path, validation)

    with pytest.raises((ValueError, RuntimeError), match="metric|canonical|tamper"):
        run_pair().build_pair_verdict(PAIR_ID, control, adaptive)


def test_pair_verdict_rejects_a_non_pass_arm(
    matched_arms: tuple[Path, Path],
) -> None:
    control, adaptive = matched_arms
    validation_path = adaptive / "validated" / "validation.json"
    validation = _load_json(validation_path)
    validation["verdict"] = "INCOMPLETE"
    _save_json(validation_path, validation)

    with pytest.raises((ValueError, RuntimeError), match="PASS|verdict"):
        run_pair().build_pair_verdict(PAIR_ID, control, adaptive)


@pytest.mark.parametrize(
    ("label", "mutation", "message"),
    (
        (
            "revision",
            lambda manifest: manifest.__setitem__(
                "kauri_revision", "d" * 40
            ),
            "revision",
        ),
        (
            "executable",
            lambda manifest: manifest["runtime"]["executables"][
                "hotstuff_app"
            ].__setitem__("sha256", "e" * 64),
            "executable|binary|sha256",
        ),
        (
            "seed",
            lambda manifest: manifest["runtime"].__setitem__(
                "snapshot_seed", campaign.SNAPSHOT_SEED + 1
            ),
            "seed",
        ),
    ),
)
def test_pair_verdict_rejects_mismatched_frozen_inputs(
    matched_arms: tuple[Path, Path],
    label: str,
    mutation: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    del label
    control, adaptive = matched_arms
    _mutate_bound_manifest(adaptive, mutation)

    with pytest.raises((ValueError, RuntimeError), match=message):
        run_pair().build_pair_verdict(PAIR_ID, control, adaptive)


@pytest.mark.parametrize(
    "corruption", ("profile", "profile_sha", "phases")
)
def test_pair_verdict_rejects_wrong_profile_or_phase_shape(
    matched_arms: tuple[Path, Path],
    corruption: str,
) -> None:
    control, adaptive = matched_arms

    def mutate(manifest: dict[str, Any]) -> None:
        if corruption == "profile":
            manifest["profile"]["identity"] = ADAPTIVE_PROFILE_ID
        elif corruption == "profile_sha":
            manifest["profile"]["sha256"] = "f" * 64
        else:
            manifest["runtime"]["throughput_windows"][-1] = {
                "phase": "optimized",
                "epoch_number": 2,
                "bucket_count": 7,
            }

    _mutate_bound_manifest(control, mutate)

    with pytest.raises(
        (ValueError, RuntimeError),
        match="profile|phase|control|sha",
    ):
        run_pair().build_pair_verdict(PAIR_ID, control, adaptive)


def test_pair_verdict_rejects_broken_validation_manifest_hash(
    matched_arms: tuple[Path, Path],
) -> None:
    control, adaptive = matched_arms
    manifest_path = adaptive / "validated" / "manifest.json"
    manifest = _load_json(manifest_path)
    manifest["runtime"]["snapshot_seed"] = campaign.SNAPSHOT_SEED + 1
    _save_json(manifest_path, manifest)

    with pytest.raises((ValueError, RuntimeError), match="hash|sha256"):
        run_pair().build_pair_verdict(PAIR_ID, control, adaptive)
