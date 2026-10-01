import importlib.util
from pathlib import Path
import sys

import pytest

HERE = Path(__file__).resolve().parents[1] / "n7-path-timeout-quorum"
sys.path.insert(0, str(HERE))
try:
    spec = importlib.util.spec_from_file_location("w19_v8_session_test", HERE / "sustained_role_v8_session.py")
    subject = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(subject)
finally:
    sys.path.remove(str(HERE))


def cells(ratios):
    rows = []
    for pair, order in enumerate(subject.design.FROZEN_PAIR_SCHEDULE):
        for arm in order:
            count = 100 if arm == "fixed_e0" else ratios[pair]
            rows.append({"run_id": f"cell{len(rows) + 1}", "arm": arm,
                         "result": {"common_committed_blocks": count}})
    return rows


def test_original_gate_requires_ten_percent_per_positive_pair():
    result = subject.paired_result(cells([109] * 6))
    assert result["verdict"] == "VALIDATED_HYPOTHESIS_REJECTED"
    assert result["positive_pairs"] == 0


def test_five_pairs_and_both_orders_and_aggregate_must_pass():
    result = subject.paired_result(cells([120, 120, 120, 120, 120, 100]))
    assert result["verdict"] == "VALIDATED_IMPROVEMENT"
    assert result["positive_pairs"] == 5


def test_five_positive_pairs_cannot_hide_aggregate_rejection():
    result = subject.paired_result(cells([110, 110, 110, 110, 110, 1]))
    assert result["positive_pairs"] == 5
    assert result["verdict"] == "VALIDATED_HYPOTHESIS_REJECTED"


def test_negative_campaign_is_retained_as_valid_negative_result():
    result = subject.paired_result(cells([90] * 6))
    assert result["verdict"] == "VALIDATED_HYPOTHESIS_REJECTED"
    assert result["aggregate_ratio"] == .9


def test_campaign_rejects_reused_cell_or_changed_order():
    rows = cells([120] * 6)
    rows[-1]["run_id"] = rows[0]["run_id"]
    with pytest.raises(subject.LiveError, match="unique"):
        subject.paired_result(rows)
    rows = cells([120] * 6); rows[0], rows[1] = rows[1], rows[0]
    with pytest.raises(subject.LiveError, match="order"):
        subject.paired_result(rows)


def test_zero_fixed_denominator_is_never_an_improvement():
    rows = cells([120] * 6)
    rows[0]["result"]["common_committed_blocks"] = 0
    with pytest.raises(subject.LiveError, match="invalid"):
        subject.paired_result(rows)
