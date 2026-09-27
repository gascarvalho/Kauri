"""Keep the prospective W16 v3 CPU authorization separate from v2."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from experiments.adaptive.kauri_experiment import n31_static_e0_feasibility as feasibility
from experiments.adaptive.tests.test_w16_cpu_campaign import (
    _campaign_root_v3, _cli_module, _write_json,
)


def _v3_inputs(tmp_path: Path) -> tuple[Path, Path, dict[str, object], bytes]:
    root = _campaign_root_v3(tmp_path)
    receipt = json.loads((root / "feasibility-receipt.json").read_text(encoding="utf-8"))
    authorization_path = root / "authorization.json"
    authorization = json.loads(authorization_path.read_text(encoding="utf-8"))
    freeze_path = tmp_path / "campaign-freeze-v2.json"
    repository = Path(__file__).resolve().parents[3]
    timeout = tmp_path / "timeout"
    timeout.write_bytes(b"synthetic-timeout\n")
    timeout.chmod(0o755)
    _write_json(freeze_path, {
        "schema_version": 2,
        "kind": "kauri-w16-cpu-repeat-freeze-v2",
        "campaign_id": authorization["campaign_id"],
        "revision": authorization["revision"],
        "host": "proteina02",
        "booking_id": "1hfblbqhgpne9en0k05jaq83t0",
        "repository_root": str(repository),
        "output_parent": str(repository / "results" / "w16-cpu-repeat-v3-test"),
        "evidence_dir": str(repository / "build-adaptive" / "w16-cpu-repeat-v3-test"),
        "timeout_path": str(timeout),
        "timeout_sha256": hashlib.sha256(timeout.read_bytes()).hexdigest(),
        "block_orders": ["forward", "reverse", "forward", "reverse", "forward", "reverse"],
        "replica_count": 31, "quorum": 21, "fanout": 5, "tree_count": 21,
        "slow_replica_ids": list(range(6)), "slow_quota_percent": 25,
        "other_quota_percent": 100, "complete_cycles": 5,
        "hard_timeout_s": 480, "external_timeout_s": 720,
        "automatic_retries": 0, "positive_blocks_required": 5,
        "positive_per_order_required": 2,
        "adjusted_gain_numerator": 11,
        "adjusted_gain_denominator": 10,
        "pilot_excluded": True,
        "authorization_schema_version": 3,
        "authorization_kind": authorization["kind"],
        "executor_receipt_schema": authorization["executor_receipt_schema"],
        "cell_validator_version": 7,
        "process_cleanup_required": True,
    })
    authorization["campaign_freeze_sha256"] = hashlib.sha256(
        freeze_path.read_bytes()
    ).hexdigest()
    _write_json(authorization_path, authorization)
    return root, freeze_path, receipt["preflight"], feasibility.canonical_json(receipt["preflight"])


def _check(
    root: Path, freeze_path: Path, preflight: dict[str, object],
    preflight_bytes: bytes,
) -> bytes:
    return _cli_module()._read_cpu_authorization(
        root / "authorization.json", preflight_bytes=preflight_bytes,
        preflight=preflight, arm="slow-roots", quota_mode="heterogeneous",
        output=root, hard_timeout_s=480, campaign_freeze_file=freeze_path,
        campaign_approval_ref="test-approval:synthetic-w16-campaign",
    )


def test_v3_authorization_binds_exact_freeze_and_cell(tmp_path: Path) -> None:
    root, freeze_path, preflight, preflight_bytes = _v3_inputs(tmp_path)
    assert _check(root, freeze_path, preflight, preflight_bytes) == (
        root / "authorization.json"
    ).read_bytes()


@pytest.mark.parametrize("mutation", (
    "validator-version", "cleanup-required", "receipt-schema", "freeze-kind",
    "freeze-hash", "approval-ref", "extra-key", "extra-freeze-field",
    "missing-freeze-field",
))
def test_v3_authorization_rejects_contract_or_approval_drift(
    tmp_path: Path, mutation: str,
) -> None:
    root, freeze_path, preflight, preflight_bytes = _v3_inputs(tmp_path)
    auth_path = root / "authorization.json"
    auth = json.loads(auth_path.read_text(encoding="utf-8"))
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    if mutation == "validator-version":
        auth["cell_validator_version"] = 6
    elif mutation == "cleanup-required":
        auth["process_cleanup_required"] = False
    elif mutation == "receipt-schema":
        auth["executor_receipt_schema"] = "kauri-n31-static-e0-local-executor-v2"
    elif mutation == "freeze-kind":
        freeze["kind"] = "kauri-w16-cpu-repeat-freeze-v1"
    elif mutation == "freeze-hash":
        auth["campaign_freeze_sha256"] = "f" * 64
    elif mutation == "approval-ref":
        auth["approval_ref"] = "wrong-approval"
    elif mutation == "extra-freeze-field":
        freeze["unexpected"] = True
        auth["campaign_freeze_sha256"] = hashlib.sha256(
            json.dumps(freeze, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        ).hexdigest()
    elif mutation == "missing-freeze-field":
        freeze.pop("quorum")
        auth["campaign_freeze_sha256"] = hashlib.sha256(
            json.dumps(freeze, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        ).hexdigest()
    else:
        auth["unexpected"] = True
    _write_json(freeze_path, freeze)
    _write_json(auth_path, auth)
    with pytest.raises(_cli_module().executor.LocalExecutorError):
        _check(root, freeze_path, preflight, preflight_bytes)
