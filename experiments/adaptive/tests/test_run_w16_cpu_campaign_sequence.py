"""CLI contracts for the W16 campaign sequence runner (no live execution)."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys

import pytest


def _cli_module():
    adaptive_root = str(Path(__file__).resolve().parents[1])
    if adaptive_root not in sys.path:
        sys.path.insert(0, adaptive_root)
    return importlib.import_module("experiments.adaptive.run_w16_cpu_campaign_sequence")


def test_prepare_cli_forwards_exact_arguments_and_maps_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    cli = _cli_module()
    freeze = tmp_path / "freeze.json"
    calls: list[tuple[Path, str, str]] = []
    monkeypatch.setattr(
        cli, "prepare_sequence",
        lambda path, *, approval_ref, approved_at_utc: (
            calls.append((path, approval_ref, approved_at_utc))
            or {"verdict": "PREPARED_NO_EXECUTION", "claim_eligible": False}
        ),
    )
    monkeypatch.setattr(
        sys, "argv", [
            "run_w16_cpu_campaign_sequence.py", "prepare", "--freeze-file", str(freeze),
            "--approval-ref", "test-approval", "--approved-at-utc", "2026-09-27T12:00:00Z",
        ],
    )

    assert cli.main() == 0
    assert calls == [(freeze, "test-approval", "2026-09-27T12:00:00Z")]
    assert '"verdict":"PREPARED_NO_EXECUTION"' in capsys.readouterr().out


def test_run_cli_forwards_exact_approval_and_maps_only_pass_to_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    cli = _cli_module()
    freeze = tmp_path / "freeze.json"; manifest = tmp_path / "manifest.json"
    calls: list[tuple[Path, str, Path, str]] = []
    monkeypatch.setattr(
        cli, "execute_sequence",
        lambda path, *, manifest_sha256, freeze_file, approval_ref: (
            calls.append((path, manifest_sha256, freeze_file, approval_ref))
            or {"verdict": "PASS", "claim_eligible": False}
        ),
    )
    monkeypatch.setattr(
        sys, "argv", [
            "run_w16_cpu_campaign_sequence.py", "run", "--freeze-file", str(freeze),
            "--manifest", str(manifest), "--manifest-sha256", "a" * 64,
            "--approval-ref", "test-approval",
        ],
    )

    assert cli.main() == 0
    assert calls == [(manifest, "a" * 64, freeze, "test-approval")]
    assert '"verdict":"PASS"' in capsys.readouterr().out

    monkeypatch.setattr(cli, "execute_sequence", lambda *_args, **_kwargs: {"verdict": "STOPPED"})
    assert cli.main() == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["run_w16_cpu_campaign_sequence.py", "prepare", "--freeze-file", "/tmp/freeze"],
        ["run_w16_cpu_campaign_sequence.py", "run", "--freeze-file", "/tmp/freeze",
         "--manifest", "/tmp/manifest", "--approval-ref", "test"],
    ],
)
def test_cli_requires_full_prepare_or_run_authorization_arguments(
    monkeypatch: pytest.MonkeyPatch, argv: list[str],
) -> None:
    cli = _cli_module()
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as raised:
        cli.main()
    assert raised.value.code == 2
