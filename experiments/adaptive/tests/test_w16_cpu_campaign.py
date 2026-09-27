"""Prospective contracts for the W16 N=31 counterbalanced CPU campaign.

These tests intentionally target the versioned v2/v6/campaign entry points.
They must never cause the v1 authorization or v4/v3 block verdicts to be
reinterpreted.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
import sys

import pytest

from experiments.adaptive.kauri_experiment import n31_static_e0_feasibility as feasibility
from experiments.adaptive.tests.test_w16_block_validator import _replace_run_id
from experiments.adaptive.tests.test_w16_output_validator import _build_output, _reseal


FORWARD = (
    "slow-roots:homogeneous",
    "fast-roots:homogeneous",
    "slow-roots:heterogeneous",
    "fast-roots:heterogeneous",
)
REVERSE = tuple(reversed(FORWARD))
CAMPAIGN_ID = "w16-cpu-repeat-test"
APPROVAL_REF = "test-approval:synthetic-w16-campaign"
V2_KIND = "kauri-w16-static-e0-campaign-authorization-v2"
_FREEZE_BYTES = b'{"campaign":"w16-cpu-repeat-test","version":1}\n'
CAMPAIGN_FREEZE_SHA256 = hashlib.sha256(_FREEZE_BYTES).hexdigest()


def _cli_module():
    adaptive_root = str(Path(__file__).resolve().parents[1])
    if adaptive_root not in sys.path:
        sys.path.insert(0, adaptive_root)
    return importlib.import_module("experiments.adaptive.run_n31_static_e0_local_executor")


def _campaign_module():
    """Load the prospective implementation, failing clearly until P2 exists."""

    return importlib.import_module(
        "experiments.adaptive.kauri_experiment.w16_campaign_validator"
    )


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _freeze_file(tmp_path: Path, *, contents: bytes = _FREEZE_BYTES) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "campaign-freeze.json"
    path.write_bytes(contents)
    return path


def _v2_authorization(
    *, preflight: dict[str, object], preflight_bytes: bytes, root: Path,
    order: tuple[str, ...], ordinal: int, campaign_id: str = CAMPAIGN_ID,
    block_index: int = 1, freeze_sha256: str = CAMPAIGN_FREEZE_SHA256,
) -> dict[str, object]:
    arm, quota_mode = order[ordinal - 1].split(":", 1)
    return {
        "schema_version": 2,
        "kind": V2_KIND,
        "campaign_id": campaign_id,
        "block_index": block_index,
        "campaign_freeze_sha256": freeze_sha256,
        "block_id": f"{campaign_id}-block-{block_index:02d}",
        "block_order": list(order),
        "cell_ordinal": ordinal,
        "revision": preflight["revision"],
        "profile_sha256": preflight["profile_sha256"],
        "arm": arm,
        "quota_mode": quota_mode,
        "preflight_sha256": hashlib.sha256(preflight_bytes).hexdigest(),
        "binary_sha256": preflight["binary_sha256"],
        "output_root": str(root.resolve()),
        "required_complete_cycles": 5,
        "hard_timeout_s": 480,
        "external_timeout_s": 720,
        "automatic_retries": 0,
        "claim_eligible": False,
        "figure_eligible": False,
        "approval_ref": APPROVAL_REF,
        "approved_at_utc": "2026-09-27T09:00:00Z",
    }


def _campaign_root(
    tmp_path: Path, *, order: tuple[str, ...], ordinal: int,
    block_index: int = 1, real_raw_interval: bool = False,
) -> Path:
    """Create a sealed v4-like root upgraded only to a v2 authorization."""

    arm, mode = order[ordinal - 1].split(":", 1)
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = _build_output(
        tmp_path / f"block-{block_index}-cell-{ordinal}", cpu_mode=mode, arm=arm,
    )
    _replace_run_id(root, f"campaign-{block_index}-run-{ordinal}", ordinal)
    receipt_path = root / "feasibility-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["schema"] = "kauri-n31-static-e0-local-executor-v2"
    # The unshifted synthetic raw stream lies near 1e9.  Only direct v6 tests
    # request a genuine producer interval; mocked block/campaign tests retain
    # artificial, sequential intervals to exercise chronology separately.
    if real_raw_interval:
        # Deliberately offset Python CLOCK_MONOTONIC by 37 seconds from the
        # native RAW stream.  V6 must use explicit RAW bounds, never infer or
        # compare cross-clock values directly.
        receipt["started_monotonic_ns"] = 37_000_000_001
        receipt["ended_monotonic_ns"] = 137_000_000_000
    else:
        receipt["started_monotonic_ns"] = (
            block_index * 1_000_000_000_000_000 + ordinal * 2_000_000_000_000
        )
        receipt["ended_monotonic_ns"] = receipt["started_monotonic_ns"] + 1_000_000_000_004
    receipt["raw_clock_id"] = "CLOCK_MONOTONIC_RAW"
    if real_raw_interval:
        receipt["started_raw_monotonic_ns"] = 1
        receipt["ended_raw_monotonic_ns"] = 100_000_000_000
    else:
        receipt["started_raw_monotonic_ns"] = receipt["started_monotonic_ns"]
        receipt["ended_raw_monotonic_ns"] = receipt["ended_monotonic_ns"]
    preflight_bytes = feasibility.canonical_json(receipt["preflight"])
    authorization = _v2_authorization(
        preflight=receipt["preflight"], preflight_bytes=preflight_bytes,
        root=root, order=order, ordinal=ordinal, block_index=block_index,
    )
    _write_json(root / "authorization.json", authorization)
    receipt["authorization_sha256"] = hashlib.sha256(
        (root / "authorization.json").read_bytes()
    ).hexdigest()
    _write_json(receipt_path, receipt)
    _reseal(root)
    return root


def test_v2_authorization_accepts_each_declared_order_and_rejects_cross_binding(
    tmp_path: Path,
) -> None:
    """The producer must bind raw preflight bytes and the exact F/R ordinal."""

    cli = _cli_module()
    freeze_file = _freeze_file(tmp_path)
    preflight = {
        "revision": "a" * 40,
        "profile_sha256": "b" * 64,
        "binary_sha256": {name: hashlib.sha256(name.encode()).hexdigest()
                          for name in ("app", "keygen", "tls_keygen", "native_digest")},
    }
    # Same JSON meaning but different bytes: v2 must bind the supplied bytes.
    preflight_bytes = feasibility.canonical_json(preflight) + b" "

    for order in (FORWARD, REVERSE):
        block_index = 1 if order == FORWARD else 2
        for ordinal, label in enumerate(order, 1):
            root = tmp_path / f"{order[0]}-{ordinal}"
            root.mkdir()
            authorization = _v2_authorization(
                preflight=preflight, preflight_bytes=preflight_bytes, root=root,
                order=order, ordinal=ordinal, block_index=block_index,
            )
            path = root / "authorization.json"
            _write_json(path, authorization)
            arm, quota_mode = label.split(":", 1)
            assert cli._read_cpu_authorization(
                path, preflight_bytes=preflight_bytes, preflight=preflight,
                arm=arm, quota_mode=quota_mode, output=root, hard_timeout_s=480,
                campaign_freeze_file=freeze_file,
                campaign_approval_ref=APPROVAL_REF,
            ) == path.read_bytes()

            authorization["block_order"] = list(
                REVERSE if order == FORWARD else FORWARD
            )
            _write_json(path, authorization)
            with pytest.raises(cli.executor.LocalExecutorError):
                cli._read_cpu_authorization(
                    path, preflight_bytes=preflight_bytes, preflight=preflight,
                    arm=arm, quota_mode=quota_mode, output=root, hard_timeout_s=480,
                    campaign_freeze_file=freeze_file,
                    campaign_approval_ref=APPROVAL_REF,
                )


@pytest.mark.parametrize(
    "field,value",
    [
        ("campaign_id", "other-campaign"),
        ("block_index", 0),
        ("block_id", "w16-cpu-repeat-test-block-99"),
        ("campaign_freeze_sha256", "G" * 64),
        ("required_complete_cycles", 4),
        ("external_timeout_s", 721),
        ("automatic_retries", 1),
        ("approval_ref", ""),
    ],
)
def test_v2_authorization_rejects_campaign_or_execution_drift(
    tmp_path: Path, field: str, value: object,
) -> None:
    cli = _cli_module()
    freeze_file = _freeze_file(tmp_path)
    root = tmp_path / "out"; root.mkdir()
    preflight = {
        "revision": "a" * 40, "profile_sha256": "b" * 64,
        "binary_sha256": {name: hashlib.sha256(name.encode()).hexdigest()
                          for name in ("app", "keygen", "tls_keygen", "native_digest")},
    }
    preflight_bytes = feasibility.canonical_json(preflight)
    document = _v2_authorization(
        preflight=preflight, preflight_bytes=preflight_bytes, root=root,
        order=FORWARD, ordinal=3,
    )
    document[field] = value
    path = root / "authorization.json"; _write_json(path, document)
    with pytest.raises(cli.executor.LocalExecutorError):
        cli._read_cpu_authorization(
            path, preflight_bytes=preflight_bytes, preflight=preflight,
            arm="slow-roots", quota_mode="heterogeneous", output=root,
            hard_timeout_s=480, campaign_freeze_file=freeze_file,
            campaign_approval_ref=APPROVAL_REF,
        )


def test_v2_authorization_requires_external_freeze_and_exact_approval(
    tmp_path: Path,
) -> None:
    cli = _cli_module()
    root = tmp_path / "out"; root.mkdir()
    preflight = {
        "revision": "a" * 40, "profile_sha256": "b" * 64,
        "binary_sha256": {name: hashlib.sha256(name.encode()).hexdigest()
                          for name in ("app", "keygen", "tls_keygen", "native_digest")},
    }
    preflight_bytes = feasibility.canonical_json(preflight)
    document = _v2_authorization(
        preflight=preflight, preflight_bytes=preflight_bytes, root=root,
        order=FORWARD, ordinal=3,
    )
    path = root / "authorization.json"; _write_json(path, document)
    freeze_file = _freeze_file(tmp_path)
    arguments = {
        "preflight_bytes": preflight_bytes, "preflight": preflight,
        "arm": "slow-roots", "quota_mode": "heterogeneous", "output": root,
        "hard_timeout_s": 480,
    }
    with pytest.raises(cli.executor.LocalExecutorError):
        cli._read_cpu_authorization(path, **arguments)
    with pytest.raises(cli.executor.LocalExecutorError):
        cli._read_cpu_authorization(
            path, **arguments, campaign_freeze_file=freeze_file,
            campaign_approval_ref="different-approved-reference",
        )
    wrong_freeze = _freeze_file(tmp_path / "wrong", contents=b"different freeze\n")
    with pytest.raises(cli.executor.LocalExecutorError):
        cli._read_cpu_authorization(
            path, **arguments, campaign_freeze_file=wrong_freeze,
            campaign_approval_ref=APPROVAL_REF,
        )
    document["approved_at_utc"] = "Z"
    _write_json(path, document)
    with pytest.raises(cli.executor.LocalExecutorError):
        cli._read_cpu_authorization(
            path, **arguments, campaign_freeze_file=freeze_file,
            campaign_approval_ref=APPROVAL_REF,
        )


def test_v6_accepts_cross_clock_v2_cell_but_v5_remains_frozen(tmp_path: Path) -> None:
    """Version 6 accepts explicit RAW bounds; frozen v5 rejects this schema."""

    module = importlib.import_module(
        "experiments.adaptive.kauri_experiment.w16_output_validator"
    )
    root = _campaign_root(tmp_path, order=FORWARD, ordinal=3, real_raw_interval=True)
    receipt = json.loads((root / "feasibility-receipt.json").read_text())
    assert receipt["started_monotonic_ns"] - receipt["started_raw_monotonic_ns"] == 37_000_000_000

    v6 = module.validate_w16_output_v6(root)
    assert v6["verdict"] == "PASS", v6
    assert v6["kind"] == "kauri-w16-output-validation-v6"
    assert v6["authorization"]["campaign_id"] == CAMPAIGN_ID
    assert v6["claim_eligible"] is False
    assert module.validate_w16_output_v5(root)["verdict"] != "PASS"
    assert module.validate_w16_output_v4(root)["verdict"] != "PASS"


def test_v6_rejects_missing_or_narrow_raw_clock_bounds(tmp_path: Path) -> None:
    module = importlib.import_module(
        "experiments.adaptive.kauri_experiment.w16_output_validator"
    )
    missing = _campaign_root(
        tmp_path / "missing", order=FORWARD, ordinal=3, real_raw_interval=True,
    )
    receipt_path = missing / "feasibility-receipt.json"
    receipt = json.loads(receipt_path.read_text())
    del receipt["started_raw_monotonic_ns"]
    _write_json(receipt_path, receipt)
    _reseal(missing)
    assert module.validate_w16_output_v6(missing)["verdict"] != "PASS"

    narrow = _campaign_root(
        tmp_path / "narrow", order=FORWARD, ordinal=3, real_raw_interval=True,
    )
    receipt_path = narrow / "feasibility-receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["started_raw_monotonic_ns"] = 10 ** 18
    receipt["ended_raw_monotonic_ns"] = 10 ** 18 + 1
    _write_json(receipt_path, receipt)
    _reseal(narrow)
    assert module.validate_w16_output_v6(narrow)["verdict"] != "PASS"


def test_v1_receipt_remains_v4_compatible_without_raw_clock_fields(tmp_path: Path) -> None:
    module = importlib.import_module(
        "experiments.adaptive.kauri_experiment.w16_output_validator"
    )
    root = _build_output(tmp_path / "v1", cpu_mode="heterogeneous")
    assert module.validate_w16_output_v4(root)["verdict"] == "PASS"
    assert module.validate_w16_output_v5(root)["verdict"] != "PASS"
    assert module.validate_w16_output_v6(root)["verdict"] != "PASS"


def test_v6_rejects_receipt_interval_outside_derived_raw_window(tmp_path: Path) -> None:
    module = importlib.import_module(
        "experiments.adaptive.kauri_experiment.w16_output_validator"
    )
    root = _campaign_root(tmp_path, order=FORWARD, ordinal=3, real_raw_interval=True)
    accepted = module.validate_w16_output_v6(root)
    assert accepted["verdict"] == "PASS", accepted
    receipt_path = root / "feasibility-receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["started_raw_monotonic_ns"] = int(
        accepted["throughput"]["window_end_monotonic_ns"]
    )
    receipt["ended_raw_monotonic_ns"] = receipt["started_raw_monotonic_ns"] + 1
    _write_json(receipt_path, receipt)
    _reseal(root)

    rejected = module.validate_w16_output_v6(root)
    assert rejected["verdict"] != "PASS"
    assert rejected["reason_code"] == "raw_lifecycle_span"


def test_v6_rejects_receipt_that_excludes_native_lifecycle_span(tmp_path: Path) -> None:
    module = importlib.import_module(
        "experiments.adaptive.kauri_experiment.w16_output_validator"
    )
    root = _campaign_root(tmp_path, order=FORWARD, ordinal=3, real_raw_interval=True)
    accepted = module.validate_w16_output_v6(root)
    assert accepted["verdict"] == "PASS", accepted
    receipt_path = root / "feasibility-receipt.json"
    receipt = json.loads(receipt_path.read_text())
    # This still encloses the complete commit-derived measurement window, but
    # deliberately discards native process.started/stopped evidence.
    receipt["started_raw_monotonic_ns"] = int(
        accepted["throughput"]["window_start_monotonic_ns"]
    )
    receipt["ended_raw_monotonic_ns"] = int(
        accepted["throughput"]["window_end_monotonic_ns"]
    )
    _write_json(receipt_path, receipt)
    _reseal(root)

    rejected = module.validate_w16_output_v6(root)
    assert rejected["verdict"] != "PASS"
    assert rejected["reason_code"] == "raw_lifecycle_span"


def _mock_v6_result(root: Path) -> dict[str, object]:
    receipt = json.loads((root / "feasibility-receipt.json").read_text())
    authorization = json.loads((root / "authorization.json").read_text())
    arm = str(authorization["arm"]); mode = str(authorization["quota_mode"])
    window_start = int(receipt["started_raw_monotonic_ns"]) + 2
    window_end = int(receipt["ended_raw_monotonic_ns"]) - 2
    return {
        "schema_version": 1, "kind": "kauri-w16-output-validation-v6",
        "verdict": "PASS", "claim_eligible": False, "figure_eligible": False,
        "evidence_class": "CPU_QUOTA_SINGLE_ARM", "run_id": receipt["run_id"],
        "revision": receipt["preflight"]["revision"], "arm": arm,
        "quota": {"mode": mode},
        "authorization": {
            "campaign_id": authorization["campaign_id"],
            "block_index": authorization["block_index"],
            "campaign_freeze_sha256": authorization["campaign_freeze_sha256"],
            "block_id": authorization["block_id"],
            "cell_ordinal": authorization["cell_ordinal"],
        },
        "throughput": {
            "window_start_monotonic_ns": window_start,
            "window_end_monotonic_ns": window_end,
            "transaction_count": 1_000_000,
            "duration_ns": window_end - window_start,
            "throughput_milli_tps": 1_000_000,
        },
        "native_lifecycle_span": {
            "start_monotonic_ns": int(receipt["started_raw_monotonic_ns"]) + 1,
            "end_monotonic_ns": int(receipt["ended_raw_monotonic_ns"]) - 1,
        },
        "producer_raw_clock_span": {
            "clock_id": "CLOCK_MONOTONIC_RAW",
            "start_monotonic_ns": receipt["started_raw_monotonic_ns"],
            "end_monotonic_ns": receipt["ended_raw_monotonic_ns"],
        },
    }


def test_block_validator_maps_reverse_rates_by_label_and_rejects_relabelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = _campaign_module()
    roots = [
        _campaign_root(tmp_path, order=REVERSE, ordinal=i, block_index=2)
        for i in range(1, 5)
    ]
    results = {root.resolve(): _mock_v6_result(root) for root in roots}
    # B-X/A-X = 1.5; B-H/A-H = 1.0.  Roots remain in reverse execution order.
    rates = {
        "fast-roots:heterogeneous": 1_200_000,
        "slow-roots:heterogeneous": 800_000,
        "fast-roots:homogeneous": 1_000_000,
        "slow-roots:homogeneous": 1_000_000,
    }
    for result in results.values():
        label = f"{result['arm']}:{result['quota']['mode']}"
        result["throughput"]["transaction_count"] = rates[label]
        result["throughput"]["throughput_milli_tps"] = rates[label]
    monkeypatch.setattr(
        campaign, "validate_w16_output_v6",
        lambda root: results[Path(root).resolve()],
    )

    result = campaign.validate_w16_campaign_block(roots)
    assert result["verdict"] == "PASS", result
    assert result["execution_order"] == "reverse"
    assert result["effects"]["heterogeneous_ratio"] == pytest.approx(1.5)
    assert result["effects"]["homogeneous_ratio"] == pytest.approx(1.0)

    # Inputs whose physical order contradicts their v2 ordinal/order must fail.
    relabelled = list(reversed(roots))
    rejected = campaign.validate_w16_campaign_block(relabelled)
    assert rejected["verdict"] != "PASS"


def test_campaign_requires_six_counterbalanced_complete_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = _campaign_module()
    blocks = [
        [_campaign_root(tmp_path / f"block-{index}", order=(FORWARD if index % 2 else REVERSE),
                        ordinal=ordinal, block_index=index)
         for ordinal in range(1, 5)]
        for index in range(1, 7)
    ]
    results = {
        root.resolve(): _mock_v6_result(root)
        for block in blocks for root in block
    }
    monkeypatch.setattr(
        campaign, "validate_w16_output_v6",
        lambda root: results[Path(root).resolve()],
    )

    accepted = campaign.validate_w16_cpu_campaign(blocks)
    assert accepted["verdict"] == "PASS", accepted
    assert accepted["campaign_id"] == CAMPAIGN_ID
    assert accepted["direction_gate"]["status"] == "OBSERVED_NEGATIVE_OR_MIXED"
    assert accepted["claim_eligible"] is False
    assert accepted["technical_improvement_gate_passed"] is False
    assert accepted["thesis_result_eligible"] is False
    assert accepted["figure_eligible"] is False

    incomplete = campaign.validate_w16_cpu_campaign(blocks[:-1])
    assert incomplete["verdict"] == "INCOMPLETE"
    assert incomplete["thesis_result_eligible"] is False
    assert incomplete["figure_eligible"] is False


def test_campaign_positive_gate_requires_direct_and_adjusted_effects_per_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = _campaign_module()
    blocks = [
        [_campaign_root(tmp_path / f"block-{index}", order=(FORWARD if index % 2 else REVERSE),
                        ordinal=ordinal, block_index=index)
         for ordinal in range(1, 5)]
        for index in range(1, 7)
    ]
    results = {
        root.resolve(): _mock_v6_result(root)
        for block in blocks for root in block
    }
    # Five blocks satisfy both B-X/A-X > 1 and d > 0; block five fails both.
    for block_index, block in enumerate(blocks, 1):
        positive = block_index != 5
        rates = {
            "slow-roots:homogeneous": 1_000_000,
            "fast-roots:homogeneous": 1_000_000,
            "slow-roots:heterogeneous": 800_000 if positive else 1_000_000,
            "fast-roots:heterogeneous": 1_200_000 if positive else 900_000,
        }
        for root in block:
            result = results[root.resolve()]
            label = f"{result['arm']}:{result['quota']['mode']}"
            result["throughput"]["transaction_count"] = rates[label]
            result["throughput"]["throughput_milli_tps"] = rates[label]
    monkeypatch.setattr(
        campaign, "validate_w16_output_v6",
        lambda root: results[Path(root).resolve()],
    )

    result = campaign.validate_w16_cpu_campaign(blocks)
    assert result["verdict"] == "PASS", result
    assert result["direction_gate"]["status"] == "POSITIVE"
    assert result["direction_gate"]["positive_block_count"] == 5
    assert result["technical_improvement_gate_passed"] is True
    assert result["claim_eligible"] is False
    assert result["thesis_result_eligible"] is False
    assert result["figure_eligible"] is False


def test_campaign_uses_exact_fraction_gate_not_rounded_geometric_mean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = _campaign_module()
    blocks = [
        [_campaign_root(tmp_path / f"block-{index}", order=(FORWARD if index % 2 else REVERSE),
                        ordinal=ordinal, block_index=index)
         for ordinal in range(1, 5)]
        for index in range(1, 7)
    ]
    results = {
        root.resolve(): _mock_v6_result(root)
        for block in blocks for root in block
    }
    baseline = 10_000_000_000_000_000
    # Each direct and adjusted ratio is positive but exactly below 1.10.  A
    # float display may round it to 1.1; the product of exact fractions cannot.
    just_below_ten_percent = 11_000_000_000_000_000 - 1
    for result in results.values():
        label = f"{result['arm']}:{result['quota']['mode']}"
        count = (
            just_below_ten_percent if label == "fast-roots:heterogeneous"
            else baseline
        )
        result["throughput"]["transaction_count"] = count
        result["throughput"]["throughput_milli_tps"] = count
    monkeypatch.setattr(
        campaign, "validate_w16_output_v6",
        lambda root: results[Path(root).resolve()],
    )

    result = campaign.validate_w16_cpu_campaign(blocks)
    assert result["verdict"] == "PASS", result
    assert result["direction_gate"]["positive_block_count"] == 6
    assert result["direction_gate"]["status"] == "OBSERVED_NEGATIVE_OR_MIXED"
    assert result["technical_improvement_gate_passed"] is False


def test_campaign_rejects_duplicated_run_id_and_overlapping_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = _campaign_module()
    blocks = [
        [_campaign_root(tmp_path / f"block-{index}", order=(FORWARD if index % 2 else REVERSE),
                        ordinal=ordinal, block_index=index)
         for ordinal in range(1, 5)]
        for index in range(1, 7)
    ]
    results = {
        root.resolve(): _mock_v6_result(root)
        for block in blocks for root in block
    }
    monkeypatch.setattr(
        campaign, "validate_w16_output_v6",
        lambda root: results[Path(root).resolve()],
    )
    first = blocks[0][0]
    second = blocks[1][0]
    second_receipt_path = second / "feasibility-receipt.json"
    second_receipt = json.loads(second_receipt_path.read_text())
    first_receipt = json.loads((first / "feasibility-receipt.json").read_text())
    second_receipt["run_id"] = first_receipt["run_id"]
    second_receipt["started_monotonic_ns"] = first_receipt["started_monotonic_ns"]
    second_receipt["ended_monotonic_ns"] = first_receipt["ended_monotonic_ns"]
    _write_json(second_receipt_path, second_receipt)
    _reseal(second)
    results[second.resolve()] = _mock_v6_result(second)

    result = campaign.validate_w16_cpu_campaign(blocks)
    assert result["verdict"] != "PASS", result
