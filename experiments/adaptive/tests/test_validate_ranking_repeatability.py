"""Independent-verifier regression and mutation tests for the W13 audit."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

from experiments.adaptive import ranking_repeatability as producer
from experiments.adaptive import validate_ranking_repeatability as verifier


REPOSITORY = Path(__file__).resolve().parents[3]
WORKSPACE = REPOSITORY.parent
CAMPAIGN = REPOSITORY / "results" / producer.CAMPAIGN_ID
VALIDATION = (
    REPOSITORY
    / "results"
    / ".cert13-n31-campaign-v13-7adabc83-r50-ba797cca2eca5d82-preflight"
    / "campaign-validation.json"
)
PLACEMENT = (
    WORKSPACE
    / "docs"
    / "context"
    / "thesis"
    / "research"
    / "evidence"
    / "cert13-placement-contrast-e7ab5506"
    / "placement-contrast.json"
)
REAL_COMMITTED_PROVENANCE = verifier._committed_source_provenance


@pytest.fixture(autouse=True)
def committed_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        verifier,
        "_committed_source_provenance",
        lambda path, _label: (
            "a" * 40,
            hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        ),
    )


@pytest.fixture(scope="session")
def accepted_artifact(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, object]]:
    output = tmp_path_factory.mktemp("w13-independent") / "ranking-repeatability.json"
    provenance = {
        "analysis_revision": "a" * 40,
        "generator_source_sha256": hashlib.sha256(
            Path(producer.__file__).read_bytes()
        ).hexdigest(),
    }
    original = producer._committed_source_provenance
    producer._committed_source_provenance = lambda: provenance
    try:
        report = producer.build_report(CAMPAIGN, VALIDATION, PLACEMENT, output)
    finally:
        producer._committed_source_provenance = original
    output.write_text(producer.canonical_json(report), encoding="ascii", newline="")
    return output, report


def _write_bound_report(path: Path, report: dict[str, object]) -> None:
    invocation = report["invocation"]
    assert isinstance(invocation, dict)
    invocation["argv"][-1] = str(path.resolve())  # type: ignore[index]
    invocation["resolved_output"] = str(path.resolve())
    path.write_text(verifier.canonical_json(report), encoding="ascii", newline="")


def test_independent_verifier_reconstructs_raw_cert13_and_writes_receipt(
    accepted_artifact: tuple[Path, dict[str, object]],
    tmp_path: Path,
) -> None:
    artifact, report = accepted_artifact
    output = tmp_path / "independent-receipt.json"
    source = report["source"]
    assert isinstance(source, dict)
    analysis_revision = source["analysis_revision"]

    receipt = verifier.write_receipt(
        artifact,
        VALIDATION,
        PLACEMENT,
        CAMPAIGN,
        output,
    )

    assert receipt["verifier_id"] == verifier.VERIFIER_ID
    assert receipt["analysis_revision"] == analysis_revision
    assert receipt["verifier_source_sha256"] == hashlib.sha256(
        Path(verifier.__file__).read_bytes()
    ).hexdigest()
    assert receipt["verdict"] == {
        "schema_version": 1,
        "verdict": "PASS",
        "analysis_id": producer.ANALYSIS_ID,
        "pair_count": 5,
        "pairwise_comparison_count": 10,
        "claim_eligible": False,
    }
    assert json.loads(output.read_text(encoding="ascii")) == receipt


def test_accepted_rj_join_is_exact_and_descriptive_only(
    accepted_artifact: tuple[Path, dict[str, object]]
) -> None:
    _artifact, report = accepted_artifact
    accepted = json.loads(VALIDATION.read_bytes())
    ratios = {row["pair_id"]: row["paired_ratio_ppm"] for row in accepted["pairs"]}

    assert [row["throughput"]["paired_ratio_ppm"] for row in report["pairs"]] == [
        ratios[f"pair-{index:02d}"] for index in range(1, 6)
    ]
    assert all(
        row["throughput"]["adjusted_effect_ppm"]
        == row["throughput"]["paired_ratio_ppm"] - 1_000_000
        for row in report["pairs"]
    )
    assert report["claim_boundary"] == verifier.CLAIM_BOUNDARY


def test_pairwise_metrics_have_known_ordering_extremes() -> None:
    ascending = {
        "pair_id": "left",
        "raw_eligible_ranking": list(range(21)),
    }
    identical = {
        "pair_id": "identical",
        "raw_eligible_ranking": list(range(21)),
    }
    reversed_order = {
        "pair_id": "reversed",
        "raw_eligible_ranking": list(reversed(range(21))),
    }

    same = verifier._pairwise(ascending, identical)
    reverse = verifier._pairwise(ascending, reversed_order)

    assert same["jaccard"] == {"numerator": 21, "denominator": 21, "decimal": 1.0}
    assert same["kendall_common"] == {
        "common_item_count": 21,
        "comparable_pair_count": 210,
        "concordant_pair_count": 210,
        "discordant_pair_count": 0,
        "numerator": 210,
        "denominator": 210,
        "tau": 1.0,
    }
    assert same["exact_position_match_count"] == 21
    assert reverse["kendall_common"]["tau"] == -1.0  # type: ignore[index]
    assert reverse["exact_position_match_count"] == 1


def test_committed_source_provenance_requires_exact_head_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = Path(verifier.__file__).resolve()
    source = source_path.read_bytes()
    revision = "b" * 40

    def exact_git(*arguments: str) -> bytes:
        if arguments == ("rev-parse", "HEAD"):
            return (revision + "\n").encode("ascii")
        assert arguments == (
            "show",
            f"{revision}:experiments/adaptive/validate_ranking_repeatability.py",
        )
        return source

    monkeypatch.setattr(verifier, "_git_bytes", exact_git)
    assert REAL_COMMITTED_PROVENANCE(source_path, "verifier") == (
        revision,
        hashlib.sha256(source).hexdigest(),
    )

    monkeypatch.setattr(
        verifier,
        "_git_bytes",
        lambda *arguments: (revision + "\n").encode("ascii")
        if arguments == ("rev-parse", "HEAD")
        else b"forged",
    )
    with pytest.raises(verifier.VerificationError, match="differs from committed"):
        REAL_COMMITTED_PROVENANCE(source_path, "verifier")


Mutation = Callable[[dict[str, object]], None]


def _swap_raw_ranking(report: dict[str, object]) -> None:
    ranking = report["pairs"][0]["raw_eligible_ranking"]  # type: ignore[index]
    ranking[0], ranking[1] = ranking[1], ranking[0]


def _forge_pair_provenance(report: dict[str, object]) -> None:
    report["pairs"][0]["adaptive_slot_id"] = "slot-10"  # type: ignore[index]


def _forge_pairwise_metric(report: dict[str, object]) -> None:
    report["pairwise_comparisons"][0]["kendall_common"]["tau"] = 1.0  # type: ignore[index]


def _forge_aggregate(report: dict[str, object]) -> None:
    report["aggregate"]["selection_frequency"][0]["count"] = 0  # type: ignore[index]


def _forge_rj(report: dict[str, object]) -> None:
    report["pairs"][0]["throughput"]["paired_ratio_ppm"] += 1  # type: ignore[index,operator]


def _forge_source_hash(report: dict[str, object]) -> None:
    report["source"]["campaign_validation"]["raw_sha256"] = "0" * 64  # type: ignore[index]


@pytest.mark.parametrize(
    "mutation",
    [
        _swap_raw_ranking,
        _forge_pair_provenance,
        _forge_pairwise_metric,
        _forge_aggregate,
        _forge_rj,
        _forge_source_hash,
    ],
    ids=[
        "raw-ranking",
        "pair-provenance",
        "pairwise-kendall",
        "aggregate-frequency",
        "accepted-rj",
        "source-hash",
    ],
)
def test_verifier_rejects_any_mutated_report_field(
    accepted_artifact: tuple[Path, dict[str, object]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: Mutation,
) -> None:
    _artifact, accepted = accepted_artifact
    expected = copy.deepcopy(accepted)
    path = tmp_path / "ranking-repeatability.json"
    _write_bound_report(path, expected)
    mutated = copy.deepcopy(expected)
    mutation(mutated)
    path.write_text(verifier.canonical_json(mutated), encoding="ascii", newline="")
    monkeypatch.setattr(
        verifier,
        "_reconstruct_expected",
        lambda *_args, **_kwargs: expected,
    )

    with pytest.raises(verifier.VerificationError, match="independent exact reconstruction"):
        verifier.verify(path, VALIDATION, PLACEMENT, CAMPAIGN)


def test_verifier_rejects_noncanonical_artifact_before_reconstruction(
    accepted_artifact: tuple[Path, dict[str, object]], tmp_path: Path
) -> None:
    _artifact, report = accepted_artifact
    path = tmp_path / "ranking-repeatability.json"
    _write_bound_report(path, report := copy.deepcopy(report))
    path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(verifier.VerificationError, match="not canonical JSON"):
        verifier.verify(path, VALIDATION, PLACEMENT, CAMPAIGN)


def test_raw_event_ranking_cannot_diverge_from_w9_and_signed_e2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    placement = json.loads(PLACEMENT.read_bytes())
    validation = json.loads(VALIDATION.read_bytes())
    arm = next(row for row in placement["arms"] if row["pair_id"] == "pair-01")
    outcome = next(row for row in validation["pairs"] if row["pair_id"] == "pair-01")
    original = verifier._raw_optimization_event

    monkeypatch.setattr(
        verifier,
        "verify_evidence_seal",
        lambda _path: SimpleNamespace(
            tree_sha256=arm["source"]["child_tree_sha256"],
            seal_sha256=arm["source"]["child_seal_sha256"],
        ),
    )

    def mutated_event(path: Path) -> tuple[dict[str, object], str]:
        event, digest = original(path)
        changed = copy.deepcopy(dict(event))
        payload = changed["payload"]
        assert isinstance(payload, dict)
        ranking = payload["eligible_ranking"]
        ranking[0], ranking[1] = ranking[1], ranking[0]
        return changed, digest

    monkeypatch.setattr(verifier, "_raw_optimization_event", mutated_event)
    with pytest.raises(verifier.VerificationError, match="does not bind W9 and E2"):
        verifier._expected_pair(CAMPAIGN, 1, "pair-01", arm, outcome)


@pytest.mark.parametrize(
    "rows",
    [
        [{"pair_id": pair_id} for pair_id in verifier.PAIR_IDS[:-1]],
        [{"pair_id": "pair-01"}] * 5,
        [{"pair_id": pair_id} for pair_id in (*verifier.PAIR_IDS[:-1], "pair-99")],
    ],
    ids=["missing", "duplicate", "substituted"],
)
def test_exact_five_pair_provenance_is_fail_closed(rows: list[dict[str, str]]) -> None:
    with pytest.raises(verifier.VerificationError, match="exact five|duplicate"):
        verifier._index_exact_rows(rows, verifier.PAIR_IDS, "mutated input")


def test_verifier_rejects_unfrozen_expected_input_hash(
    accepted_artifact: tuple[Path, dict[str, object]]
) -> None:
    artifact, _report = accepted_artifact
    with pytest.raises(verifier.VerificationError, match="differs from frozen input"):
        verifier.verify(
            artifact,
            VALIDATION,
            PLACEMENT,
            CAMPAIGN,
            expected_campaign_validation_sha256="0" * 64,
        )
