"""Contracts for the isolated W16 v7 sequence; no live cell is launched."""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path

import pytest


def _module():
    return importlib.import_module(
        "experiments.adaptive.kauri_experiment.w16_cpu_campaign_sequence_v7"
    )


def _write(path: Path, value: object) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    path.write_bytes(payload)
    return payload


def _freeze(tmp_path: Path, module: object) -> tuple[Path, dict[str, object]]:
    repository = (tmp_path / "repo").resolve()
    timeout = repository / "bin" / "timeout"
    timeout.parent.mkdir(parents=True)
    timeout.write_bytes(b"synthetic-timeout\n")
    timeout.chmod(0o755)
    freeze = {
        "schema_version": 2, "kind": "kauri-w16-cpu-repeat-freeze-v2",
        "campaign_id": "w16-cpu-repeat-v7-test", "revision": "a" * 40,
        "host": "proteina02", "booking_id": "1hfblbqhgpne9en0k05jaq83t0",
        "repository_root": str(repository),
        "output_parent": str(repository / "results" / "w16-cpu-repeat-v7-test"),
        "evidence_dir": str(repository / "build-adaptive" / "w16-cpu-repeat-v7-test"),
        "timeout_path": str(timeout), "timeout_sha256": hashlib.sha256(timeout.read_bytes()).hexdigest(),
        "block_orders": ["forward", "reverse", "forward", "reverse", "forward", "reverse"],
        "replica_count": 31, "quorum": 21, "fanout": 5, "tree_count": 21,
        "slow_replica_ids": list(range(6)), "slow_quota_percent": 25,
        "other_quota_percent": 100, "complete_cycles": 5, "hard_timeout_s": 480,
        "external_timeout_s": 720, "automatic_retries": 0,
        "positive_blocks_required": 5, "positive_per_order_required": 2,
        "adjusted_gain_numerator": 11, "adjusted_gain_denominator": 10,
        "pilot_excluded": True, "authorization_schema_version": 3,
        "authorization_kind": "kauri-w16-static-e0-campaign-authorization-v3",
        "executor_receipt_schema": "kauri-n31-static-e0-local-executor-v3",
        "cell_validator_version": 7, "process_cleanup_required": True,
    }
    path = tmp_path / "freeze-v2.json"
    _write(path, freeze)
    return path, freeze


def _preflight(arm: str) -> dict[str, object]:
    return {
        "schema_version": 1, "kind": "kauri-n31-static-e0-feasibility-preflight-v1",
        "verdict": "PREFLIGHT_OK_NO_EXECUTION", "revision": "a" * 40,
        "profile_sha256": "b" * 64, "arm": arm,
        "binary_sha256": {name: hashlib.sha256(name.encode()).hexdigest()
                          for name in ("app", "keygen", "tls_keygen", "native_digest")},
    }


def test_v7_freeze_is_strict_and_does_not_accept_v1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "REPOSITORY", (tmp_path / "repo").resolve())
    _path, freeze = _freeze(tmp_path, module)
    assert module.validate_freeze_v7(freeze)["verdict"] == "PASS"
    legacy = dict(freeze)
    legacy["schema_version"] = 1
    legacy["kind"] = "kauri-w16-cpu-repeat-freeze-v1"
    assert module.validate_freeze_v7(legacy)["verdict"] != "PASS"
    drifted = dict(freeze)
    drifted["cell_validator_version"] = 6
    assert module.validate_freeze_v7(drifted)["verdict"] != "PASS"
    for field in ("replica_count", "quorum", "authorization_schema_version", "cell_validator_version"):
        float_drift = dict(freeze)
        float_drift[field] = float(freeze[field])
        assert module.validate_freeze_v7(float_drift)["verdict"] != "PASS"
    float_replica = dict(freeze)
    float_replica["slow_replica_ids"] = [0.0, 1, 2, 3, 4, 5]
    assert module.validate_freeze_v7(float_replica)["verdict"] != "PASS"


def test_v7_prepare_writes_fresh_v3_authorizations_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    repository = (tmp_path / "repo").resolve()
    monkeypatch.setattr(module, "REPOSITORY", repository)
    freeze_path, _freeze_value = _freeze(tmp_path, module)
    monkeypatch.setattr(
        module.feasibility, "preflight",
        lambda **kwargs: _preflight(str(kwargs["arm"])),
    )
    result = module.prepare_sequence_v7(
        freeze_path, approval_ref="test-approval:v7",
        approved_at_utc="2026-09-27T12:00:00Z",
    )
    assert result["verdict"] == "PREPARED_NO_EXECUTION", result
    manifest_path = Path(str(result["manifest_path"]))
    manifest = json.loads(manifest_path.read_text())
    assert module.validate_manifest_v7(
        manifest, freeze_bytes=freeze_path.read_bytes(), approval_ref="test-approval:v7",
    )["verdict"] == "PASS"
    assert len(manifest["cells"]) == 24
    for field in (
        "ordinal", "block_index", "block_cell_ordinal", "hard_timeout_s",
        "external_timeout_s", "automatic_retries",
    ):
        drifted_manifest = dict(manifest)
        drifted_manifest["cells"] = [dict(cell) for cell in manifest["cells"]]
        drifted_manifest["cells"][0][field] = float(manifest["cells"][0][field])
        assert module.validate_manifest_v7(
            drifted_manifest, freeze_bytes=freeze_path.read_bytes(),
            approval_ref="test-approval:v7",
        )["verdict"] != "PASS"
    authorization = json.loads(Path(manifest["cells"][0]["authorization_path"]).read_text())
    assert authorization["schema_version"] == 3
    assert authorization["executor_receipt_schema"] == "kauri-n31-static-e0-local-executor-v3"
    assert authorization["cell_validator_version"] == 7
    assert authorization["process_cleanup_required"] is True
    authorization_path = Path(manifest["cells"][0]["authorization_path"])
    original_authorization = authorization_path.read_bytes()
    for field in (
        "schema_version", "block_index", "cell_ordinal",
        "required_complete_cycles", "hard_timeout_s", "external_timeout_s",
        "automatic_retries", "cell_validator_version",
    ):
        drifted_authorization = dict(authorization)
        drifted_authorization[field] = float(authorization[field])
        changed = _write(authorization_path, drifted_authorization)
        drifted_manifest = dict(manifest)
        drifted_manifest["cells"] = [dict(cell) for cell in manifest["cells"]]
        drifted_manifest["cells"][0]["authorization_sha256"] = hashlib.sha256(changed).hexdigest()
        assert module.validate_manifest_v7(
            drifted_manifest, freeze_bytes=freeze_path.read_bytes(),
            approval_ref="test-approval:v7",
        )["verdict"] != "PASS"
    authorization_path.write_bytes(original_authorization)

    authorization["process_cleanup_required"] = False
    _write(Path(manifest["cells"][0]["authorization_path"]), authorization)
    assert module.validate_manifest_v7(
        manifest, freeze_bytes=freeze_path.read_bytes(), approval_ref="test-approval:v7",
    )["verdict"] != "PASS"


def _prepared_sequence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = _module()
    monkeypatch.setattr(module, "REPOSITORY", (tmp_path / "repo").resolve())
    freeze_path, _freeze_value = _freeze(tmp_path, module)
    monkeypatch.setattr(
        module.feasibility, "preflight",
        lambda **kwargs: _preflight(str(kwargs["arm"])),
    )
    prepared = module.prepare_sequence_v7(
        freeze_path, approval_ref="test-approval:v7",
        approved_at_utc="2026-09-27T12:00:00Z",
    )
    assert prepared["verdict"] == "PREPARED_NO_EXECUTION", prepared
    return module, freeze_path, prepared


def test_v7_execution_rechecks_live_source_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, freeze_path, prepared = _prepared_sequence(tmp_path, monkeypatch)
    def dirty(**_kwargs):
        raise module.feasibility.StaticE0FeasibilityError("repository is dirty")
    monkeypatch.setattr(module.feasibility, "preflight", dirty)
    monkeypatch.setattr(
        module.subprocess, "Popen",
        lambda *_args, **_kwargs: pytest.fail("cell launched after source drift"),
    )
    result = module.execute_sequence_v7(
        Path(str(prepared["manifest_path"])),
        manifest_sha256=str(prepared["manifest_sha256"]),
        freeze_file=freeze_path, approval_ref="test-approval:v7",
    )
    assert result["verdict"] == "STOPPED"
    assert result["attempted_cells"] == 0
    assert "dirty" in result["detail"]


def test_v7_controller_interrupt_waits_for_existing_child_before_stopping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, freeze_path, prepared = _prepared_sequence(tmp_path, monkeypatch)
    launches: list[object] = []

    class InterruptedChild:
        waits = 0

        def wait(self) -> int:
            self.waits += 1
            if self.waits == 1:
                raise KeyboardInterrupt
            return 0

    def launch(*_args, **_kwargs):
        child = InterruptedChild()
        launches.append(child)
        return child

    monkeypatch.setattr(module.subprocess, "Popen", launch)
    result = module.execute_sequence_v7(
        Path(str(prepared["manifest_path"])),
        manifest_sha256=str(prepared["manifest_sha256"]),
        freeze_file=freeze_path, approval_ref="test-approval:v7",
    )
    assert result["verdict"] == "STOPPED"
    assert result["reason_code"] == "controller_interrupted"
    assert result["attempted_cells"] == 1
    assert len(launches) == 1
    assert launches[0].waits == 2
    assert result["cells"][0]["controller_interruptions"] == 1
