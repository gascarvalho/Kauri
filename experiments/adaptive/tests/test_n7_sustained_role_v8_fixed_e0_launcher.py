from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "n7-path-timeout-quorum" / "sustained_role_v8_fixed_e0_launcher.py"
SPEC = importlib.util.spec_from_file_location("n7_sustained_role_v8_fixed_e0_launcher", MODULE)
assert SPEC and SPEC.loader
subject = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = subject
SPEC.loader.exec_module(subject)


RUN = "fixed-v8"
PROFILE = "a" * 64
E0 = "b" * 64
START = 10_000_000_000
END = START + 82_000_000_000


def _argv() -> list[str]:
    return ["/archive/adaptation-manager", "--structured-event-run-id", RUN,
            "--scheduled-fixed-e0-control", "--scheduled-fixed-e0-run-id", RUN,
            "--scheduled-fixed-e0-profile-sha256", PROFILE,
            "--scheduled-fixed-e0-epoch-zero-digest", E0,
            "--scheduled-fixed-e0-window-start-monotonic-ns", str(START),
            "--scheduled-fixed-e0-window-end-monotonic-ns", str(END)]


def _contract(**changes: object) -> dict[str, object]:
    values = {"scheduled_start_monotonic_ns": START, "scheduled_end_monotonic_ns": END,
              "anchor_monotonic_ns": START + 10_000_000_000,
              "manager_argv": _argv(), "run_id": RUN, "profile_sha256": PROFILE,
              "e0_digest": E0}
    values.update(changes)
    return subject.prepare_no_launch_control_contract(**values)


def test_fixed_v8_contract_requires_shared_physical_horizon_and_no_successor() -> None:
    anchor = 20_000_000_000
    value = _contract()
    assert value["arm"] == "fixed_e0"
    assert value["deadlines"]["measurement_start_ns"] == anchor + 32_000_000_000
    assert value["deadlines"]["measurement_end_ns"] == anchor + 72_000_000_000
    assert value["manager_mode"] == "native_scheduled_fixed_e0_control"


def test_fixed_v8_contract_rejects_short_or_adaptive_control() -> None:
    with pytest.raises(subject.V8FixedControlError, match="ends before"):
        _contract(scheduled_end_monotonic_ns=START + 71_000_000_000,
                  anchor_monotonic_ns=START)
    with pytest.raises(subject.V8FixedControlError, match="may not request"):
        _contract(manager_argv=_argv() + ["--transition-request", "x"])
    with pytest.raises(subject.V8FixedControlError, match="first-omission"):
        _contract(anchor_monotonic_ns=START + 10_000_000_001)


@pytest.mark.parametrize("mutation, error", [
    ("fault_arm", "conflicts"), ("wrong_run", "binding"),
    ("wrong_digest", "binding"), ("wrong_window", "binding"),
    ("missing_mode", "lacks native"),
])
def test_fixed_v8_contract_rejects_native_binding_drift(mutation: str, error: str) -> None:
    argv = _argv()
    if mutation == "fault_arm":
        argv.append("--fault-window-arm-control-only")
    elif mutation == "wrong_run":
        argv[argv.index("--scheduled-fixed-e0-run-id") + 1] = "other"
    elif mutation == "wrong_digest":
        argv[argv.index("--scheduled-fixed-e0-epoch-zero-digest") + 1] = "c" * 64
    elif mutation == "wrong_window":
        argv[argv.index("--scheduled-fixed-e0-window-end-monotonic-ns") + 1] = str(END + 1)
    else:
        argv.remove("--scheduled-fixed-e0-control")
    with pytest.raises(subject.V8FixedControlError, match=error):
        _contract(manager_argv=argv)
