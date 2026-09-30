from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kauri_experiment import operator_capacity_v3_pair_evaluator as subject


def _write(path: Path, value: object) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
    path.write_bytes(raw)
    return raw


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _arm(tmp_path: Path, name: str, *, count: int = 12, window_ns: int = subject.WINDOW_NS,
         epoch0: str = "e" * 64, base_profile_sha: str = "d" * 64,
         binary_sha: str = "b" * 64) -> tuple[Path, Path, Path]:
    root = tmp_path / name
    artifacts = {
        "materialization_manifest_sha256": ("materialization-manifest.json", {
            "artifact": "manifest", "arm": name, "binary_sha256": {"app": binary_sha},
        }),
        "runner_receipt_sha256": ("runtime/local-shakedown-receipt.json", {"artifact": "receipt", "arm": name}),
        "cpu_quota_frozen_contract_sha256": ("runtime/frozen-cpu-quota-contract.json", {
            "base_profile_id": "n31-static-resource-cpu-sham-v1",
            "base_profile_sha256": base_profile_sha,
            "base_profile_canonical_sha256": "e" * 64,
        }),
        "cpu_quota_contract_sha256": ("runtime/cpu-quota-contract.json", {"artifact": "contract", "arm": name}),
        "cpu_quota_launch_sha256": ("runtime/cpu-quota-launch.json", {"artifact": "launch", "arm": name}),
        "cpu_quota_samples_sha256": ("raw/cpu-quota-samples.jsonl", {"artifact": "samples", "arm": name}),
        "cpu_quota_rounds_sha256": ("raw/cpu-quota-monitor-rounds.jsonl", {"artifact": "rounds", "arm": name}),
    }
    authority: dict[str, object] = {"schema_version": 1, "kind": subject._AUTHORITY_KIND,
                                      "event_stream_sha256": {}, "pins": {
                                          "arm": name, "source_revision": "a" * 40,
                                          "epoch0_consensus_digest": epoch0,
                                          "epoch0_topology_digest": "t" * 64,
                                          "approved_capacity_digest": "c" * 64,
                                          "run_id": f"run-{name}",
                                      }}
    for key, (relative, value) in artifacts.items():
        authority[key] = _sha(_write(root / relative, value))
    streams: dict[str, str] = {}
    streams["manager"] = _sha(_write(root / "raw/manager-events.jsonl", {"source": "manager", "arm": name}))
    for replica in range(subject.N):
        streams[f"replica-{replica}"] = _sha(_write(root / f"raw/replica-{replica}.jsonl", {"source": replica, "arm": name}))
    authority["event_stream_sha256"] = streams
    authority_path = tmp_path / f"{name}-authority.json"
    authority_raw = _write(authority_path, authority)
    start = 1_000_000_000
    commits = [{"height": item + 1, "block_hash": f"{item + 1:064x}",
                "designated_ns": start + item, "completion_ns": start + item + 1}
               for item in range(count)]
    raw = {"schema_version": 1, "kind": subject._RAW_KIND, "verdict": "COMPLETE_NO_CLAIM",
           "claim_eligible": False, "figure_eligible": False,
           "complete_common_commit_count": count, "common_commits": commits,
           "measurement_window": {"start_monotonic_ns": start, "end_monotonic_ns": start + window_ns},
           "consumption_chain": {"arm": name,
                                 "stage_a_wire_sha256": ("1" if name == "sham" else "2") * 64,
                                 "stage_b_authorization_wire_sha256": "3" * 64}}
    raw_path = tmp_path / f"{name}-raw-validation.json"
    _write(raw_path, raw)
    return root, raw_path, authority_path


def _manifest(sham_raw: Path, sham_authority: Path, treatment_raw: Path, treatment_authority: Path) -> dict[str, object]:
    return {"schema_version": 1, "kind": subject._MANIFEST_KIND, "pair_id": "w18-001",
            "measurement": {"epoch": 1, "window_ns": subject.WINDOW_NS,
                            "commit_metric": "all-31-common-commits-v1"},
            "effect_statistic": "treatment_to_sham_common_commit_ratio_v1",
            "threshold_numerator": 110, "threshold_denominator": 100,
            "arms": {"sham": {"raw_validation_sha256": _sha(sham_raw.read_bytes()),
                                "authority_sha256": _sha(sham_authority.read_bytes())},
                     "treatment": {"raw_validation_sha256": _sha(treatment_raw.read_bytes()),
                                   "authority_sha256": _sha(treatment_authority.read_bytes())}}}


def _revalidator(*items: tuple[Path, Path]):
    frozen = {str(authority.resolve()): json.loads(raw.read_text(encoding="ascii"))
              for raw, authority in items}
    return lambda _root, authority: frozen[str(authority.resolve())]


def _evaluate(tmp_path: Path, *, sham_count: int = 10, treatment_count: int = 12, **kwargs: object) -> dict[str, object]:
    sham_root, sham_raw, sham_authority = _arm(tmp_path, "sham", count=sham_count)
    treatment_root, treatment_raw, treatment_authority = _arm(tmp_path, "treatment", count=treatment_count, **kwargs)
    return subject.evaluate_matched_pair(
        _manifest(sham_raw, sham_authority, treatment_raw, treatment_authority),
        sham_root=sham_root, treatment_root=treatment_root,
        sham_raw_validation=sham_raw, treatment_raw_validation=treatment_raw,
        sham_authority=sham_authority, treatment_authority=treatment_authority,
        revalidate=_revalidator((sham_raw, sham_authority), (treatment_raw, treatment_authority)),
    )


def test_complete_pair_exposes_frozen_ratio_but_never_a_claim(tmp_path: Path) -> None:
    result = _evaluate(tmp_path)
    assert result["verdict"] == "PAIR_COMPLETE_DESCRIPTIVE_ONLY"
    assert result["ratio"] == {"numerator": 12, "denominator": 10}
    assert result["meets_practical_threshold"] is True
    assert result["claim_eligible"] is False
    assert result["figure_eligible"] is False
    assert result["campaign_eligible"] is False


def test_pair_rejects_raw_artifact_drift_after_external_pin(tmp_path: Path) -> None:
    sham_root, sham_raw, sham_authority = _arm(tmp_path, "sham")
    treatment_root, treatment_raw, treatment_authority = _arm(tmp_path, "treatment")
    (treatment_root / "raw/replica-7.jsonl").write_text('{"tampered":true}\n', encoding="ascii")
    with pytest.raises(subject.PairEvaluationError, match="replica-7 stream differs"):
        subject.evaluate_matched_pair(
            _manifest(sham_raw, sham_authority, treatment_raw, treatment_authority),
            sham_root=sham_root, treatment_root=treatment_root,
            sham_raw_validation=sham_raw, treatment_raw_validation=treatment_raw,
            sham_authority=sham_authority, treatment_authority=treatment_authority,
            revalidate=_revalidator((sham_raw, sham_authority), (treatment_raw, treatment_authority)),
        )


def test_pair_rejects_non_symmetric_window(tmp_path: Path) -> None:
    with pytest.raises(subject.PairEvaluationError, match="exact 30-second"):
        _evaluate(tmp_path, window_ns=subject.WINDOW_NS - 1)


def test_pair_rejects_mismatched_epoch_zero_identity(tmp_path: Path) -> None:
    sham_root, sham_raw, sham_authority = _arm(tmp_path, "sham")
    treatment_root, treatment_raw, treatment_authority = _arm(tmp_path, "treatment", epoch0="f" * 64)
    with pytest.raises(subject.PairEvaluationError, match="epoch0_consensus_digest"):
        subject.evaluate_matched_pair(
            _manifest(sham_raw, sham_authority, treatment_raw, treatment_authority),
            sham_root=sham_root, treatment_root=treatment_root,
            sham_raw_validation=sham_raw, treatment_raw_validation=treatment_raw,
            sham_authority=sham_authority, treatment_authority=treatment_authority,
            revalidate=_revalidator((sham_raw, sham_authority), (treatment_raw, treatment_authority)),
        )


def test_pair_rejects_zero_sham_denominator(tmp_path: Path) -> None:
    with pytest.raises(subject.PairEvaluationError, match="denominator is not positive"):
        _evaluate(tmp_path, sham_count=0, treatment_count=1)


def test_manifest_hash_binds_raw_result_and_threshold_is_exact(tmp_path: Path) -> None:
    sham_root, sham_raw, sham_authority = _arm(tmp_path, "sham", count=10)
    treatment_root, treatment_raw, treatment_authority = _arm(tmp_path, "treatment", count=10)
    manifest = _manifest(sham_raw, sham_authority, treatment_raw, treatment_authority)
    manifest["threshold_numerator"] = 101
    manifest["threshold_denominator"] = 100
    assert subject.evaluate_matched_pair(
        manifest, sham_root=sham_root, treatment_root=treatment_root,
        sham_raw_validation=sham_raw, treatment_raw_validation=treatment_raw,
        sham_authority=sham_authority, treatment_authority=treatment_authority,
        revalidate=_revalidator((sham_raw, sham_authority), (treatment_raw, treatment_authority)),
    )["meets_practical_threshold"] is False
    raw = json.loads(treatment_raw.read_text(encoding="ascii"))
    raw["complete_common_commit_count"] = 11
    _write(treatment_raw, raw)
    with pytest.raises(subject.PairEvaluationError, match="raw validation differs from manifest pin"):
        subject.evaluate_matched_pair(
            manifest, sham_root=sham_root, treatment_root=treatment_root,
            sham_raw_validation=sham_raw, treatment_raw_validation=treatment_raw,
            sham_authority=sham_authority, treatment_authority=treatment_authority,
            revalidate=_revalidator((sham_raw, sham_authority), (treatment_raw, treatment_authority)),
        )


def test_pair_requires_independent_recomputation_to_match_persisted_result(tmp_path: Path) -> None:
    sham_root, sham_raw, sham_authority = _arm(tmp_path, "sham")
    treatment_root, treatment_raw, treatment_authority = _arm(tmp_path, "treatment")
    revalidate = _revalidator((sham_raw, sham_authority), (treatment_raw, treatment_authority))
    forged = json.loads(treatment_raw.read_text(encoding="ascii"))
    forged["complete_common_commit_count"] += 1
    forged["common_commits"].append({"height": 99, "block_hash": "f" * 64,
                                      "designated_ns": 1_000_000_100,
                                      "completion_ns": 1_000_000_101})
    _write(treatment_raw, forged)
    with pytest.raises(subject.PairEvaluationError, match="independent recomputation"):
        subject.evaluate_matched_pair(
            _manifest(sham_raw, sham_authority, treatment_raw, treatment_authority),
            sham_root=sham_root, treatment_root=treatment_root,
            sham_raw_validation=sham_raw, treatment_raw_validation=treatment_raw,
            sham_authority=sham_authority, treatment_authority=treatment_authority,
            revalidate=revalidate,
        )


def test_pair_requires_identical_frozen_cpu_and_binary_identity(tmp_path: Path) -> None:
    sham_root, sham_raw, sham_authority = _arm(tmp_path, "sham")
    treatment_root, treatment_raw, treatment_authority = _arm(
        tmp_path, "treatment", base_profile_sha="9" * 64, binary_sha="8" * 64)
    with pytest.raises(subject.PairEvaluationError, match="frozen CPU quota contract"):
        subject.evaluate_matched_pair(
            _manifest(sham_raw, sham_authority, treatment_raw, treatment_authority),
            sham_root=sham_root, treatment_root=treatment_root,
            sham_raw_validation=sham_raw, treatment_raw_validation=treatment_raw,
            sham_authority=sham_authority, treatment_authority=treatment_authority,
            revalidate=_revalidator((sham_raw, sham_authority), (treatment_raw, treatment_authority)),
        )


def test_pair_rejects_binary_identity_drift_after_matching_cpu_contract(tmp_path: Path) -> None:
    sham_root, sham_raw, sham_authority = _arm(tmp_path, "sham")
    treatment_root, treatment_raw, treatment_authority = _arm(tmp_path, "treatment", binary_sha="8" * 64)
    with pytest.raises(subject.PairEvaluationError, match="materialized binary identity"):
        subject.evaluate_matched_pair(
            _manifest(sham_raw, sham_authority, treatment_raw, treatment_authority),
            sham_root=sham_root, treatment_root=treatment_root,
            sham_raw_validation=sham_raw, treatment_raw_validation=treatment_raw,
            sham_authority=sham_authority, treatment_authority=treatment_authority,
            revalidate=_revalidator((sham_raw, sham_authority), (treatment_raw, treatment_authority)),
        )


def test_external_writer_uses_canonical_bytes_and_refuses_reuse(tmp_path: Path) -> None:
    root, raw, authority = _arm(tmp_path, "sham")
    output = tmp_path / "persisted-result.json"
    result = subject.write_canonical_raw_validation_result(
        root=root, authority_path=authority, output_path=output,
        revalidate=_revalidator((raw, authority)),
    )
    assert json.loads(output.read_text(encoding="ascii")) == result
    with pytest.raises(subject.PairEvaluationError, match="fresh file"):
        subject.write_canonical_raw_validation_result(
            root=root, authority_path=authority, output_path=output,
            revalidate=_revalidator((raw, authority)),
        )
