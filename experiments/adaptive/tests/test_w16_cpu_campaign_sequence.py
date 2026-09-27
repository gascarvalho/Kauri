"""Tests-first contracts for the frozen W16 24-cell sequence runner.

No test in this module launches Kauri, creates cgroups, or contacts INESC.
The fake subprocess seam proves only that the runner derives one fixed command
per authorized cell, preserves external records, and stops rather than retries.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
from typing import Any

import pytest


FORWARD = (
    "slow-roots:homogeneous", "fast-roots:homogeneous",
    "slow-roots:heterogeneous", "fast-roots:heterogeneous",
)
REVERSE = tuple(reversed(FORWARD))
ORDERS = ("forward", "reverse", "forward", "reverse", "forward", "reverse")
CAMPAIGN_ID = "w16-cpu-repeat-sequence-test"
REVISION = "a" * 40
PROFILE_SHA256 = "b" * 64
BINARIES = {name: hashlib.sha256(name.encode()).hexdigest()
            for name in ("app", "keygen", "tls_keygen", "native_digest")}
APPROVAL_REF = "test-approval:sequence-runner"


def _sequence_module():
    return importlib.import_module(
        "experiments.adaptive.kauri_experiment.w16_cpu_campaign_sequence"
    )


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")
        + b"\n"
    )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_json(path: Path, value: object) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _canonical(value)
    path.write_bytes(data)
    return data


def _freeze(tmp_path: Path) -> tuple[Path, dict[str, object], bytes]:
    repository_root = (tmp_path / "repo").resolve()
    timeout_path = repository_root / "bin" / "timeout"
    timeout_path.parent.mkdir(parents=True, exist_ok=True)
    timeout_path.write_bytes(b"synthetic-timeout\n")
    timeout_path.chmod(0o755)
    output_parent = repository_root / "results" / "w16-cpu-repeat-sequence-test"
    evidence_dir = repository_root / "build-adaptive" / "w16-cpu-repeat-sequence-test"
    freeze = {
        "schema_version": 1,
        "kind": "kauri-w16-cpu-repeat-freeze-v1",
        "campaign_id": CAMPAIGN_ID,
        "revision": REVISION,
        "repository_root": str(repository_root),
        "host": "proteina02",
        "booking_id": "1hfblbqhgpne9en0k05jaq83t0",
        "output_parent": str(output_parent),
        "evidence_dir": str(evidence_dir),
        "timeout_path": str(timeout_path),
        "timeout_sha256": _sha256(timeout_path.read_bytes()),
        "block_orders": list(ORDERS),
        "replica_count": 31,
        "quorum": 21,
        "fanout": 5,
        "tree_count": 21,
        "slow_replica_ids": [0, 1, 2, 3, 4, 5],
        "slow_quota_percent": 25,
        "other_quota_percent": 100,
        "complete_cycles": 5,
        "hard_timeout_s": 480,
        "external_timeout_s": 720,
        "automatic_retries": 0,
        "positive_blocks_required": 5,
        "positive_per_order_required": 2,
        "adjusted_gain_numerator": 11,
        "adjusted_gain_denominator": 10,
        "pilot_excluded": True,
    }
    path = tmp_path / "freeze.json"
    return path, freeze, _write_json(path, freeze)


def _patch_repository(
    monkeypatch: pytest.MonkeyPatch, sequence: object, tmp_path: Path,
) -> None:
    monkeypatch.setattr(sequence, "REPOSITORY", (tmp_path / "repo").resolve())


def _label_slug(label: str) -> str:
    return label.replace(":", "-")


def _authorization(
    *, freeze_sha256: str, output_root: Path, preflight_bytes: bytes,
    block_index: int, order: tuple[str, ...], cell_ordinal: int,
) -> dict[str, object]:
    arm, quota_mode = order[cell_ordinal - 1].split(":", 1)
    return {
        "schema_version": 2,
        "kind": "kauri-w16-static-e0-campaign-authorization-v2",
        "campaign_id": CAMPAIGN_ID,
        "block_index": block_index,
        "campaign_freeze_sha256": freeze_sha256,
        "block_id": f"{CAMPAIGN_ID}-block-{block_index:02d}",
        "block_order": list(order),
        "cell_ordinal": cell_ordinal,
        "revision": REVISION,
        "profile_sha256": PROFILE_SHA256,
        "arm": arm,
        "quota_mode": quota_mode,
        "preflight_sha256": _sha256(preflight_bytes),
        "binary_sha256": BINARIES,
        "output_root": str(output_root),
        "required_complete_cycles": 5,
        "hard_timeout_s": 480,
        "external_timeout_s": 720,
        "automatic_retries": 0,
        "claim_eligible": False,
        "figure_eligible": False,
        "approval_ref": APPROVAL_REF,
        "approved_at_utc": "2026-09-27T12:00:00Z",
    }


def _manifest(tmp_path: Path) -> tuple[Path, Path, dict[str, object], bytes, dict[str, object]]:
    freeze_path, freeze, freeze_bytes = _freeze(tmp_path)
    freeze_sha256 = _sha256(freeze_bytes)
    output_parent = Path(str(freeze["output_parent"]))
    evidence_dir = Path(str(freeze["evidence_dir"]))
    cells: list[dict[str, object]] = []
    for block_index, order_name in enumerate(ORDERS, 1):
        order = FORWARD if order_name == "forward" else REVERSE
        for cell_ordinal, label in enumerate(order, 1):
            ordinal = (block_index - 1) * 4 + cell_ordinal
            output_root = output_parent / f"block-{block_index:02d}" / _label_slug(label)
            input_dir = evidence_dir / "inputs" / f"block-{block_index:02d}" / f"cell-{cell_ordinal:02d}"
            preflight = {
                "schema_version": 1,
                "kind": "kauri-n31-static-e0-feasibility-preflight-v1",
                "verdict": "PREFLIGHT_OK_NO_EXECUTION", "revision": REVISION,
                "profile_sha256": PROFILE_SHA256, "arm": label.split(":", 1)[0],
                "binary_sha256": BINARIES,
            }
            preflight_path = input_dir / "preflight.json"
            preflight_bytes = _write_json(preflight_path, preflight)
            authorization_path = input_dir / "authorization.json"
            authorization_bytes = _write_json(
                authorization_path,
                _authorization(
                    freeze_sha256=freeze_sha256, output_root=output_root,
                    preflight_bytes=preflight_bytes, block_index=block_index,
                    order=order, cell_ordinal=cell_ordinal,
                ),
            )
            cells.append({
                "ordinal": ordinal,
                "block_index": block_index,
                "block_order": order_name,
                "cell_label": label,
                "block_cell_ordinal": cell_ordinal,
                "output_root": str(output_root),
                "preflight_path": str(preflight_path),
                "preflight_sha256": _sha256(preflight_bytes),
                "authorization_path": str(authorization_path),
                "authorization_sha256": _sha256(authorization_bytes),
                "hard_timeout_s": 480,
                "external_timeout_s": 720,
                "automatic_retries": 0,
            })
    manifest = {
        "schema_version": 1,
        "kind": "kauri-w16-cpu-repeat-sequence-manifest-v1",
        "campaign_id": CAMPAIGN_ID,
        "revision": REVISION,
        "campaign_freeze_sha256": freeze_sha256,
        "approval_ref": APPROVAL_REF,
        "output_parent": str(output_parent),
        "evidence_dir": str(evidence_dir),
        "cells": cells,
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_bytes = _write_json(manifest_path, manifest)
    return freeze_path, manifest_path, freeze, manifest_bytes, manifest


def test_validate_freeze_accepts_exact_contract_and_rejects_extra_or_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence = _sequence_module()
    _patch_repository(monkeypatch, sequence, tmp_path)
    _path, freeze, _bytes = _freeze(tmp_path)
    result = sequence.validate_freeze(freeze)
    assert result["verdict"] == "PASS", result
    assert result["claim_eligible"] is False

    malformed = dict(freeze); malformed["extra"] = True
    assert sequence.validate_freeze(malformed)["verdict"] != "PASS"
    drifted = dict(freeze); drifted["block_orders"] = list(reversed(ORDERS))
    assert sequence.validate_freeze(drifted)["verdict"] != "PASS"


def test_validate_manifest_reconstructs_all_24_and_rejects_path_hash_or_order_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence = _sequence_module()
    _patch_repository(monkeypatch, sequence, tmp_path)
    freeze_path, manifest_path, _freeze, _manifest_bytes, manifest = _manifest(tmp_path)
    freeze_bytes = freeze_path.read_bytes()
    result = sequence.validate_manifest(
        manifest, freeze_bytes=freeze_bytes, manifest_path=manifest_path,
        approval_ref=APPROVAL_REF,
    )
    assert result["verdict"] == "PASS", result
    assert result["cell_count"] == 24
    assert result["claim_eligible"] is False

    path_drift = json.loads(json.dumps(manifest))
    path_drift["cells"][0]["output_root"] = str(tmp_path / "other-output")
    assert sequence.validate_manifest(
        path_drift, freeze_bytes=freeze_bytes, manifest_path=manifest_path,
        approval_ref=APPROVAL_REF,
    )["verdict"] != "PASS"
    order_drift = json.loads(json.dumps(manifest))
    order_drift["cells"][4]["block_order"] = "forward"
    assert sequence.validate_manifest(
        order_drift, freeze_bytes=freeze_bytes, manifest_path=manifest_path,
        approval_ref=APPROVAL_REF,
    )["verdict"] != "PASS"
    changed_authorization = Path(manifest["cells"][0]["authorization_path"])
    changed_authorization.write_bytes(b"forged\n")
    assert sequence.validate_manifest(
        manifest, freeze_bytes=freeze_bytes, manifest_path=manifest_path,
        approval_ref=APPROVAL_REF,
    )["verdict"] != "PASS"


def test_execute_sequence_stops_after_first_nonzero_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence = _sequence_module()
    _patch_repository(monkeypatch, sequence, tmp_path)
    freeze_path, manifest_path, _freeze, manifest_bytes, _manifest_value = _manifest(tmp_path)
    calls: list[tuple[list[str], dict[str, object]]] = []
    validation_calls: list[Path] = []

    class Process:
        def __init__(self, returncode: int, pid: int) -> None:
            self.returncode = None
            self._planned_returncode = returncode
            self.pid = pid

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            assert timeout == 30
            self.returncode = self._planned_returncode
            return self.returncode

    def fake_popen(argv: list[str], **kwargs: Any) -> Process:
        ordinal = len(calls) + 1
        execution = Path(_manifest_value["evidence_dir"]) / "execution"
        assert (execution / "journal.jsonl").is_file()
        assert (execution / "snapshot" / "freeze.json").is_file()
        assert (execution / "snapshot" / "manifest.json").is_file()
        assert (execution / f"block-{(ordinal - 1) // 4 + 1:02d}"
                / f"cell-{(ordinal - 1) % 4 + 1:02d}" / "cell-intent.json").is_file()
        calls.append((list(argv), kwargs))
        return Process(1 if ordinal == 3 else 0, 1000 + ordinal)

    monkeypatch.setattr(sequence.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sequence, "_verify_live_preflight", lambda *_args: None)
    monkeypatch.setattr(
        sequence,
        "validate_w16_output_v5",
        lambda root: (validation_calls.append(Path(root)) or {"verdict": "PASS"}),
    )
    result = sequence.execute_sequence(
        manifest_path, manifest_sha256=_sha256(manifest_bytes),
        freeze_file=freeze_path, approval_ref=APPROVAL_REF,
    )

    assert result["verdict"] == "STOPPED", result
    assert result["attempted_cells"] == 3
    assert result["first_failure_ordinal"] == 3
    assert len(calls) == 3
    assert len(validation_calls) == 2
    assert all(kwargs["start_new_session"] is True for _argv, kwargs in calls)
    assert result["claim_eligible"] is False
    for record in result["cells"]:
        assert record["wrapper_exit_code"] in (0, 1)
        assert Path(record["stdout_path"]).is_relative_to(
            Path(_manifest_value["evidence_dir"])
        )
        assert Path(record["stderr_path"]).is_relative_to(
            Path(_manifest_value["evidence_dir"])
        )
        assert Path(record["validation_path"]).is_relative_to(
            Path(_manifest_value["evidence_dir"])
        )


def test_execute_sequence_stops_on_v5_failure_after_zero_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence = _sequence_module()
    _patch_repository(monkeypatch, sequence, tmp_path)
    freeze_path, manifest_path, _freeze, manifest_bytes, _manifest_value = _manifest(tmp_path)
    launches: list[int] = []
    validations: list[Path] = []

    class Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            assert timeout == 30
            self.returncode = 0
            return 0

    def fake_popen(_argv: list[str], **kwargs: Any) -> Process:
        assert kwargs["start_new_session"] is True
        launches.append(len(launches) + 1)
        return Process(2000 + len(launches))

    def fake_validation(root: Path) -> dict[str, object]:
        validations.append(Path(root))
        return {"verdict": "FAIL" if len(validations) == 2 else "PASS"}

    monkeypatch.setattr(sequence.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sequence, "_verify_live_preflight", lambda *_args: None)
    monkeypatch.setattr(sequence, "validate_w16_output_v5", fake_validation)
    result = sequence.execute_sequence(
        manifest_path, manifest_sha256=_sha256(manifest_bytes),
        freeze_file=freeze_path, approval_ref=APPROVAL_REF,
    )
    assert result["verdict"] == "STOPPED", result
    assert result["attempted_cells"] == 2
    assert result["first_failure_ordinal"] == 2
    assert launches == [1, 2]
    assert len(validations) == 2


def test_execute_sequence_never_returns_pass_when_terminal_record_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence = _sequence_module()
    _patch_repository(monkeypatch, sequence, tmp_path)
    freeze_path, manifest_path, _freeze, manifest_bytes, _manifest_value = _manifest(tmp_path)

    class Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            assert timeout == 30
            self.returncode = 0
            return 0

    monkeypatch.setattr(
        sequence.subprocess, "Popen",
        lambda _argv, **_kwargs: Process(3000),
    )
    monkeypatch.setattr(sequence, "_verify_live_preflight", lambda *_args: None)
    monkeypatch.setattr(sequence, "validate_w16_output_v5", lambda _root: {"verdict": "PASS"})
    monkeypatch.setattr(sequence, "validate_w16_campaign_block", lambda _roots: {"verdict": "PASS"})
    monkeypatch.setattr(sequence, "validate_w16_cpu_campaign", lambda _blocks: {"verdict": "PASS"})
    original_write = sequence._write_new_json

    def fail_terminal(path: Path, value: object) -> None:
        if path.name == "sequence-result.json":
            raise OSError("synthetic terminal record failure")
        original_write(path, value)

    monkeypatch.setattr(sequence, "_write_new_json", fail_terminal)
    result = sequence.execute_sequence(
        manifest_path, manifest_sha256=_sha256(manifest_bytes),
        freeze_file=freeze_path, approval_ref=APPROVAL_REF,
    )
    assert result["verdict"] != "PASS", result
    assert result["claim_eligible"] is False


def test_interrupted_live_child_preserves_intent_and_append_only_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence = _sequence_module()
    _patch_repository(monkeypatch, sequence, tmp_path)
    freeze_path, manifest_path, _freeze, manifest_bytes, manifest = _manifest(tmp_path)

    class LiveProcess:
        pid = 4242
        returncode = None

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            assert timeout == 30
            raise KeyboardInterrupt()

    monkeypatch.setattr(sequence, "_verify_live_preflight", lambda *_args: None)
    monkeypatch.setattr(sequence.subprocess, "Popen", lambda *_args, **_kwargs: LiveProcess())
    result = sequence.execute_sequence(
        manifest_path, manifest_sha256=_sha256(manifest_bytes),
        freeze_file=freeze_path, approval_ref=APPROVAL_REF,
    )

    execution = Path(manifest["evidence_dir"]) / "execution"
    assert result["verdict"] == "STOPPED", result
    assert result["attempted_cells"] == 1
    assert result["live_child_pid"] == 4242
    assert (execution / "block-01" / "cell-01" / "cell-intent.json").is_file()
    events = [json.loads(line)["event"] for line in (execution / "journal.jsonl").read_text().splitlines()]
    assert events[:3] == ["sequence_start", "cell_intent", "cell_pid"]
    assert events[-1] == "sequence_abort"
    assert (execution / "sequence-abort.json").is_file()


def test_prepare_sequence_writes_24_authorized_inputs_without_launching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence = _sequence_module()
    _patch_repository(monkeypatch, sequence, tmp_path)
    freeze_path, freeze, _freeze_bytes = _freeze(tmp_path)
    preflight_calls: list[str] = []
    launch_calls: list[object] = []

    def fake_preflight(**kwargs: Any) -> dict[str, object]:
        arm = str(kwargs["arm"])
        preflight_calls.append(arm)
        return {
            "schema_version": 1,
            "kind": "kauri-n31-static-e0-feasibility-preflight-v1",
            "verdict": "PREFLIGHT_OK_NO_EXECUTION",
            "revision": REVISION,
            "profile_sha256": PROFILE_SHA256,
            "arm": arm,
            "binary_sha256": BINARIES,
        }

    def forbidden_launch(*_args: Any, **_kwargs: Any) -> None:
        launch_calls.append((_args, _kwargs))
        raise AssertionError("prepare_sequence must not launch a subprocess")

    monkeypatch.setattr(sequence.feasibility, "preflight", fake_preflight)
    monkeypatch.setattr(sequence.subprocess, "run", forbidden_launch)
    result = sequence.prepare_sequence(
        freeze_path, approval_ref=APPROVAL_REF,
        approved_at_utc="2026-09-27T12:00:00Z",
    )

    assert result["verdict"] == "PREPARED_NO_EXECUTION", result
    assert result["claim_eligible"] is False
    assert len(preflight_calls) == 24
    assert launch_calls == []
    manifest_path = Path(result["manifest_path"])
    assert manifest_path.is_file()
    assert result["manifest_sha256"] == _sha256(manifest_path.read_bytes())
    manifest = json.loads(manifest_path.read_text())
    assert [cell["cell_label"] for cell in manifest["cells"]] == list(FORWARD + REVERSE + FORWARD + REVERSE + FORWARD + REVERSE)
    validation = sequence.validate_manifest(
        manifest, freeze_bytes=freeze_path.read_bytes(), manifest_path=manifest_path,
        approval_ref=APPROVAL_REF,
    )
    assert validation["verdict"] == "PASS", validation
    assert not Path(str(freeze["output_parent"])).exists()


@pytest.mark.parametrize("existing_key", ("output_parent", "evidence_dir"))
def test_prepare_sequence_refuses_existing_campaign_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing_key: str,
) -> None:
    sequence = _sequence_module()
    _patch_repository(monkeypatch, sequence, tmp_path)
    freeze_path, freeze, _freeze_bytes = _freeze(tmp_path)
    Path(str(freeze[existing_key])).mkdir(parents=True)
    _write_json(freeze_path, freeze)
    monkeypatch.setattr(
        sequence.feasibility, "preflight",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must fail before preflight")),
    )
    result = sequence.prepare_sequence(
        freeze_path, approval_ref=APPROVAL_REF,
        approved_at_utc="2026-09-27T12:00:00Z",
    )
    assert result["verdict"] != "PREPARED_NO_EXECUTION"
    assert result["claim_eligible"] is False
