"""Regression tests for the accepted-artifact CERT13 placement audit."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from experiments.adaptive.kauri_experiment import cert13_placement_contrast as audit
from experiments.adaptive import validate_cert13_placement_contrast as verifier


REPOSITORY = Path(__file__).resolve().parents[3]
CAMPAIGN = REPOSITORY / "results" / audit.CAMPAIGN_ID


def _bind_test_output(report: dict[str, object], path: Path) -> None:
    report["invocation"] = {
        "argv": [audit.AUDIT_COMMAND, "--campaign-root", str(CAMPAIGN), "--output-root", str(path.parent)],
        "resolved_campaign_root": str(CAMPAIGN.resolve()),
        "resolved_output_root": str(path.parent.resolve()),
    }


def test_audit_reconstructs_the_fixed_five_arm_denominator() -> None:
    report = audit.build_report(CAMPAIGN, command="test")

    assert report["verdict"] == "PASS"
    assert report["fixed_denominator"] == {
        "adaptive_arm_count": 5,
        "slot_ids": list(audit.ADAPTIVE_SLOTS),
        "tree_root_position_count": 21,
        "consensus_quorum_Q": 21,
    }
    assert [arm["slot_id"] for arm in report["arms"]] == list(audit.ADAPTIVE_SLOTS)
    assert all(arm["epoch2_exact_eligible_ranking_binding"] is True for arm in report["arms"])
    assert all(arm["score_margin"]["state"] == "unknown" for arm in report["arms"])


def test_verifier_recomputes_source_bound_placement_values(tmp_path: Path) -> None:
    report = audit.build_report(CAMPAIGN, command="test")
    path = tmp_path / audit.REPORT_NAME
    _bind_test_output(report, path)
    path.write_text(audit.canonical_json(report), encoding="ascii")

    assert verifier.verify(CAMPAIGN, path)["verdict"] == "PASS"


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("changed_root_position_count", 0),
        ("epoch2_exact_eligible_ranking_binding", False),
    ],
)
def test_verifier_rejects_tampered_report_counts(
    tmp_path: Path, field: str, replacement: object
) -> None:
    report = copy.deepcopy(audit.build_report(CAMPAIGN, command="test"))
    report["arms"][0][field] = replacement
    path = tmp_path / audit.REPORT_NAME
    _bind_test_output(report, path)
    path.write_text(audit.canonical_json(report), encoding="ascii")

    with pytest.raises(verifier.VerificationError, match="independent exact reconstruction"):
        verifier.verify(CAMPAIGN, path)


def test_verifier_rejects_reordered_root_positions(tmp_path: Path) -> None:
    report = copy.deepcopy(audit.build_report(CAMPAIGN, command="test"))
    roots = report["arms"][0]["epoch2_roots"]
    roots[0], roots[1] = roots[1], roots[0]
    path = tmp_path / audit.REPORT_NAME
    _bind_test_output(report, path)
    path.write_text(audit.canonical_json(report), encoding="ascii")

    with pytest.raises(verifier.VerificationError, match="independent exact reconstruction"):
        verifier.verify(CAMPAIGN, path)


def test_verifier_rejects_tampered_aggregate_and_source_bindings(tmp_path: Path) -> None:
    report = copy.deepcopy(audit.build_report(CAMPAIGN, command="test"))
    report["summary"]["changed_root_position_counts"] = [0] * 5
    path = tmp_path / audit.REPORT_NAME
    _bind_test_output(report, path)
    path.write_text(audit.canonical_json(report), encoding="ascii")
    with pytest.raises(verifier.VerificationError, match="independent exact reconstruction"):
        verifier.verify(CAMPAIGN, path)

    report = copy.deepcopy(audit.build_report(CAMPAIGN, command="test"))
    report["source"]["campaign_revision"] = "0" * 40
    _bind_test_output(report, path)
    path.write_text(audit.canonical_json(report), encoding="ascii")
    with pytest.raises(verifier.VerificationError, match="independent exact reconstruction"):
        verifier.verify(CAMPAIGN, path)


@pytest.mark.parametrize(
    "argv",
    [
        ["forged", "--campaign-root", str(CAMPAIGN), "--output-root", "OUTPUT"],
        [audit.AUDIT_COMMAND, "--output-root", "OUTPUT", "--campaign-root", str(CAMPAIGN)],
        [audit.AUDIT_COMMAND, "--campaign-root", str(CAMPAIGN), "--output-root", "OUTPUT", "--unknown", "x"],
        [audit.AUDIT_COMMAND, "--campaign-root", str(CAMPAIGN), "--campaign-root", str(CAMPAIGN), "--output-root", "OUTPUT"],
    ],
)
def test_verifier_rejects_noncanonical_or_ambiguous_invocation(
    tmp_path: Path, argv: list[str]
) -> None:
    report = audit.build_report(CAMPAIGN, command="test")
    path = tmp_path / audit.REPORT_NAME
    _bind_test_output(report, path)
    report["invocation"]["argv"] = [value if value != "OUTPUT" else str(path.parent) for value in argv]  # type: ignore[index]
    path.write_text(audit.canonical_json(report), encoding="ascii")

    with pytest.raises(verifier.VerificationError, match="exact audit command"):
        verifier.verify(CAMPAIGN, path)


@pytest.mark.parametrize("field", ["resolved_campaign_root", "resolved_output_root"])
def test_verifier_rejects_forged_invocation_paths(tmp_path: Path, field: str) -> None:
    report = audit.build_report(CAMPAIGN, command="test")
    path = tmp_path / audit.REPORT_NAME
    _bind_test_output(report, path)
    report["invocation"][field] = str(tmp_path / "forged")  # type: ignore[index]
    path.write_text(audit.canonical_json(report), encoding="ascii")

    with pytest.raises(verifier.VerificationError, match="invocation .* path"):
        verifier.verify(CAMPAIGN, path)


def test_verifier_rejects_argv_path_that_disagrees_with_report_binding(tmp_path: Path) -> None:
    report = audit.build_report(CAMPAIGN, command="test")
    path = tmp_path / audit.REPORT_NAME
    _bind_test_output(report, path)
    report["invocation"]["argv"][2] = str(tmp_path / "forged-campaign")  # type: ignore[index]
    path.write_text(audit.canonical_json(report), encoding="ascii")

    with pytest.raises(verifier.VerificationError, match="--campaign-root differs"):
        verifier.verify(CAMPAIGN, path)


def test_verifier_rejects_an_extra_invocation_claim_field(tmp_path: Path) -> None:
    report = audit.build_report(CAMPAIGN, command="test")
    path = tmp_path / audit.REPORT_NAME
    _bind_test_output(report, path)
    report["invocation"]["claim_eligible"] = True  # type: ignore[index]
    path.write_text(audit.canonical_json(report), encoding="ascii")

    with pytest.raises(verifier.VerificationError, match="unexpected schema"):
        verifier.verify(CAMPAIGN, path)


def test_verifier_rejects_noncanonical_report_json(tmp_path: Path) -> None:
    report = audit.build_report(CAMPAIGN, command="test")
    path = tmp_path / audit.REPORT_NAME
    _bind_test_output(report, path)
    path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(verifier.VerificationError, match="not canonical JSON"):
        verifier.verify(CAMPAIGN, path)


def test_writer_binds_canonical_paths_across_working_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "fresh-audit"
    relative_campaign = Path("results") / audit.CAMPAIGN_ID
    command = "\x00".join(
        [audit.AUDIT_COMMAND, "--campaign-root", str(relative_campaign), "--output-root", str(output)]
    )
    report_path = audit.write_report(relative_campaign, output, command=command)
    report = json.loads(report_path.read_text(encoding="ascii"))
    assert report["invocation"]["argv"] == [
        audit.AUDIT_COMMAND,
        "--campaign-root",
        str(CAMPAIGN.resolve()),
        "--output-root",
        str(output.resolve()),
    ]

    monkeypatch.chdir(tmp_path)
    assert verifier.verify(CAMPAIGN, report_path)["verdict"] == "PASS"


def test_audit_rejects_a_self_consistent_but_unaccepted_campaign_seal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        audit,
        "verify_evidence_seal",
        lambda _root: SimpleNamespace(tree_sha256="0" * 64, seal_sha256="1" * 64),
    )

    with pytest.raises(audit.PlacementContrastError, match="differs from accepted evidence"):
        audit.build_report(CAMPAIGN, command="test")


@pytest.mark.parametrize(
    ("location", "value"),
    [
        (("schema_version",), 99),
        (("audit_id",), "forged-audit"),
        (("generator_id",), "forged command"),
        (("arms", 0, "limitations"), []),
        (("arms", 0, "score_margin"), {"state": "measured", "value": 1.2}),
    ],
)
def test_verifier_rejects_any_unreconstructed_report_content(
    tmp_path: Path, location: tuple[object, ...], value: object
) -> None:
    report = copy.deepcopy(audit.build_report(CAMPAIGN, command="test"))
    target: object = report
    for key in location[:-1]:
        target = target[key]  # type: ignore[index]
    target[location[-1]] = value  # type: ignore[index]
    path = tmp_path / audit.REPORT_NAME
    _bind_test_output(report, path)
    path.write_text(audit.canonical_json(report), encoding="ascii")

    with pytest.raises(verifier.VerificationError, match="independent exact reconstruction"):
        verifier.verify(CAMPAIGN, path)


def test_writer_refuses_an_output_descendant_of_the_sealed_campaign() -> None:
    with pytest.raises(audit.PlacementContrastError, match="outside the sealed campaign"):
        audit.write_report(CAMPAIGN, CAMPAIGN / "placement-audit", command="test")


def test_writer_persists_a_first_failure_abort_without_aggregate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        audit,
        "verify_evidence_seal",
        lambda _root: SimpleNamespace(
            tree_sha256=audit.EXPECTED_CAMPAIGN_TREE_SHA256,
            seal_sha256=audit.EXPECTED_CAMPAIGN_SEAL_SHA256,
        ),
    )
    monkeypatch.setattr(
        audit,
        "_arm_record",
        lambda _root, slot, _plan: (_ for _ in ()).throw(
            audit.PlacementContrastError(f"{slot['slot_id']} forced invalid fixture")
        ),
    )
    output = tmp_path / "fresh-audit"
    with pytest.raises(audit.PlacementContrastAbort, match="forced invalid fixture") as raised:
        audit.write_report(CAMPAIGN, output, command="exact fixture command")

    assert raised.value.path == output / audit.ABORT_NAME
    abort = json.loads(raised.value.path.read_text(encoding="ascii"))
    assert abort["verdict"] == "INVALID"
    assert abort["slot_id"] == "slot-02"
    assert abort["command"] == "exact fixture command"
    assert abort["aggregate_conclusion"] is None
    assert "arms" not in abort and "summary" not in abort


def test_verifier_receipt_refuses_a_sealed_campaign_descendant() -> None:
    with pytest.raises(verifier.VerificationError, match="outside the sealed campaign"):
        verifier.write_receipt(CAMPAIGN, CAMPAIGN / audit.REPORT_NAME, CAMPAIGN / "new-receipt.json")


def test_writer_persists_abort_for_a_missing_campaign_path(tmp_path: Path) -> None:
    output = tmp_path / "fresh-audit"
    with pytest.raises(audit.PlacementContrastAbort, match="campaign seal is invalid") as raised:
        audit.write_report(tmp_path / audit.CAMPAIGN_ID, output, command="missing fixture command")

    abort = json.loads(raised.value.path.read_text(encoding="ascii"))
    assert abort["verdict"] == "INVALID"
    assert abort["error_reason"] == "campaign seal is invalid"
    assert abort["aggregate_conclusion"] is None
