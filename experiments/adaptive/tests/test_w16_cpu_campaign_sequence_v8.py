"""Contracts for the distinct W16 v8 sequence; no cell is launched."""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
import sys

import pytest


def _module():
    return importlib.import_module("experiments.adaptive.kauri_experiment.w16_cpu_campaign_sequence_v8")


def _write(path: Path, value: object) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    path.write_bytes(payload)
    return payload


def _freeze(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    repository = (tmp_path / "repo").resolve()
    timeout = repository / "bin" / "timeout"
    timeout.parent.mkdir(parents=True)
    timeout.write_bytes(b"synthetic-timeout\n")
    timeout.chmod(0o755)
    freeze = {
        "schema_version": 3, "kind": "kauri-w16-cpu-repeat-freeze-v8-neutral-v1",
        "campaign_id": "w16-cpu-repeat-v8-test", "revision": "a" * 40,
        "host": "proteina02", "booking_id": "1hfblbqhgpne9en0k05jaq83t0",
        "repository_root": str(repository),
        "output_parent": str(repository / "results" / "w16-cpu-repeat-v8-test"),
        "evidence_dir": str(repository / "build-adaptive" / "w16-cpu-repeat-v8-test"),
        "timeout_path": str(timeout),
        "timeout_sha256": hashlib.sha256(timeout.read_bytes()).hexdigest(),
        "block_orders": ["forward", "reverse", "forward", "reverse", "forward", "reverse"],
        "replica_count": 31, "quorum": 21, "fanout": 5, "tree_count": 21,
        "authoritative_observer": 27,
        "slow_replica_ids": list(range(6)), "slow_quota_percent": 25,
        "other_quota_percent": 100, "complete_cycles": 5,
        "hard_timeout_s": 480, "external_timeout_s": 720, "automatic_retries": 0,
        "positive_blocks_required": 5, "positive_per_order_required": 2,
        "adjusted_gain_numerator": 11, "adjusted_gain_denominator": 10,
        "pilot_excluded": True, "authorization_schema_version": 3,
        "authorization_kind": "kauri-w16-static-e0-campaign-authorization-v3",
        "executor_receipt_schema": "kauri-n31-static-e0-local-executor-v3",
        "cell_validator_version": 8, "process_cleanup_required": True,
    }
    path = tmp_path / "freeze-v3.json"
    _write(path, freeze)
    return path, freeze


def _preflight(arm: str) -> dict[str, object]:
    return {
        "schema_version": 8, "kind": "kauri-n31-static-e0-feasibility-preflight-v8",
        "verdict": "PREFLIGHT_OK_NO_EXECUTION", "revision": "a" * 40,
        "profile_id": "n31-static-e0-local-feasibility-v8", "profile_sha256": "b" * 64, "arm": arm,
        "binary_sha256": {name: hashlib.sha256(name.encode()).hexdigest()
                          for name in ("app", "keygen", "tls_keygen", "native_digest")},
    }


def _execution_approval(
    path: Path,
    prepared: dict[str, object],
    freeze_path: Path,
    *,
    user_approval_ref: str = "user-confirmation:v8",
) -> Path:
    manifest_path = Path(str(prepared["manifest_path"]))
    manifest = json.loads(manifest_path.read_text())
    approval = {
        "schema_version": 1,
        "kind": "kauri-w16-cpu-repeat-execution-approval-v1",
        "campaign_id": manifest["campaign_id"],
        "revision": manifest["revision"],
        "campaign_freeze_sha256": str(prepared["campaign_freeze_sha256"]),
        "manifest_sha256": str(prepared["manifest_sha256"]),
        "preflight_sha256s": [cell["preflight_sha256"] for cell in manifest["cells"]],
        "authorization_sha256s": [cell["authorization_sha256"] for cell in manifest["cells"]],
        "user_approval_ref": user_approval_ref,
        "approved_at_utc": "2026-09-27T12:15:00Z",
    }
    _write(path, approval)
    return path


def test_v8_freeze_is_strict_and_v7_rejects_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "REPOSITORY", (tmp_path / "repo").resolve())
    _path, freeze = _freeze(tmp_path)
    assert module.validate_freeze_v8(freeze)["verdict"] == "PASS"
    assert importlib.import_module(
        "experiments.adaptive.kauri_experiment.w16_cpu_campaign_sequence_v7"
    ).validate_freeze_v7(freeze)["verdict"] != "PASS"
    for field in ("cell_validator_version", "automatic_retries", "authorization_schema_version"):
        drifted = dict(freeze)
        drifted[field] = 7 if field == "cell_validator_version" else 1
        assert module.validate_freeze_v8(drifted)["verdict"] != "PASS"
    wrong_output = dict(freeze)
    wrong_output["output_parent"] = str((tmp_path / "repo" / "results" / "still-v8-but-not-campaign").resolve())
    assert module.validate_freeze_v8(wrong_output)["verdict"] != "PASS"


def test_v8_prepare_binds_distinct_roots_and_validator_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "REPOSITORY", (tmp_path / "repo").resolve())
    freeze_path, _freeze_value = _freeze(tmp_path)
    monkeypatch.setattr(module.feasibility, "preflight", lambda **kwargs: _preflight(str(kwargs["arm"])))
    result = module.prepare_sequence_v8(
        freeze_path, approval_ref="test-approval:v8", approved_at_utc="2026-09-27T12:00:00Z",
    )
    assert result["verdict"] == "PREPARED_NO_EXECUTION", result
    manifest_path = Path(str(result["manifest_path"]))
    manifest = json.loads(manifest_path.read_text())
    assert manifest["kind"] == "kauri-w16-cpu-repeat-sequence-manifest-v8-neutral-v1"
    assert manifest_path.name == "manifest-v8.json"
    assert module.validate_manifest_v8(manifest, freeze_bytes=freeze_path.read_bytes(), approval_ref="test-approval:v8")["verdict"] == "PASS"
    authorization = json.loads(Path(manifest["cells"][0]["authorization_path"]).read_text())
    assert authorization["schema_version"] == 3
    assert authorization["cell_validator_version"] == 8
    assert authorization["automatic_retries"] == 0


def test_v8_execution_stops_before_launch_when_live_preflight_drifts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "REPOSITORY", (tmp_path / "repo").resolve())
    freeze_path, _freeze_value = _freeze(tmp_path)
    monkeypatch.setattr(module.feasibility, "preflight", lambda **kwargs: _preflight(str(kwargs["arm"])))
    prepared = module.prepare_sequence_v8(
        freeze_path, approval_ref="test-approval:v8", approved_at_utc="2026-09-27T12:00:00Z",
    )
    assert prepared["verdict"] == "PREPARED_NO_EXECUTION"
    execution_approval = _execution_approval(tmp_path / "execution-approval-v1.json", prepared, freeze_path)
    monkeypatch.setattr(module.feasibility, "preflight", lambda **_kwargs: {"verdict": "DRIFT"})
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("launched after drift"))
    result = module.execute_sequence_v8(
        Path(str(prepared["manifest_path"])), manifest_sha256=str(prepared["manifest_sha256"]),
        freeze_file=freeze_path, approval_ref="test-approval:v8",
        execution_approval_file=execution_approval,
    )
    assert result["verdict"] == "STOPPED"
    assert result["attempted_cells"] == 0
    assert result["reason_code"] == "live_preflight_drift"


def test_v8_execution_stops_on_post_wrapper_authorization_hash_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "REPOSITORY", (tmp_path / "repo").resolve())
    freeze_path, _freeze_value = _freeze(tmp_path)
    monkeypatch.setattr(module.feasibility, "preflight", lambda **kwargs: _preflight(str(kwargs["arm"])))
    prepared = module.prepare_sequence_v8(
        freeze_path, approval_ref="test-approval:v8", approved_at_utc="2026-09-27T12:00:00Z",
    )
    execution_approval = _execution_approval(tmp_path / "execution-approval-v1.json", prepared, freeze_path)

    class Child:
        def wait(self) -> int:
            return 0

    launches: list[object] = []
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: launches.append(Child()) or launches[-1])
    monkeypatch.setattr(module, "validate_w16_output_v8", lambda _root: {
        "verdict": "PASS", "authorization": {"sha256": "0" * 64},
    })
    result = module.execute_sequence_v8(
        Path(str(prepared["manifest_path"])), manifest_sha256=str(prepared["manifest_sha256"]),
        freeze_file=freeze_path, approval_ref="test-approval:v8",
        execution_approval_file=execution_approval,
    )
    assert result["verdict"] == "STOPPED"
    assert result["first_failure_ordinal"] == 1
    assert result["reason_code"] == "authorization_binding"
    assert result["cells"][0]["validation_verdict"] == "FAIL_AUTHORIZATION_BINDING"
    assert len(launches) == 1
    execution_root = Path(str(prepared["manifest_path"])).parent / "execution-v8"
    assert (execution_root / "snapshot" / "execution-approval-v1.json").read_bytes() == execution_approval.read_bytes()


def test_v8_execution_requires_exact_post_manifest_approval_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "REPOSITORY", (tmp_path / "repo").resolve())
    freeze_path, _freeze_value = _freeze(tmp_path)
    monkeypatch.setattr(module.feasibility, "preflight", lambda **kwargs: _preflight(str(kwargs["arm"])))
    prepared = module.prepare_sequence_v8(
        freeze_path, approval_ref="preparation-only:v8", approved_at_utc="2026-09-27T12:00:00Z",
    )
    assert prepared["verdict"] == "PREPARED_NO_EXECUTION"
    manifest_path = Path(str(prepared["manifest_path"]))
    launches: list[object] = []
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: launches.append(object()))

    missing = module.execute_sequence_v8(
        manifest_path, manifest_sha256=str(prepared["manifest_sha256"]), freeze_file=freeze_path,
        approval_ref="preparation-only:v8", execution_approval_file=tmp_path / "missing.json",
    )
    assert missing["reason_code"] == "missing_input"
    assert missing["attempted_cells"] == 0
    assert not (manifest_path.parent / "execution-v8").exists()

    pre_manifest_only = tmp_path / "preparation-approval.json"
    _write(pre_manifest_only, {"approval_ref": "preparation-only:v8"})
    rejected = module.execute_sequence_v8(
        manifest_path, manifest_sha256=str(prepared["manifest_sha256"]), freeze_file=freeze_path,
        approval_ref="preparation-only:v8", execution_approval_file=pre_manifest_only,
    )
    assert rejected["reason_code"] == "execution_approval_contract"
    assert rejected["attempted_cells"] == 0
    assert not (manifest_path.parent / "execution-v8").exists()
    assert not launches


def test_v8_run_cli_rejects_pre_manifest_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    cli = importlib.import_module("experiments.adaptive.run_w16_cpu_campaign_sequence_v8")
    monkeypatch.setattr(sys, "argv", [
        "run_w16_cpu_campaign_sequence_v8.py", "run", "--freeze-file", "/tmp/freeze.json",
        "--manifest", "/tmp/manifest.json", "--manifest-sha256", "a" * 64,
        "--approval-ref", "preparation-only:v8",
    ])
    monkeypatch.setattr(cli, "execute_sequence_v8", lambda **_kwargs: pytest.fail("launched without post-manifest approval"))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


def test_v8_execution_approval_rejects_every_bound_identity_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "REPOSITORY", (tmp_path / "repo").resolve())
    freeze_path, _freeze_value = _freeze(tmp_path)
    monkeypatch.setattr(module.feasibility, "preflight", lambda **kwargs: _preflight(str(kwargs["arm"])))
    prepared = module.prepare_sequence_v8(
        freeze_path, approval_ref="test-approval:v8", approved_at_utc="2026-09-27T12:00:00Z",
    )
    manifest_path = Path(str(prepared["manifest_path"]))
    valid_path = _execution_approval(tmp_path / "valid-approval.json", prepared, freeze_path)
    valid = json.loads(valid_path.read_text())
    mutations = (
        {**valid, "campaign_id": "w16-cpu-repeat-v8-other"},
        {**valid, "revision": "c" * 40},
        {**valid, "campaign_freeze_sha256": "d" * 64},
        {**valid, "manifest_sha256": "e" * 64},
        {**valid, "preflight_sha256s": ["e" * 64, *valid["preflight_sha256s"][1:]]},
        {**valid, "authorization_sha256s": ["f" * 64, *valid["authorization_sha256s"][1:]]},
        {**valid, "user_approval_ref": ""},
        {**valid, "user_approval_ref": "test-approval:v8"},
        {**valid, "approved_at_utc": "not-a-timestamp"},
        {**valid, "extra": True},
    )
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("launched with mutated approval"))
    for index, mutation in enumerate(mutations):
        path = tmp_path / f"mutated-{index}.json"
        _write(path, mutation)
        result = module.execute_sequence_v8(
            manifest_path, manifest_sha256=str(prepared["manifest_sha256"]), freeze_file=freeze_path,
            approval_ref="test-approval:v8", execution_approval_file=path,
        )
        assert result["reason_code"] == "execution_approval_contract"
        assert result["attempted_cells"] == 0
    noncanonical = tmp_path / "noncanonical.json"
    noncanonical.write_bytes(valid_path.read_bytes() + b" ")
    result = module.execute_sequence_v8(
        manifest_path, manifest_sha256=str(prepared["manifest_sha256"]), freeze_file=freeze_path,
        approval_ref="test-approval:v8", execution_approval_file=noncanonical,
    )
    assert result["reason_code"] == "execution_approval_bytes"
    assert result["attempted_cells"] == 0
    assert not (manifest_path.parent / "execution-v8").exists()


def test_executor_accepts_only_v8_bound_to_v8_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    repository = (tmp_path / "repo").resolve()
    monkeypatch.setattr(module, "REPOSITORY", repository)
    freeze_path, freeze = _freeze(tmp_path)
    output = (tmp_path / "cell-output").resolve()
    preflight = _preflight("slow-roots")
    preflight_bytes = json.dumps(preflight, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    approval_ref = "test-approval:v8"
    authorization = {
        "schema_version": 3,
        "kind": "kauri-w16-static-e0-campaign-authorization-v3",
        "campaign_id": freeze["campaign_id"], "block_index": 1,
        "campaign_freeze_sha256": hashlib.sha256(freeze_path.read_bytes()).hexdigest(),
        "block_id": f"{freeze['campaign_id']}-block-01",
        "block_order": ["slow-roots:homogeneous", "fast-roots:homogeneous", "slow-roots:heterogeneous", "fast-roots:heterogeneous"],
        "cell_ordinal": 1, "revision": preflight["revision"],
            "profile_sha256": preflight["profile_sha256"], "arm": "slow-roots",
            "quota_mode": "homogeneous", "authoritative_observer": 27, "preflight_sha256": hashlib.sha256(preflight_bytes).hexdigest(),
        "binary_sha256": preflight["binary_sha256"], "output_root": str(output),
        "required_complete_cycles": 5, "hard_timeout_s": 480, "external_timeout_s": 720,
        "automatic_retries": 0, "claim_eligible": False, "figure_eligible": False,
        "approval_ref": approval_ref, "approved_at_utc": "2026-09-27T12:00:00Z",
        "executor_receipt_schema": "kauri-n31-static-e0-local-executor-v3",
        "cell_validator_version": 8, "process_cleanup_required": True,
    }
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    producer_sequence = importlib.import_module("kauri_experiment.w16_cpu_campaign_sequence_v8")
    monkeypatch.setattr(producer_sequence, "REPOSITORY", repository)
    from experiments.adaptive import run_n31_static_e0_local_executor as cli

    payload = _canonical = json.dumps(authorization, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    assert cli._read_campaign_cpu_authorization_v3(
        payload, authorization, preflight_bytes=preflight_bytes, preflight=preflight,
        arm="slow-roots", quota_mode="homogeneous", output=output,
        hard_timeout_s=480, campaign_freeze_file=freeze_path,
        campaign_approval_ref=approval_ref,
    ) == payload
    authorization["cell_validator_version"] = 7
    with pytest.raises(cli.executor.LocalExecutorError, match="authorization contract differs"):
        cli._read_campaign_cpu_authorization_v3(
            payload, authorization, preflight_bytes=preflight_bytes, preflight=preflight,
            arm="slow-roots", quota_mode="homogeneous", output=output,
            hard_timeout_s=480, campaign_freeze_file=freeze_path,
            campaign_approval_ref=approval_ref,
        )


def test_v8_campaign_disclosure_rejects_omission_and_mutation() -> None:
    campaign = importlib.import_module(
        "experiments.adaptive.kauri_experiment.w16_campaign_validator_v8"
    )
    valid = {
        "schema_version": 1,
        "event_type": "block.commit_identity_unavailable",
        "total_count": 1,
        "in_measurement_count": 1,
        "details": [{
            "replica_id": 0, "block_height": 700, "block_hash": "c" * 64,
            "transaction_count": 1000, "phase": "in_measurement",
            "rich_peer_count": 30, "observed_replica_count": 31,
            "tree_id": 15, "view_generation": 100,
        }],
        "undisputed_height_gaps": [],
        "unanimous_observed_bridges": [],
        "post_measurement_peer_tails": {
            "count": 1,
            "reporters": [0, 1],
            "details": [{
                "block_height": 701, "block_hash": "d" * 64,
                "parent_hash": "c" * 64, "transaction_count": 1000,
                "reporters": [0, 1], "tree_id": 15, "view_generation": 101,
            }],
        },
    }
    assert campaign._identity_unavailable(valid, ordinal=1, authoritative_observer=27)["total_count"] == 1
    for mutation in (
        {},
        {**valid, "in_measurement_count": 0},
        {**valid, "details": [{**valid["details"][0], "rich_peer_count": 29}]},
            {**valid, "details": [{**valid["details"][0], "replica_id": 27}]},
    ):
        with pytest.raises(campaign._InvalidCampaign):
            campaign._identity_unavailable(mutation, ordinal=1, authoritative_observer=27)
