"""No-launch control contract for the prospective W19 v8 study.

The control arm must share the same physical fault exposure and A+32..A+72
metric window as adaptive v8, while never asking the manager to construct a
successor epoch. The native scheduled-fixed-E0 mode has its own complete
binding and rejects fault-window-arm mode. This module only checks a prepared
command; it cannot materialize or launch a process.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Mapping, Sequence


HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("w19_v8_profile_fixed", HERE / "sustained_role_v8_profile.py")
assert _SPEC and _SPEC.loader
profile = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(profile)


class V8FixedControlError(ValueError):
    pass


def _one_value(argv: tuple[str, ...], option: str) -> str:
    if argv.count(option) != 1:
        raise V8FixedControlError(f"control manager argv lacks one {option}")
    position = argv.index(option)
    if position + 1 >= len(argv) or not argv[position + 1].strip():
        raise V8FixedControlError(f"control manager argv lacks a value for {option}")
    return argv[position + 1]


def _hex64(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def prepare_no_launch_control_contract(*, scheduled_start_monotonic_ns: int,
                                       scheduled_end_monotonic_ns: int,
                                       anchor_monotonic_ns: int,
                                       manager_argv: Sequence[str], run_id: str,
                                       profile_sha256: str, e0_digest: str) -> dict[str, Any]:
    """Validate the prospective control envelope without materializing or spawning.

    The anchor remains a runtime fact.  It is accepted here solely to check
    that a future native fault event can fit inside the frozen physical window;
    a production launcher must derive it from that sealed event instead of
    taking it from an operator.
    """
    if (type(scheduled_start_monotonic_ns) is not int or type(scheduled_end_monotonic_ns) is not int or
            scheduled_start_monotonic_ns < 0 or scheduled_end_monotonic_ns <= scheduled_start_monotonic_ns):
        raise V8FixedControlError("scheduled control window is invalid")
    if (type(anchor_monotonic_ns) is not int or anchor_monotonic_ns < scheduled_start_monotonic_ns or
            anchor_monotonic_ns > scheduled_start_monotonic_ns + 10_000_000_000):
        raise V8FixedControlError("control anchor is outside the frozen first-omission interval")
    deadlines = profile.deadlines(anchor_monotonic_ns)
    if scheduled_end_monotonic_ns < deadlines["horizon_ns"]:
        raise V8FixedControlError("control physical window ends before A+72 seconds")
    if scheduled_end_monotonic_ns - scheduled_start_monotonic_ns < profile.SCHEDULED_START_TO_HORIZON_NS:
        raise V8FixedControlError("control window is shorter than the frozen 82-second exposure")
    if (not isinstance(manager_argv, Sequence) or isinstance(manager_argv, (str, bytes)) or
            not all(isinstance(value, str) and value for value in manager_argv)):
        raise V8FixedControlError("control manager argv is malformed")
    argv = tuple(manager_argv)
    if (not isinstance(run_id, str) or not run_id or not isinstance(profile_sha256, str) or
            not isinstance(e0_digest, str) or not _hex64(profile_sha256) or not _hex64(e0_digest)):
        raise V8FixedControlError("control identity is incomplete")
    if any(option in argv for option in ("--transition-request", "--bundle-output", "--epoch-change-request")):
        raise V8FixedControlError("control manager argv may not request a successor epoch")
    if any(option == "--fault-window-arm-control-only" or option.startswith("--fault-window-arm-") for option in argv):
        raise V8FixedControlError("scheduled control conflicts with native fault-window arm")
    if argv.count("--scheduled-fixed-e0-control") != 1:
        raise V8FixedControlError("control manager argv lacks native scheduled-fixed-E0 mode")
    expected = {
        "--structured-event-run-id": run_id,
        "--scheduled-fixed-e0-run-id": run_id,
        "--scheduled-fixed-e0-profile-sha256": profile_sha256,
        "--scheduled-fixed-e0-epoch-zero-digest": e0_digest,
        "--scheduled-fixed-e0-window-start-monotonic-ns": str(scheduled_start_monotonic_ns),
        "--scheduled-fixed-e0-window-end-monotonic-ns": str(scheduled_end_monotonic_ns),
    }
    if any(_one_value(argv, option) != value for option, value in expected.items()):
        raise V8FixedControlError("native scheduled-fixed-E0 binding differs from sealed inputs")
    return {
        "state": "PREPARED_NO_LAUNCH_EXTERNAL_APPROVAL_REQUIRED",
        "arm": "fixed_e0", "profile": dict(profile.FROZEN_PROFILE),
        "clock": "CLOCK_MONOTONIC_RAW", "scheduled_window": {
            "start_monotonic_ns": scheduled_start_monotonic_ns,
            "end_monotonic_ns": scheduled_end_monotonic_ns,
        }, "anchor_monotonic_ns": anchor_monotonic_ns, "deadlines": deadlines,
        "manager_mode": "native_scheduled_fixed_e0_control",
        "claim_eligible": False, "figure_eligible": False,
    }
