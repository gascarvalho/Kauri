"""Independent validation gates for the diagnosis-to-policy transfer audit."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from experiments.adaptive import run_diagnosis_policy_transfer as producer
from experiments.adaptive import validate_diagnosis_policy_transfer as verifier


REPOSITORY = Path(__file__).resolve().parents[3]
PROFILE = REPOSITORY / verifier.PROFILE_RELATIVE_PATH


@pytest.fixture(autouse=True)
def allow_uncommitted_test_source(monkeypatch: pytest.MonkeyPatch) -> None:
    # Calculation/mutation tests run before code is committed. The final
    # producer and validator CLIs enforce committed bytes independently.
    monkeypatch.setattr(producer, "_require_committed_source", lambda _path: None)
    monkeypatch.setattr(verifier, "_require_committed_source", lambda _path: None)


def _write_artifact(path: Path, artifact: object) -> None:
    path.write_text(
        json.dumps(
            artifact,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="ascii",
    )


def _artifact(tmp_path: Path) -> Path:
    path = tmp_path / producer.ARTIFACT_NAME
    _write_artifact(path, producer.build())
    return path


def test_validator_independently_reconstructs_all_five_terminal_rankings(
    tmp_path: Path,
) -> None:
    result = verifier.verify(_artifact(tmp_path), PROFILE)

    assert result["verdict"] == "PASS"
    assert result["independently_reconstructed_runs"] == 5
    assert result["accepted_observations_replayed"] == 2647
    assert result["analysis_timing"] == "retrospective_post_hoc"
    assert result["evidence_cutoff"] == "terminal_complete_accepted_manager_prefix"


@pytest.mark.parametrize(
    ("location", "replacement"),
    [
        (("summary", "responsiveness", "reporter_excluded_count"), 1),
        (("runs", 0, "mechanisms", "latency-priority", "reporter_rank"), 0),
        (("runs", 0, "mechanisms", "responsiveness", "influential_ids"), [6, 4, 2]),
        (("runs", 0, "diagnosis", "reporter_id"), 5),
        (("runs", 0, "diagnosis", "durable_role_exclusions"), []),
        (("runs", 0, "mechanisms", "responsiveness", "reporter_scalar_eligible"), False),
        (("summary", "latency-priority", "reporter_scalar_eligible_count"), 4),
        (("runs", 0, "accepted_observation_count"), 548),
        (("fixed_contract", "analysis_timing"), "at_settlement"),
    ],
)
def test_validator_rejects_any_derived_or_scope_tamper(
    tmp_path: Path,
    location: tuple[object, ...],
    replacement: object,
) -> None:
    artifact = deepcopy(producer.build())
    target: object = artifact
    for key in location[:-1]:
        target = target[key]  # type: ignore[index]
    target[location[-1]] = replacement  # type: ignore[index]
    path = tmp_path / producer.ARTIFACT_NAME
    _write_artifact(path, artifact)

    with pytest.raises(
        verifier.VerificationError,
        match="independent exact reconstruction",
    ):
        verifier.verify(path, PROFILE)


def test_validator_rejects_a_dropped_or_duplicated_run(tmp_path: Path) -> None:
    artifact = deepcopy(producer.build())
    artifact["runs"].pop()
    path = tmp_path / "dropped.json"
    _write_artifact(path, artifact)
    with pytest.raises(verifier.VerificationError, match="exact reconstruction"):
        verifier.verify(path, PROFILE)

    artifact = deepcopy(producer.build())
    artifact["runs"][1] = deepcopy(artifact["runs"][0])
    path = tmp_path / "duplicated.json"
    _write_artifact(path, artifact)
    with pytest.raises(verifier.VerificationError, match="exact reconstruction"):
        verifier.verify(path, PROFILE)


def test_validator_rejects_a_source_path_escape_without_following_it(
    tmp_path: Path,
) -> None:
    artifact = deepcopy(producer.build())
    artifact["runs"][0]["sources"]["manager"]["path"] = "../outside.json"
    path = tmp_path / producer.ARTIFACT_NAME
    _write_artifact(path, artifact)

    with pytest.raises(verifier.VerificationError, match="exact reconstruction"):
        verifier.verify(path, PROFILE)


def test_validator_rejects_noncanonical_artifact_encoding(tmp_path: Path) -> None:
    path = tmp_path / producer.ARTIFACT_NAME
    path.write_text(json.dumps(producer.build()), encoding="utf-8")

    with pytest.raises(verifier.VerificationError, match="not canonical JSON"):
        verifier.verify(path, PROFILE)


def test_validator_recomputes_certificate_digest_and_raw_observation_binding() -> None:
    run = verifier.RUNS[0]
    paths = verifier._paths(run)
    manager = REPOSITORY / paths["manager"]
    verdict_path = REPOSITORY / paths["arm_verdict"]
    observations, _digest = verifier._accepted(manager)
    verdict = deepcopy(verifier._read_json(verdict_path, "arm verdict"))
    verdict["diagnostic_certificate"]["settled_hypothesis"][
        "false_reporters"
    ] = [5]

    with pytest.raises(verifier.VerificationError, match="certificate digest"):
        verifier._check_certificate(verdict, run, observations)


def test_independent_ranker_is_reporter_identity_invariant() -> None:
    run = verifier.RUNS[0]
    manager = REPOSITORY / verifier._paths(run)["manager"]
    observations, _digest = verifier._accepted(manager)
    replay_profile = verifier._replay_profile()
    permuted = deepcopy(observations)
    for observation in permuted:
        observation["reporter_id"] = (observation["reporter_id"] + 1) % 7

    for mechanism in verifier.MECHANISMS:
        assert verifier._ranking(
            observations, replay_profile, mechanism
        ) == verifier._ranking(permuted, replay_profile, mechanism)


def test_validator_rejects_source_hash_drift_before_using_source_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_path = _artifact(tmp_path)
    original = verifier._sha_file

    def drift(path: Path) -> str:
        if path.name == "adaptive-manager.jsonl":
            return "0" * 64
        return original(path)

    monkeypatch.setattr(verifier, "_sha_file", drift)
    with pytest.raises(verifier.VerificationError, match="manager source hash"):
        verifier.verify(artifact_path, PROFILE)


def test_receipt_writer_never_overwrites(tmp_path: Path) -> None:
    artifact_path = _artifact(tmp_path)
    receipt = tmp_path / "validation.json"
    verifier.write_receipt(artifact_path, PROFILE, receipt)

    with pytest.raises(verifier.VerificationError, match="new regular file"):
        verifier.write_receipt(artifact_path, PROFILE, receipt)


def test_receipt_writer_stays_with_the_derived_artifact(tmp_path: Path) -> None:
    artifact_path = _artifact(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    with pytest.raises(verifier.VerificationError, match="artifact directory"):
        verifier.write_receipt(
            artifact_path,
            PROFILE,
            elsewhere / "validation.json",
        )
