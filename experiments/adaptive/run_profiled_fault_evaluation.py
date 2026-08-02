#!/usr/bin/env python3
"""Preflight or run one frozen N=31 fault-evaluation attempt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

if __package__:
    from .kauri_experiment import profiled_fault_runtime as runtime
    from .kauri_experiment.profiled_fault_evaluation import (
        ProfiledFaultEvaluationError,
        load_frozen_profile,
    )
else:
    from kauri_experiment import profiled_fault_runtime as runtime
    from kauri_experiment.profiled_fault_evaluation import (
        ProfiledFaultEvaluationError,
        load_frozen_profile,
    )


REPOSITORY = Path(__file__).resolve().parents[2]
DEFAULT_RESULTS_ROOT = REPOSITORY / "results" / "n31-f5-crash-shakedown-v1"
EXPECTED_PROFILE_ID = "n31-f5-q21-sigkill-shakedown-v1"
EXPECTED_PROFILE_SHA256 = (
    "2ce1bcc8e8f6af3201710d34b23cd66c70d05a7658b7ec737e35b5a35e73bcfa"
)
EXPECTED_PROFILES = runtime.SHIPPED_PROFILES


def _verify_shipped_profile(profile_path: Path) -> None:
    profile = load_frozen_profile(profile_path)
    if EXPECTED_PROFILES.get(profile.profile_id) != profile.profile_sha256:
        raise runtime.ProfiledFaultRuntimeError(
            "profile ID and SHA-256 do not match a shipped N31 profile"
        )


def _arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "run", "validate"))
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--run-directory", type=Path)
    parser.add_argument("--repository", type=Path, default=REPOSITORY)
    parser.add_argument(
        "--results-root",
        type=Path,
        default=DEFAULT_RESULTS_ROOT,
    )
    parser.add_argument(
        "--build-directory",
        type=Path,
        default=REPOSITORY / "build-adaptive",
    )
    parser.add_argument(
        "--app-binary",
        type=Path,
        default=REPOSITORY / "build-adaptive/examples/hotstuff-app",
    )
    parser.add_argument(
        "--manager-binary",
        type=Path,
        default=(REPOSITORY / "build-adaptive/examples/adaptation-manager"),
    )
    parser.add_argument(
        "--keygen-binary",
        type=Path,
        default=REPOSITORY / "build-adaptive/hotstuff-keygen",
    )
    parser.add_argument(
        "--tls-keygen-binary",
        type=Path,
        default=REPOSITORY / "build-adaptive/hotstuff-tls-keygen",
    )
    parser.add_argument(
        "--epoch-profile-digest-binary",
        type=Path,
        default=(REPOSITORY / "build-adaptive/examples/epoch-profile-digest"),
    )
    return parser.parse_args(argv)


def _runtime_arguments(args: argparse.Namespace) -> dict[str, Path]:
    build_directory = args.build_directory.resolve()
    return {
        "profile_path": args.profile.resolve(),
        "repository": args.repository.resolve(),
        "app_binary": args.app_binary.resolve(),
        "manager_binary": args.manager_binary.resolve(),
        "keygen_binary": args.keygen_binary.resolve(),
        "tls_keygen_binary": args.tls_keygen_binary.resolve(),
        "epoch_profile_digest_binary": (args.epoch_profile_digest_binary.resolve()),
        "build_directory": build_directory,
        "build_provenance_path": (build_directory / runtime.BUILD_PROVENANCE_FILENAME),
    }


def _emit(value: object, *, error: bool = False) -> None:
    print(
        json.dumps(value, separators=(",", ":"), sort_keys=True),
        file=sys.stderr if error else sys.stdout,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    try:
        if args.command == "validate":
            if args.run_directory is None:
                raise runtime.ProfiledFaultRuntimeError(
                    "validate requires --run-directory"
                )
            result = runtime.validate_preserved_run(args.run_directory.resolve())
            _emit(result)
            return 0
        if args.profile is None:
            raise runtime.ProfiledFaultRuntimeError(
                f"{args.command} requires --profile"
            )
        runtime_arguments = _runtime_arguments(args)
        _verify_shipped_profile(runtime_arguments["profile_path"])
        if args.command == "preflight":
            runtime.prepare_exact_revision_build(
                repository=runtime_arguments["repository"],
                build_directory=runtime_arguments["build_directory"],
            )
            result = runtime.preflight(**runtime_arguments)
            if result.get("verdict") == "PASS":
                _emit(result)
                return 0
            if result.get("verdict") == "REJECT":
                _emit(result, error=True)
                return 2
            raise runtime.ProfiledFaultRuntimeError(
                "preflight returned an unsupported verdict"
            )

        run_directory, verdict = runtime.run_once(
            **runtime_arguments,
            results_root=args.results_root.resolve(),
        )
        if verdict not in {"PASS", "FAIL", "INCOMPLETE"}:
            raise runtime.ProfiledFaultRuntimeError(
                "run returned an unsupported verdict"
            )
        _emit(
            {
                "run_directory": str(run_directory),
                "verdict": verdict,
            }
        )
        return 0 if verdict == "PASS" else 1
    except (
        OSError,
        ProfiledFaultEvaluationError,
        runtime.ProfiledFaultRuntimeError,
    ) as exc:
        _emit({"error": str(exc), "verdict": "REJECT"}, error=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
