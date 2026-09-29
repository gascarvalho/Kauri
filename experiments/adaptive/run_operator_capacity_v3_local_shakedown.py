#!/usr/bin/env python3
"""Prepare, but do not implicitly run, one excluded local W18 N31 arm."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

try:
    from .kauri_experiment import cpu_quota
    from .kauri_experiment import operator_capacity_v3_local_runner as runner
except ImportError:
    from kauri_experiment import cpu_quota
    from kauri_experiment import operator_capacity_v3_local_runner as runner


def _json(path: Path) -> object:
    return json.loads(path.read_text(encoding="ascii"))


def _revision() -> str:
    return subprocess.run(("git", "rev-parse", "HEAD"), check=True,
                          capture_output=True, text=True).stdout.strip()


def _clean() -> bool:
    return not subprocess.run(("git", "status", "--porcelain"), check=True,
                              capture_output=True, text=True).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--materialization-root", type=Path, required=True)
    parser.add_argument("--manager-argv", type=Path, required=True)
    parser.add_argument("--replica-argv", type=Path, required=True)
    parser.add_argument("--quota-profile", type=Path, required=True)
    parser.add_argument("--request-output", type=Path, required=True)
    parser.add_argument("--hard-timeout-s", type=int, default=1200)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--authorization-receipt", type=Path)
    parser.add_argument("--tool-identity-approval", type=Path)
    parser.add_argument("--quota-contract", type=Path)
    parser.add_argument("--base-profile", type=Path)
    parser.add_argument("--stage-a-preflight", type=Path)
    parser.add_argument("--stage-a-request", type=Path)
    parser.add_argument("--stage-a-approval", type=Path)
    parser.add_argument("--stage-a-expected-approval-sha256")
    parser.add_argument("--native-stage-a-receipt", type=Path)
    parser.add_argument("--epoch0-tree", type=Path)
    parser.add_argument("--capacity-snapshot", type=Path)
    parser.add_argument("--stage-a-envelope", type=Path)
    parser.add_argument("--binary-adaptation-manager", type=Path)
    parser.add_argument("--binary-hotstuff-app", type=Path)
    parser.add_argument("--binary-identity-parity-verifier", type=Path)
    parser.add_argument("--binary-keygen", type=Path)
    parser.add_argument("--binary-tls-keygen", type=Path)
    parser.add_argument("--binary-capacity-digest", type=Path)
    parser.add_argument("--binary-epoch0-digest", type=Path)
    parser.add_argument("--binary-stage-a-envelope-signer", type=Path)
    parser.add_argument("--binary-stage-a-envelope-verifier", type=Path)
    parser.add_argument("--binary-stage-b-authorization-verifier", type=Path)
    args = parser.parse_args(argv)
    try:
        manager = _json(args.manager_argv)
        replicas = _json(args.replica_argv)
        if not isinstance(manager, list) or not all(isinstance(item, str) for item in manager):
            raise runner.OperatorCapacityV3LocalRunnerError("manager argv JSON is invalid")
        if (not isinstance(replicas, list) or any(not isinstance(row, list) or
                not all(isinstance(item, str) for item in row) for row in replicas)):
            raise runner.OperatorCapacityV3LocalRunnerError("replica argv JSON is invalid")
        from kauri_experiment import operator_capacity_v3_backend as backend
        plan = backend.prepare_no_launch_backend(
            materialization_root=args.materialization_root, manager_argv=manager,
            replica_argv=replicas, quota_profile=args.quota_profile)
        request = runner.build_execution_request(
            plan, materialization_root=args.materialization_root,
            timeout_s=args.hard_timeout_s)
        with args.request_output.open("xb") as output:
            output.write(request)
        if not args.execute:
            print(json.dumps({"verdict": "PREPARED_NO_EXECUTION",
                              "request_sha256": runner._sha(request)}, sort_keys=True))
            return 0
        required = (
            args.authorization_receipt, args.tool_identity_approval,
            args.quota_contract, args.base_profile, args.stage_a_preflight,
            args.stage_a_request, args.stage_a_approval,
            args.stage_a_expected_approval_sha256, args.native_stage_a_receipt,
            args.epoch0_tree, args.capacity_snapshot, args.stage_a_envelope,
            args.binary_adaptation_manager, args.binary_hotstuff_app,
            args.binary_identity_parity_verifier, args.binary_keygen,
            args.binary_tls_keygen, args.binary_capacity_digest,
            args.binary_epoch0_digest, args.binary_stage_a_envelope_signer,
            args.binary_stage_a_envelope_verifier,
            args.binary_stage_b_authorization_verifier,
        )
        if not all(required):
            raise runner.OperatorCapacityV3LocalRunnerError(
                "--execute requires complete external authorization and Stage-A authority inputs")
        receipt = _json(args.authorization_receipt)
        if not isinstance(receipt, dict):
            raise runner.OperatorCapacityV3LocalRunnerError("authorization receipt is invalid")
        contract = cpu_quota.load_cpu_quota_contract(
            args.quota_contract, base_profile_path=args.base_profile,
            expected_replica_ids=tuple(range(31)))
        pre_spawn_authority = {
            "preflight": args.stage_a_preflight,
            "request": args.stage_a_request,
            "approval": args.stage_a_approval,
            "expected_approval_sha256": args.stage_a_expected_approval_sha256,
            "native_receipt": args.native_stage_a_receipt,
            "tool_approval": args.tool_identity_approval,
            "epoch0_tree": args.epoch0_tree,
            "snapshot": args.capacity_snapshot,
            "envelope": args.stage_a_envelope,
            "quota_profile": args.quota_profile,
            "binaries": {
                "adaptation_manager": args.binary_adaptation_manager,
                "hotstuff_app": args.binary_hotstuff_app,
                "identity_parity_verifier": args.binary_identity_parity_verifier,
                "keygen": args.binary_keygen,
                "tls_keygen": args.binary_tls_keygen,
                "capacity_digest": args.binary_capacity_digest,
                "epoch0_digest": args.binary_epoch0_digest,
                "stage_a_envelope_signer": args.binary_stage_a_envelope_signer,
                "stage_a_envelope_verifier": args.binary_stage_a_envelope_verifier,
                "stage_b_authorization_verifier": args.binary_stage_b_authorization_verifier,
            },
        }
        # No lifecycle object exists before this call.  It reopens the exact
        # authorization and Stage-A chain, rehashes the executable bytes, and
        # proves the logs/runtime roots are still fresh.
        runner.verify_execution_admission(
            plan=plan, materialization_root=args.materialization_root,
            manager_argv=manager, replica_argv=replicas,
            authorization_request=request, authorization_receipt=receipt,
            tool_identity_approval_path=args.tool_identity_approval,
            pre_spawn_authority=pre_spawn_authority, current_revision=_revision,
            worktree_clean=_clean, quota_contract=contract,
            timeout_s=args.hard_timeout_s,
        )
        result = runner.execute_excluded_local_shakedown(
            materialization_root=args.materialization_root, manager_argv=manager,
            replica_argv=replicas, quota_profile=args.quota_profile,
            authorization_request=request, authorization_receipt=receipt,
            tool_identity_approval_path=args.tool_identity_approval,
            execute=True, timeout_s=args.hard_timeout_s, current_revision=_revision,
            worktree_clean=_clean,
            lifecycle_factory=lambda: runner.CpuQuotaLocalLifecycle(
                contract=contract, run_id=f"w18-{runner._sha(request)[:16]}",
                root=args.materialization_root),
            pre_spawn_authority=pre_spawn_authority, quota_contract=contract)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["verdict"] == "PROCESS_COMPLETED_PENDING_RAW_VALIDATION" else 1
    except (OSError, ValueError, subprocess.SubprocessError,
            runner.OperatorCapacityV3LocalRunnerError, cpu_quota.CpuQuotaContractError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
