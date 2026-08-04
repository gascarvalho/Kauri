#!/usr/bin/env python3
"""Preflight, execute, or validate the frozen three-arm N=31 PQAR pilot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

if __package__:
    from .kauri_experiment import n31_post_qc_audit_runtime as audit_runtime
    from .kauri_experiment import profiled_fault_runtime
    from .kauri_experiment.n31_post_qc_audit import N31PostQcAuditError
    from .kauri_experiment.profiled_fault_evaluation import (
        ProfiledFaultEvaluationError,
    )
else:
    from kauri_experiment import n31_post_qc_audit_runtime as audit_runtime
    from kauri_experiment import profiled_fault_runtime
    from kauri_experiment.n31_post_qc_audit import N31PostQcAuditError
    from kauri_experiment.profiled_fault_evaluation import (
        ProfiledFaultEvaluationError,
    )


REPOSITORY = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE = (
    REPOSITORY
    / "experiments"
    / "adaptive"
    / "profiles"
    / "n31-f5-post-qc-audit-v5.json"
)
DEFAULT_RESULTS_ROOT = REPOSITORY / "results" / "n31-f5-post-qc-audit-v5"


def _arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("preflight", "run", "validate", "validate-sequence")
    )
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--run-directory", type=Path)
    parser.add_argument(
        "--trusted-provenance",
        type=Path,
        required=True,
        help="external receipt path; it must remain outside all result directories",
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
        "audit_profile_path": args.profile.resolve(),
        "repository": args.repository.resolve(),
        "results_root": args.results_root.resolve(),
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


def _trusted_path(args: argparse.Namespace, *, forbidden: Path) -> Path:
    path = args.trusted_provenance.resolve()
    try:
        path.relative_to(forbidden.resolve())
    except ValueError:
        return path
    raise audit_runtime.N31PostQcAuditRuntimeError(
        "trusted provenance receipt must remain outside result/run directories"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    try:
        if args.command in {"validate", "validate-sequence"}:
            if args.run_directory is None:
                raise audit_runtime.N31PostQcAuditRuntimeError(
                    "validate requires --run-directory"
                )
            trusted_path = _trusted_path(args, forbidden=args.run_directory)
            trusted = audit_runtime.load_trusted_provenance(trusted_path)
            validator = (
                audit_runtime.validate_preserved_run
                if args.command == "validate"
                else audit_runtime.validate_pilot_sequence
            )
            result = validator(args.run_directory.resolve(), trusted_provenance=trusted)
            _emit(result)
            return 0 if result["verdict"] == "PASS" else 1

        if (
            args.command == "run"
            and args.results_root.resolve() != DEFAULT_RESULTS_ROOT.resolve()
        ):
            raise audit_runtime.N31PostQcAuditRuntimeError(
                "run requires the canonical frozen v5 results root: "
                f"{DEFAULT_RESULTS_ROOT.resolve()}"
            )
        values = _runtime_arguments(args)
        trusted_path = _trusted_path(args, forbidden=args.results_root)
        if args.command == "preflight":
            profiled_fault_runtime.prepare_exact_revision_build(
                repository=values["repository"],
                build_directory=values["build_directory"],
            )
            trusted = audit_runtime.derive_trusted_provenance(
                repository=values["repository"],
                app_binary=values["app_binary"],
                manager_binary=values["manager_binary"],
                keygen_binary=values["keygen_binary"],
                tls_keygen_binary=values["tls_keygen_binary"],
                epoch_profile_digest_binary=values["epoch_profile_digest_binary"],
                build_directory=values["build_directory"],
                build_provenance_path=values["build_provenance_path"],
            )
            result = audit_runtime.preflight(
                **{key: value for key, value in values.items() if key != "results_root"}
            )
            receipt_sha256 = audit_runtime.write_trusted_provenance(
                trusted_path, trusted
            )
            _emit(
                {
                    **result,
                    "trusted_provenance_path": str(trusted_path),
                    "trusted_provenance_sha256": receipt_sha256,
                }
            )
            return 0

        sequence, results, trusted = audit_runtime.prepare_and_run_pilot_sequence(
            **values,
            trusted_provenance_path=trusted_path,
        )
        _emit(
            {
                "sequence_directory": str(sequence),
                "results": list(results),
                "trusted_provenance_path": str(trusted_path),
                "trusted_provenance_sha256": trusted.sha256,
                "evidence_ceiling": "harness_validation_only",
                "figure_eligible": False,
            }
        )
        return 0 if all(result["verdict"] == "PASS" for result in results) else 1
    except (
        OSError,
        ValueError,
        N31PostQcAuditError,
        audit_runtime.N31PostQcAuditRuntimeError,
        ProfiledFaultEvaluationError,
        profiled_fault_runtime.ProfiledFaultRuntimeError,
    ) as error:
        _emit({"error": str(error), "verdict": "REJECT"}, error=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
