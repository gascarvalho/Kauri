"""Explicit, local-only W18 process-launch seam.

This module is deliberately not a campaign runner.  It may start exactly one
materialized N=31 arm only after an external authorization receipt is supplied,
and it never retries.  It retains a receipt for every post-admission outcome;
raw-result acceptance remains the independent validator's responsibility.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import time
from typing import Any, Callable, Mapping, Protocol, Sequence

from . import operator_capacity_v3_backend as backend
from . import cpu_quota
from . import operator_capacity_excluded_pair as excluded_pair
from . import operator_capacity_preflight
from . import operator_capacity_stage_a_preflight as stage_a_preflight
from .processes import ProcessRegistry


_KIND = "kauri-n31-operator-capacity-v3-local-execution-request-v1"
_AUTH_KIND = "kauri-n31-operator-capacity-v3-local-execution-authorization-v1"
_MAX_TIMEOUT_S = 20 * 60


class OperatorCapacityV3LocalRunnerError(RuntimeError):
    pass


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _fail(message: str) -> None:
    raise OperatorCapacityV3LocalRunnerError(message)


def _json_bytes(raw: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"{label} is not canonical ASCII JSON")
        raise AssertionError from exc
    if not isinstance(value, dict) or _canonical(value) != raw:
        _fail(f"{label} is not canonical JSON")
    return value


def _regular(path: Path, label: str) -> bytes:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        _fail(f"{label} is not a regular non-symlink file")
    try:
        return candidate.read_bytes()
    except OSError as exc:
        _fail(f"cannot read {label}")
        raise AssertionError from exc


def build_execution_request(plan: Mapping[str, object], *, materialization_root: Path, timeout_s: int) -> bytes:
    """Build approval inputs only; this function cannot authorize execution."""
    if type(timeout_s) is not int or not 1 <= timeout_s <= _MAX_TIMEOUT_S:
        _fail("local hard timeout must be an integer in 1..1200 seconds")
    expected = {
        "schema_version", "kind", "verdict", "claim_eligible", "figure_eligible",
        "launch_permitted", "materialization_manifest_sha256", "arm", "revision", "epoch0_tree",
        "automatic_retries", "binary_sha256", "quota_ownership", "stage_a", "stage_b",
        "cleanup_contract", "native_policy_order_repaired", "execution_blocker",
    }
    if set(plan) != expected or plan.get("verdict") != "BACKEND_PLAN_REVIEW_REQUIRED_NO_EXECUTION":
        _fail("backend plan is not the current no-launch W18 plan")
    if plan.get("launch_permitted") is not False or plan.get("automatic_retries") != 0:
        _fail("backend plan weakens no-launch or no-retry policy")
    if (plan.get("native_policy_order_repaired") is not True or
            plan.get("execution_blocker") != "EXTERNAL_AUTHORIZATION_AND_PRESPAWN_AUTHORITY_REQUIRED"):
        _fail("backend plan does not record the repaired native admission and runner boundary")
    root = Path(materialization_root).resolve()
    return _canonical({
        "schema_version": 1, "kind": _KIND, "backend_plan_sha256": _sha(_canonical(dict(plan))),
        "materialization_root": str(root), "materialization_manifest_sha256": plan["materialization_manifest_sha256"],
        "revision": plan["revision"], "arm": plan["arm"], "hard_timeout_s": timeout_s,
        "automatic_retries": 0, "environment": "local-only", "cluster_execution": False,
        "claim_eligible": False, "figure_eligible": False,
    })


def verify_external_authorization(request: bytes, receipt: Mapping[str, object]) -> dict[str, object]:
    """Accept only an externally created receipt bound byte-for-byte to request."""
    requested = _json_bytes(request, "execution request")
    expected_keys = {*requested, "request_sha256", "approval_reference", "approved_utc"}
    document = dict(receipt)
    if (set(document) != expected_keys or document.get("schema_version") != 1 or
            document.get("kind") != _AUTH_KIND or
            any(document.get(key) != value for key, value in requested.items()
                if key != "kind") or
            document.get("request_sha256") != _sha(request) or
            not isinstance(document.get("approval_reference"), str) or not document["approval_reference"] or
            not isinstance(document.get("approved_utc"), str) or not document["approved_utc"].endswith("Z")):
        _fail("external authorization is not bound to the exact local no-retry request")
    return document


def verify_tool_identity_before_spawn(
    path: Path, *, plan: Mapping[str, object], manager_argv: Sequence[str],
    replica_argv: Sequence[Sequence[str]],
) -> bytes:
    """Recheck the externally approved manager/app bytes at the launch edge."""
    raw = _regular(path, "external tool-identity approval")
    stage_a = plan.get("stage_a")
    if not isinstance(stage_a, dict) or _sha(raw) != stage_a.get("tool_identity_approval_receipt_sha256"):
        _fail("external tool-identity approval differs from the materialization pin")
    document = _json_bytes(raw, "external tool-identity approval")
    binaries = document.get("binary_sha256")
    expected = plan.get("binary_sha256")
    if (document.get("schema_version") != 1 or
            document.get("kind") != "kauri-n31-operator-capacity-tool-identity-approval-v1" or
            document.get("verdict") != "EXTERNAL_TOOL_IDENTITY_APPROVED" or
            document.get("revision") != plan.get("revision") or
            not isinstance(document.get("approval_ref"), str) or not document["approval_ref"] or
            not isinstance(document.get("approved_at_utc"), str) or not document["approved_at_utc"].endswith("Z") or
            not isinstance(binaries, dict) or not isinstance(expected, dict) or
            any(binaries.get(name) != digest for name, digest in expected.items())):
        _fail("external tool-identity approval does not bind the frozen binaries")
    if not manager_argv or not replica_argv or any(not argv for argv in replica_argv):
        _fail("manager or replica argv is absent at pre-spawn verification")
    if (_sha(_regular(Path(manager_argv[0]), "adaptation manager binary")) != expected["adaptation_manager"] or
            any(_sha(_regular(Path(argv[0]), "hotstuff app binary")) != expected["hotstuff_app"]
                for argv in replica_argv)):
        _fail("manager or hotstuff app bytes differ from external approval before spawn")
    return raw


def verify_pre_spawn_authority(
    authority: Mapping[str, object], *, plan: Mapping[str, object], root: Path,
    manager_argv: Sequence[str], replica_argv: Sequence[Sequence[str]],
) -> None:
    """Reopen every Stage-A authority source immediately before process spawn."""
    required = {"preflight", "request", "approval", "expected_approval_sha256",
                "native_receipt", "tool_approval", "epoch0_tree", "snapshot",
                "envelope", "quota_profile", "binaries"}
    if set(authority) != required:
        _fail("pre-spawn authority schema differs")
    arm = plan.get("arm")
    if arm not in {"sham", "treatment"}:
        _fail("backend plan arm is invalid")
    try:
        checked = excluded_pair._arm_authority(
            arm=arm, preflight_path=Path(str(authority["preflight"])),
            request_path=Path(str(authority["request"])),
            approval_path=Path(str(authority["approval"])),
            expected_approval_sha256=str(authority["expected_approval_sha256"]),
            expected_output_root=root,
        )
    except Exception as exc:
        _fail(f"pre-spawn Stage-A authority chain is invalid: {exc}")
    plan_binaries = plan.get("binary_sha256")
    if (checked["revision"] != plan.get("revision") or not isinstance(plan_binaries, Mapping) or
            any(checked["binary_sha256"].get(name) != digest
                for name, digest in plan_binaries.items())):
        _fail("pre-spawn authority does not match materialization revision or binaries")
    input_paths = {"epoch0_tree_file": "epoch0_tree", "capacity_snapshot_wire": "snapshot",
                   "stage_a_envelope_wire": "envelope", "quota_profile": "quota_profile"}
    for digest_name, path_name in input_paths.items():
        if _sha(_regular(Path(str(authority[path_name])), digest_name)) != checked["input_sha256"][digest_name]:
            _fail(f"pre-spawn {digest_name} bytes differ from Stage-A authority")
    if (Path(str(authority["epoch0_tree"])).resolve() != root / "config/epoch0.tree" or
            Path(str(authority["envelope"])).resolve() != root / "config/stage-a-envelope.wire"):
        _fail("pre-spawn Stage-A authority does not name the exact materialized inputs")
    native_raw = _regular(Path(str(authority["native_receipt"])), "native Stage-A receipt")
    native = _json_bytes(native_raw, "native Stage-A receipt")
    expected_native_fields = {
        "schema_version", "kind", "verdict", "envelope_wire_sha256",
        "envelope_canonical_digest", "approved_capacity_digest", "issuer_id",
        "issuer_reference", "issuer_public_key_fingerprint", "arm",
        "source_revision", "verification_monotonic_raw_ns",
        "epoch0_tree_file_sha256", "epoch0_consensus_digest",
        "epoch0_topology_digest",
    }
    epoch0_tree = plan.get("epoch0_tree")
    if (_sha(native_raw) != checked["native_verifier_receipt_sha256"] or
            set(native) != expected_native_fields or native.get("schema_version") != 1 or
            native.get("kind") != stage_a_preflight.VERIFIER_KIND or
            native.get("verdict") != stage_a_preflight.VERIFIER_VERDICT or
            native.get("source_revision") != plan.get("revision") or
            native.get("arm") != ("fast_priority_treatment" if arm == "treatment" else "exact_copy_sham") or
            native.get("envelope_wire_sha256") != checked["stage_a_envelope_sha256"] or
            native.get("epoch0_tree_file_sha256") != checked["input_sha256"]["epoch0_tree_file"] or
            not isinstance(epoch0_tree, Mapping) or
            native.get("epoch0_tree_file_sha256") != epoch0_tree.get("sha256") or
            native.get("epoch0_topology_digest") != epoch0_tree.get("topology_digest") or
            not isinstance(native.get("verification_monotonic_raw_ns"), int) or
            native["verification_monotonic_raw_ns"] <= 0):
        _fail("native Stage-A receipt is not the authority-bound verifier result")
    stage_a = plan.get("stage_a")
    if (not isinstance(stage_a, Mapping) or
            checked["stage_a_envelope_sha256"] != stage_a.get("envelope_sha256") or
            checked["native_verifier_receipt_sha256"] != stage_a.get("native_receipt_sha256")):
        _fail("pre-spawn Stage-A authority is not bound to the executable materialization plan")
    binaries = authority["binaries"]
    if not isinstance(binaries, Mapping) or set(binaries) != set(stage_a_preflight.REQUIRED_BINARIES):
        _fail("pre-spawn binary map is incomplete")
    for name, path in binaries.items():
        if _sha(_regular(Path(str(path)), f"binary {name}")) != checked["binary_sha256"][name]:
            _fail(f"pre-spawn binary {name} differs from Stage-A authority")
    # This repeats the manager/app rehash at the immediate edge after the full
    # authority chain is known valid.
    verify_tool_identity_before_spawn(
        Path(str(authority["tool_approval"])), plan=plan,
        manager_argv=manager_argv, replica_argv=replica_argv)


def verify_w18_cpu_contract(contract: cpu_quota.CpuQuotaContract) -> None:
    """Reject a coherent-but-different quota contract at the launch edge.

    ``load_cpu_quota_contract`` verifies that a contract agrees with whichever
    base profile it was given.  W18 additionally needs the frozen study
    profile: replicas 0..5 at 25%, replicas 6..30 at 100%, and no manager
    visibility.  Keep this check here so injected lifecycles cannot silently
    use a different all-live experiment.
    """
    expected_profile = operator_capacity_preflight._EXPECTED_QUOTA_PROFILE
    if (
        contract.contract_id != expected_profile["contract_id"]
        or contract.base_profile_id != expected_profile["base_profile_id"]
        or contract.base_profile_sha256 != expected_profile["base_profile_sha256"]
        or contract.base_profile_canonical_sha256
        != expected_profile["base_profile_canonical_sha256"]
        or contract.enabled is not True
        or contract.figure_eligible is not False
        or contract.launcher != expected_profile["launcher"]
        or contract.manager_visibility != "none"
        or contract.sampling_interval_ms != expected_profile["sampling_interval_ms"]
        or contract.replica_ids != tuple(range(31))
        or tuple(contract.capacity_class(replica) for replica in range(31))
        != ("slow",) * 6 + ("fast",) * 25
        or tuple(contract.quota_percent(replica) for replica in range(31))
        != (25,) * 6 + (100,) * 25
    ):
        _fail("CPU quota contract is not the frozen W18 25/100 base-profile binding")


def _verify_fresh_runtime(root: Path) -> tuple[Path, Path]:
    logs = root / "logs"
    runtime = root / "runtime"
    if logs.exists() or runtime.exists():
        _fail("local execution output directories must be fresh; retries and resume are forbidden")
    return logs, runtime


def verify_execution_admission(
    *, plan: Mapping[str, object], materialization_root: Path,
    manager_argv: Sequence[str], replica_argv: Sequence[Sequence[str]],
    authorization_request: bytes, authorization_receipt: Mapping[str, object],
    tool_identity_approval_path: Path, pre_spawn_authority: Mapping[str, object],
    current_revision: Callable[[], str], worktree_clean: Callable[[], bool],
    quota_contract: cpu_quota.CpuQuotaContract, timeout_s: int,
) -> bytes:
    """Complete every non-spawning admission check, including fresh outputs.

    The CLI calls this before constructing the systemd lifecycle.  The runner
    calls it again immediately before directory creation to make the direct
    Python entrypoint fail closed as well.
    """
    root = Path(materialization_root).resolve()
    expected_request = build_execution_request(
        plan, materialization_root=root, timeout_s=timeout_s,
    )
    if authorization_request != expected_request:
        _fail("execution request differs from the current frozen backend plan")
    verify_external_authorization(authorization_request, authorization_receipt)
    verify_pre_spawn_authority(pre_spawn_authority, plan=plan, root=root,
                               manager_argv=manager_argv, replica_argv=replica_argv)
    tool_approval = verify_tool_identity_before_spawn(
        tool_identity_approval_path, plan=plan, manager_argv=manager_argv,
        replica_argv=replica_argv)
    verify_w18_cpu_contract(quota_contract)
    if current_revision() != plan["revision"] or not worktree_clean():
        _fail("local execution requires a clean worktree at the pinned materialization revision")
    _verify_fresh_runtime(root)
    return tool_approval


class _Lifecycle(Protocol):
    def start_replica(self, replica_id: int, argv: Sequence[str], log: Path) -> None: ...
    def start_manager(self, argv: Sequence[str], log: Path) -> None: ...
    def wait_manager(self, deadline_monotonic: float) -> int: ...
    def stop_monitor(self) -> None: ...
    def terminate_manager_and_replicas(self) -> None: ...
    def terminate_owned_replica_scopes(self, deadline_monotonic: float) -> Mapping[str, object]: ...
    def verify_scope_cleanup(self, deadline_monotonic: float) -> Mapping[str, object]: ...


class CpuQuotaLocalLifecycle:
    """The real local process adapter; it is inert until the caller passes it.

    Replicas are wrapped by the proven per-replica systemd CPU-scope runtime.
    The manager deliberately remains outside those scopes, as required by the
    frozen contract.  This class does not decide whether execution is allowed.
    """

    def __init__(self, *, contract: cpu_quota.CpuQuotaContract, run_id: str, root: Path) -> None:
        self._root = Path(root)
        self._registry = ProcessRegistry()
        self._quota = cpu_quota.CpuQuotaRuntime(contract, run_id=run_id, run_directory=self._root)
        self._handles: list[Any] = []
        self._manager: subprocess.Popen[bytes] | None = None
        self._manager_log: Any = None
        self._monitor_started = False

    def start_replica(self, replica_id: int, argv: Sequence[str], log: Path) -> None:
        _record, handle = self._quota.spawn_owned_process(
            self._registry, name=f"replica-{replica_id}", replica_id=replica_id,
            command=tuple(argv), log_path=log, working_directory=self._root,
        )
        self._handles.append(handle)

    def start_manager(self, argv: Sequence[str], log: Path) -> None:
        if len(self._registry.records) != 31:
            _fail("all 31 replica scopes must exist before manager launch")
        self._quota.start_monitor()
        self._monitor_started = True
        self._manager_log = log.open("xb")
        self._manager = subprocess.Popen(
            tuple(argv), cwd=self._root, stdin=subprocess.DEVNULL,
            stdout=self._manager_log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def wait_manager(self, deadline_monotonic: float) -> int:
        if self._manager is None:
            _fail("manager was not launched")
        while time.monotonic() < deadline_monotonic:
            status = self._manager.poll()
            if status is not None:
                return status
            time.sleep(0.1)
        raise TimeoutError("local W18 manager exceeded hard timeout")

    def stop_monitor(self) -> None:
        if self._monitor_started:
            stopped, error = self._quota.stop_monitor()
            self._monitor_started = False
            if not stopped or error is not None:
                _fail(f"CPU quota monitor did not stop: {error or 'unknown failure'}")

    def terminate_manager_and_replicas(self) -> None:
        if self._manager is not None and self._manager.poll() is None:
            self._manager.terminate()
            try:
                self._manager.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._manager.kill()
                self._manager.wait(timeout=5)
        self._registry.cleanup(timeout_s=5.0)
        for handle in self._handles:
            handle.close()
        self._handles.clear()
        if self._manager_log is not None:
            self._manager_log.close()
            self._manager_log = None

    def terminate_owned_replica_scopes(self, deadline_monotonic: float) -> Mapping[str, object]:
        return self._quota.terminate_owned_scopes(deadline_ns=int(deadline_monotonic * 1_000_000_000))

    def verify_scope_cleanup(self, deadline_monotonic: float) -> Mapping[str, object]:
        return self._quota.verify_cleanup(deadline_ns=int(deadline_monotonic * 1_000_000_000))


def _write_new(path: Path, payload: Mapping[str, object]) -> None:
    with path.open("xb") as handle:
        handle.write(_canonical(payload))


def _rerun_native_stage_a_verifier(
    *, authority: Mapping[str, object], plan: Mapping[str, object],
    manager_argv: Sequence[str], runtime: Path,
    native_verifier_run: Callable[..., Any],
) -> str:
    """Re-run the externally approved verifier over the launch-bound inputs.

    The original receipt is provenance, not a substitute for a fresh native
    check.  The verifier timestamp is expected to differ; every other field
    must agree exactly with the retained receipt.
    """
    output = runtime / "fresh-native-stage-a-receipt.json"
    stage_a = plan.get("stage_a")
    if (not isinstance(stage_a, Mapping) or not isinstance(stage_a.get("verifier_arguments"), list)
            or not all(isinstance(value, str) for value in stage_a["verifier_arguments"])):
        _fail("executable plan lacks the persisted native Stage-A verifier invocation")
    command = (str(authority["binaries"]["stage_a_envelope_verifier"]),
               *stage_a["verifier_arguments"], "--output", str(output))
    try:
        invoked = native_verifier_run(command, capture_output=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _fail("pinned native Stage-A verifier could not complete before lifecycle construction")
        raise AssertionError from exc
    if getattr(invoked, "returncode", None) != 0:
        _fail("pinned native Stage-A verifier rejected the exact launch inputs")
    retained = _json_bytes(_regular(Path(str(authority["native_receipt"])), "native Stage-A receipt"), "native Stage-A receipt")
    fresh = _json_bytes(_regular(output, "fresh native Stage-A receipt"), "fresh native Stage-A receipt")
    if (set(fresh) != set(retained) or
            any(fresh.get(key) != value for key, value in retained.items() if key != "verification_monotonic_raw_ns") or
            not isinstance(fresh.get("verification_monotonic_raw_ns"), int) or
            fresh["verification_monotonic_raw_ns"] <= 0):
        _fail("fresh native Stage-A verifier output differs from the retained authority receipt")
    return _sha(_canonical(fresh))


def execute_excluded_local_shakedown(
    *, materialization_root: Path, manager_argv: Sequence[str], replica_argv: Sequence[Sequence[str]],
    quota_profile: Path, authorization_request: bytes, authorization_receipt: Mapping[str, object],
    tool_identity_approval_path: Path, execute: bool, timeout_s: int,
    current_revision: Callable[[], str], worktree_clean: Callable[[], bool],
    lifecycle_factory: Callable[[], _Lifecycle], pre_spawn_authority: Mapping[str, object] | None = None,
    quota_contract: cpu_quota.CpuQuotaContract | None = None,
    native_verifier_run: Callable[..., Any] = subprocess.run,
) -> dict[str, object]:
    """Run one local-only arm through an injected real lifecycle.

    The explicit ``execute`` flag is a second gate; false returns approval inputs
    only and never calls any lifecycle method.  The default CLI supplies no
    lifecycle, so a human cannot accidentally launch from the command line.
    """
    plan = backend.prepare_no_launch_backend(
        materialization_root=materialization_root, manager_argv=manager_argv,
        replica_argv=replica_argv, quota_profile=quota_profile,
    )
    expected_request = build_execution_request(plan, materialization_root=materialization_root, timeout_s=timeout_s)
    if authorization_request != expected_request:
        _fail("execution request differs from the current frozen backend plan")
    if not execute:
        return {"verdict": "PREPARED_NO_EXECUTION", "claim_eligible": False,
                "figure_eligible": False, "launch_permitted": False,
                "execution_request_sha256": _sha(expected_request)}
    if pre_spawn_authority is None or quota_contract is None:
        _fail("execution is blocked: complete pre-spawn Stage-A authority is required")
    root = Path(materialization_root).resolve()
    tool_approval = verify_execution_admission(
        plan=plan, materialization_root=root, manager_argv=manager_argv,
        replica_argv=replica_argv, authorization_request=authorization_request,
        authorization_receipt=authorization_receipt,
        tool_identity_approval_path=tool_identity_approval_path,
        pre_spawn_authority=pre_spawn_authority, current_revision=current_revision,
        worktree_clean=worktree_clean, quota_contract=quota_contract,
        timeout_s=timeout_s,
    )
    logs, runtime = _verify_fresh_runtime(root)
    logs.mkdir(mode=0o700)
    runtime.mkdir(mode=0o700)
    _write_new(runtime / "execution-authorization.json", dict(authorization_receipt))
    with (runtime / "tool-identity-approval.json").open("xb") as handle:
        handle.write(tool_approval)
    deadline = time.monotonic() + timeout_s
    failure: str | None = None
    manager_exit: int | None = None
    cleanup: dict[str, object] = {}
    lifecycle: _Lifecycle | None = None
    fresh_native_stage_a_receipt_sha256: str | None = None
    try:
        fresh_native_stage_a_receipt_sha256 = _rerun_native_stage_a_verifier(
            authority=pre_spawn_authority, plan=plan, manager_argv=manager_argv,
            runtime=runtime, native_verifier_run=native_verifier_run,
        )
        # The factory may create CPU scope bookkeeping.  It is intentionally
        # invoked only after this runner has atomically claimed fresh runtime
        # paths and retained both authorization documents.
        lifecycle = lifecycle_factory()
        for replica_id, argv in enumerate(replica_argv):
            lifecycle.start_replica(replica_id, argv, logs / f"replica-{replica_id}.log")
        lifecycle.start_manager(manager_argv, logs / "manager.log")
        manager_exit = lifecycle.wait_manager(deadline)
        if manager_exit != 0:
            failure = f"manager exited with {manager_exit}"
    except BaseException as exc:
        failure = str(exc).strip() or type(exc).__name__
        fresh = runtime / "fresh-native-stage-a-receipt.json"
        if fresh.exists() and not fresh.is_symlink():
            try:
                fresh_native_stage_a_receipt_sha256 = _sha(
                    _regular(fresh, "failed fresh native Stage-A receipt")
                )
            except OperatorCapacityV3LocalRunnerError:
                # The retained abort receipt records the failure; do not hide
                # it with a secondary read error from a malformed verifier output.
                pass
    finally:
        if lifecycle is None:
            cleanup["lifecycle_construction"] = "not_completed"
        else:
            # Cleanup always happens, exactly once and in the receipt's declared order.
            for name, operation in (
                ("stop_quota_monitor", lifecycle.stop_monitor),
                ("terminate_manager_and_replicas", lifecycle.terminate_manager_and_replicas),
            ):
                try:
                    operation()
                    cleanup[name] = "completed"
                except BaseException as exc:
                    cleanup[name] = f"failed:{str(exc).strip() or type(exc).__name__}"
                    failure = failure or f"{name} failed"
            for name, operation in (
                ("terminate_owned_replica_scopes", lifecycle.terminate_owned_replica_scopes),
                ("verify_scope_cleanup", lifecycle.verify_scope_cleanup),
            ):
                try:
                    cleanup[name] = dict(operation(deadline))
                except BaseException as exc:
                    cleanup[name] = f"failed:{str(exc).strip() or type(exc).__name__}"
                    failure = failure or f"{name} failed"
    receipt = {
        "schema_version": 1, "kind": "kauri-n31-operator-capacity-v3-local-shakedown-receipt-v1",
        "verdict": "ABORTED" if failure else "PROCESS_COMPLETED_PENDING_RAW_VALIDATION",
        "claim_eligible": False, "figure_eligible": False, "automatic_retries": 0,
        "execution_request_sha256": _sha(authorization_request), "manager_exit_code": manager_exit,
        "failure": failure, "cleanup": cleanup,
        "fresh_native_stage_a_receipt_sha256": fresh_native_stage_a_receipt_sha256,
        "raw_validation_required": True,
    }
    _write_new(runtime / ("local-shakedown-abort.json" if failure else "local-shakedown-receipt.json"), receipt)
    return receipt
