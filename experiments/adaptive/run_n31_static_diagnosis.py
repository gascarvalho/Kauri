#!/usr/bin/env python3
"""Preflight, run, or validate one frozen N=31 signer-aware pilot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

if __package__:
    from .kauri_experiment import profiled_fault_runtime
    from .kauri_experiment.n31_static_diagnosis import (
        ARM_NAMES,
        N31StaticDiagnosisError,
    )
    from .kauri_experiment import n31_static_diagnosis_runtime as runtime
    from .kauri_experiment.profiled_fault_evaluation import (
        ProfiledFaultEvaluationError,
    )
else:
    from kauri_experiment import profiled_fault_runtime
    from kauri_experiment.n31_static_diagnosis import (
        ARM_NAMES,
        N31StaticDiagnosisError,
    )
    from kauri_experiment import n31_static_diagnosis_runtime as runtime
    from kauri_experiment.profiled_fault_evaluation import (
        ProfiledFaultEvaluationError,
    )


REPOSITORY = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE = (
    REPOSITORY
    / "experiments"
    / "adaptive"
    / "profiles"
    / "n31-f5-static-diagnosis-v1.json"
)
DEFAULT_RESULTS_ROOT = REPOSITORY / "results" / "n31-f5-signer-aware-diagnosis-v1"


def _arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "run", "validate"))
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--arm", choices=ARM_NAMES)
    parser.add_argument("--run-directory", type=Path)
    parser.add_argument(
        "--trusted-provenance",
        type=Path,
        help="external receipt output for preflight/run; required input for validate",
    )
    parser.add_argument("--repository", type=Path, default=REPOSITORY)
    parser.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS_ROOT)
    parser.add_argument(
        "--build-directory", type=Path, default=REPOSITORY / "build-adaptive"
    )
    parser.add_argument(
        "--app-binary",
        type=Path,
        default=REPOSITORY / "build-adaptive/examples/hotstuff-app",
    )
    parser.add_argument(
        "--manager-binary",
        type=Path,
        default=REPOSITORY / "build-adaptive/examples/adaptation-manager",
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
        default=REPOSITORY / "build-adaptive/examples/epoch-profile-digest",
    )
    return parser.parse_args(argv)


def _emit(value: object, *, error: bool = False) -> None:
    print(
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True),
        file=sys.stderr if error else sys.stdout,
    )


def _runtime_arguments(args: argparse.Namespace) -> dict[str, Path]:
    build_directory = args.build_directory.resolve()
    return {
        "diagnosis_profile_path": args.profile.resolve(),
        "repository": args.repository.resolve(),
        "app_binary": args.app_binary.resolve(),
        "manager_binary": args.manager_binary.resolve(),
        "keygen_binary": args.keygen_binary.resolve(),
        "tls_keygen_binary": args.tls_keygen_binary.resolve(),
        "epoch_profile_digest_binary": args.epoch_profile_digest_binary.resolve(),
        "build_directory": build_directory,
        "build_provenance_path": (
            build_directory / profiled_fault_runtime.BUILD_PROVENANCE_FILENAME
        ),
    }


def _trusted_receipt_path(
    args: argparse.Namespace,
    *,
    run_directory: Path | None = None,
) -> Path:
    if args.trusted_provenance is None:
        raise runtime.N31StaticDiagnosisRuntimeError(
            "all commands require --trusted-provenance outside the run directory"
        )
    path = args.trusted_provenance.resolve()
    forbidden = (
        run_directory.resolve()
        if run_directory is not None
        else args.results_root.resolve()
    )
    try:
        path.relative_to(forbidden)
    except ValueError:
        return path
    raise runtime.N31StaticDiagnosisRuntimeError(
        "trusted provenance receipt must remain outside result/run directories"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    try:
        if args.command == "validate":
            if args.run_directory is None:
                raise runtime.N31StaticDiagnosisRuntimeError(
                    "validate requires --run-directory"
                )
            trusted_path = _trusted_receipt_path(
                args,
                run_directory=args.run_directory,
            )
            trusted = runtime.load_trusted_provenance(trusted_path)
            result = runtime.validate_preserved_run(
                args.run_directory.resolve(),
                trusted_provenance=trusted,
            )
            _emit(result)
            return 0

        trusted_path = _trusted_receipt_path(args)
        if args.command == "run" and args.arm is None:
            raise runtime.N31StaticDiagnosisRuntimeError("run requires --arm")
        arguments = _runtime_arguments(args)
        profiled_fault_runtime.prepare_exact_revision_build(
            repository=arguments["repository"],
            build_directory=arguments["build_directory"],
        )
        trusted = runtime.derive_trusted_provenance(
            repository=arguments["repository"],
            app_binary=arguments["app_binary"],
            manager_binary=arguments["manager_binary"],
            keygen_binary=arguments["keygen_binary"],
            tls_keygen_binary=arguments["tls_keygen_binary"],
            epoch_profile_digest_binary=arguments["epoch_profile_digest_binary"],
            build_directory=arguments["build_directory"],
            build_provenance_path=arguments["build_provenance_path"],
        )
        if args.command == "preflight":
            result = runtime.preflight(**arguments)
            if result.get("verdict") != "PASS":
                raise runtime.N31StaticDiagnosisRuntimeError(
                    "preflight returned an unsupported verdict"
                )
            receipt_sha256 = runtime.write_trusted_provenance(
                trusted_path,
                trusted,
            )
            _emit(
                {
                    **result,
                    "trusted_provenance_path": str(trusted_path),
                    "trusted_provenance_sha256": receipt_sha256,
                }
            )
            return 0

        receipt_sha256 = runtime.write_trusted_provenance(
            trusted_path,
            trusted,
        )
        run_directory, verdict = runtime.run_once(
            **arguments,
            arm=args.arm,
            trusted_provenance=trusted,
            results_root=args.results_root.resolve(),
        )
        if verdict not in {"PASS", "FAIL", "INCOMPLETE"}:
            raise runtime.N31StaticDiagnosisRuntimeError(
                "run returned an unsupported verdict"
            )
        _emit(
            {
                "run_directory": str(run_directory),
                "verdict": verdict,
                "trusted_provenance_path": str(trusted_path),
                "trusted_provenance_sha256": receipt_sha256,
            }
        )
        return 0 if verdict == "PASS" else 1
    except (
        OSError,
        ValueError,
        N31StaticDiagnosisError,
        ProfiledFaultEvaluationError,
        profiled_fault_runtime.ProfiledFaultRuntimeError,
        runtime.N31StaticDiagnosisRuntimeError,
    ) as error:
        _emit({"error": str(error), "verdict": "REJECT"}, error=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
