from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _write_manifest(path: Path) -> None:
    path.write_bytes(json.dumps({"schema_version": 1, "pair": "frozen"}, sort_keys=True,
                                separators=(",", ":")).encode("ascii") + b"\n")


def _argv(manifest: Path) -> list[str]:
    return [
        "--pair-manifest", str(manifest),
        "--sham-root", "/tmp/sham", "--treatment-root", "/tmp/treatment",
        "--sham-raw-validation", "/tmp/sham-raw.json", "--treatment-raw-validation", "/tmp/treatment-raw.json",
        "--sham-authority", "/tmp/sham-authority.json", "--treatment-authority", "/tmp/treatment-authority.json",
        "--stage-a-verifier-binary", "/tmp/stage-a", "--stage-b-verifier-binary", "/tmp/stage-b",
    ]


def test_cli_revalidates_existing_pair_and_keeps_descriptive_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    module = importlib.import_module("run_operator_capacity_v3_pair")
    manifest = tmp_path / "pair.json"; _write_manifest(manifest)
    captured: dict[str, object] = {}
    def make_revalidator(**kwargs: object) -> object:
        captured["binaries"] = kwargs
        return "real-revalidator"
    def evaluate(value: object, **kwargs: object) -> dict[str, object]:
        captured["manifest"] = value; captured["revalidate"] = kwargs["revalidate"]
        return {"verdict": "PAIR_COMPLETE_DESCRIPTIVE_ONLY", "claim_eligible": False,
                "figure_eligible": False, "campaign_eligible": False}
    monkeypatch.setattr(module.bridge, "make_pair_revalidator", make_revalidator)
    monkeypatch.setattr(module.evaluator, "evaluate_matched_pair", evaluate)
    assert module.main(_argv(manifest)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["verdict"] == "PAIR_COMPLETE_DESCRIPTIVE_ONLY"
    assert result["claim_eligible"] is False
    assert captured["revalidate"] == "real-revalidator"
    assert captured["binaries"] == {
        "stage_a_verifier_binary": Path("/tmp/stage-a"),
        "stage_b_verifier_binary": Path("/tmp/stage-b"),
    }


def test_cli_returns_nonzero_and_no_claim_for_raw_or_pair_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    module = importlib.import_module("run_operator_capacity_v3_pair")
    manifest = tmp_path / "pair.json"; _write_manifest(manifest)
    monkeypatch.setattr(module.bridge, "make_pair_revalidator", lambda **_kwargs: object())
    monkeypatch.setattr(module.evaluator, "evaluate_matched_pair",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(module.evaluator.PairEvaluationError("raw recheck incomplete")))
    assert module.main(_argv(manifest)) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["verdict"] == "PAIR_INCOMPLETE_NO_CLAIM"
    assert result["claim_eligible"] is False
    assert result["figure_eligible"] is False


def test_cli_rejects_noncanonical_frozen_manifest_before_revalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    module = importlib.import_module("run_operator_capacity_v3_pair")
    manifest = tmp_path / "pair.json"; manifest.write_text('{ "pair": "mutable" }\n', encoding="ascii")
    monkeypatch.setattr(module.bridge, "make_pair_revalidator",
                        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not revalidate")))
    assert module.main(_argv(manifest)) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["verdict"] == "PAIR_INCOMPLETE_NO_CLAIM"
    assert "canonical" in result["detail"]
