"""Source-bound, one-attempt W18 cluster lifecycle and native admission.

This adapter has distinct requests and receipts from the local rehearsal. It
collects the actual exclusive booking, clean source and build identity before
entering a bounded Linux scope. Raw acceptance is a separate offline step.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import socket
import subprocess
import time
import sys
import re
from zoneinfo import ZoneInfo

from . import cpu_quota
from . import operator_capacity_v3_authority as authority
from . import operator_capacity_v3_backend as backend
from . import operator_capacity_v3_cluster_profiles as profiles
from . import operator_capacity_v3_local_runner as physical


BOOKINGS = {
    "32usk1i80tieqq3e8jterd8as4": ("2026-10-02 09:30", "2026-10-02 17:00"),
    "9ju0eqk272j7ofgtkvii0rag68": ("2026-10-02 17:00", "2026-10-02 22:00"),
    "i1v82b83o8flllp1g9ctb7b084": ("2026-10-02 22:00", "2026-10-03 00:00"),
}
REQUEST_KIND = "kauri-w18-cluster-arm-request-v1"
APPROVAL_KIND = "kauri-w18-cluster-arm-approval-v1"
RECEIPT_KIND = "kauri-w18-cluster-arm-receipt-v1"
TIMEOUT_S = 300


class ClusterError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii") + b"\n"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path, maximum=512 * 1024 * 1024):
    raw = authority._read(Path(path), "cluster artifact", maximum)
    return authority._json(raw, "cluster artifact"), raw


def write(path, value):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(canonical(value)); stream.flush(); os.fsync(stream.fileno())


def clock():
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)


def booking_row(stdout, booking_id, observed_utc, *, reserve_s=360):
    if booking_id not in BOOKINGS:
        raise ClusterError("W18 booking was not authorized for this study")
    rows = [[field.strip() for field in line.split("|")[1:-1]]
            for line in stdout.splitlines() if line.startswith("|")]
    selected = [row for row in rows if row[:4] ==
                [booking_id, "proteina02", "gascarvalho", "EXCLUSIVE"]]
    if len(selected) != 1 or len(selected[0]) != 7 or tuple(selected[0][5:]) != BOOKINGS[booking_id]:
        raise ClusterError("exact exclusive W18 booking is absent")
    start, end = [datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("Europe/Lisbon"))
                  for value in BOOKINGS[booking_id]]
    if not start <= observed_utc < end or (end - observed_utc).total_seconds() < reserve_s:
        raise ClusterError("W18 booking is inactive or lacks fixed scope/cleanup reserve")
    return selected[0]


def repository_state(repo, revision):
    for arguments, expected in ((["rev-parse", "HEAD"], revision),
            (["branch", "--show-current"], "feature/adaptive-epoch-throughput"),
            (["rev-parse", "origin/feature/adaptive-epoch-throughput"], revision),
            (["status", "--porcelain=v1", "--untracked-files=all"], "")):
        if subprocess.check_output(["git", "-C", str(repo), *arguments], text=True).strip() != expected:
            raise ClusterError("W18 source is not the exact clean pushed fixed branch")


def require_no_owned_native():
    leftovers = []
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            if process.stat().st_uid == os.getuid() and (process / "comm").read_text().strip() in {
                    "hotstuff-app", "adaptation-mana", "adaptation-manager"}:
                leftovers.append(int(process.name))
        except (OSError, PermissionError):
            pass
    if leftovers:
        raise ClusterError("owned native workload remains before W18 admission")
    return leftovers


def build_request(plan, *, root, run_id, build_receipt, booking_id):
    if (plan.get("cluster_physical_regime") not in {"heterogeneous", "homogeneous"} or
            plan.get("verdict") != "BACKEND_PLAN_REVIEW_REQUIRED_NO_EXECUTION" or
            plan.get("automatic_retries") != 0 or plan.get("launch_permitted") is not False or
            root != root.resolve() or booking_id not in BOOKINGS or
            re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id) is None):
        raise ClusterError("W18 cluster plan or root is not frozen")
    return {"kind": REQUEST_KIND, "schema_version": 1, "root": str(root), "run_id": run_id,
            "plan_sha256": sha(canonical(plan)), "revision": plan["revision"],
            "materialization_manifest_sha256": plan["materialization_manifest_sha256"],
            "build_receipt_sha256": sha(authority._read(build_receipt, "cluster build")),
            "booking_id": booking_id, "booking_bounds": list(BOOKINGS[booking_id]),
            "physical_regime": plan["cluster_physical_regime"], "arm": plan["arm"],
            "quota_profile_sha256": plan["cluster_quota_sha256"],
            "hard_timeout_s": TIMEOUT_S, "kill_grace_s": 15, "automatic_retries": 0,
            "claim_eligible": False, "figure_eligible": False}


def verify_approval(request, external, expected_sha):
    approved, raw = read(external, 256 * 1024)
    expected = {**request, "kind": APPROVAL_KIND, "request_sha256": sha(canonical(request)),
                "approval_reference": approved.get("approval_reference"),
                "approved_utc": approved.get("approved_utc")}
    if (raw != canonical(approved) or sha(raw) != expected_sha or canonical(approved) != canonical(expected) or
            not isinstance(approved.get("approval_reference"), str) or not approved["approval_reference"] or
            not isinstance(approved.get("approved_utc"), str) or not approved["approved_utc"].endswith("Z")):
        raise ClusterError("external W18 approval differs from exact cluster request")
    return approved


def collect_authority(*, repo, revision, build_receipt, booking_id, plan):
    if socket.gethostname() != "proteina02" or pwd.getpwuid(os.getuid()).pw_name != "gascarvalho":
        raise ClusterError("wrong cluster host or Unix identity")
    repository_state(repo, revision)
    build, build_raw = read(build_receipt)
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if (build.get("kind") != "kauri-w18-cluster-build-provenance-v1" or
            build.get("repository_revision") != revision or build.get("origin_revision") != revision or
            build.get("repository_clean_after_build") is not True or build.get("build_exit_code") != 0 or
            build.get("host") != "proteina02" or build.get("linux_boot_id") != boot or
            build.get("build_type") != "Release"):
        raise ClusterError("W18 build provenance differs from source or host")
    if sha(authority._read(Path(build["build_log_path"]), "actual clean build log")) != build["build_log_sha256"]:
        raise ClusterError("actual W18 clean-build log differs")
    cache = authority._read(repo / "build-adaptive/CMakeCache.txt", "effective build cache")
    if sha(cache) != build["cmake_cache_sha256"] or any(flag not in cache for flag in
            (b"CMAKE_BUILD_TYPE:STRING=Release\n", b"HOTSTUFF_TWO_STEP:BOOL=OFF\n",
             b"HOTSTUFF_DEBUG_LOG:BOOL=OFF\n", b"HOTSTUFF_NORMAL_LOG:BOOL=ON\n")):
        raise ClusterError("W18 effective build flags differ")
    binaries = build.get("binaries")
    if not isinstance(binaries, dict) or set(binaries) != {
            "adaptation_manager", "hotstuff_app", "keygen", "tls_keygen", "capacity_digest",
            "epoch0_digest", "stage_a_envelope_signer", "stage_a_envelope_verifier",
            "stage_b_authorization_verifier", "identity_parity_verifier", "readiness_verifier"}:
        raise ClusterError("W18 build does not close all native tool identities")
    for name, row in binaries.items():
        path = Path(row["path"])
        if not path.resolve().is_relative_to(repo / "build-adaptive") or sha(
                authority._read(path, "built native tool", 512 * 1024 * 1024)) != row["sha256"]:
            raise ClusterError("W18 executable differs from exact native build")
        if name in plan["binary_sha256"] and plan["binary_sha256"][name] != row["sha256"]:
            raise ClusterError("materialized executable differs from native build")
    leftovers = require_no_owned_native()
    command = ["gsd_manager", "-N", "proteina02", "booking", "ls", "-c", "-u", "gascarvalho", "-m", "exclusive"]
    stdout = subprocess.check_output(command, text=True, timeout=25)
    now = datetime.now(timezone.utc)
    return {"kind": "kauri-w18-native-cluster-authority-v1", "host": "proteina02",
            "user": "gascarvalho", "linux_boot_id": boot, "repository_revision": revision,
            "build_receipt_sha256": sha(build_raw), "booking_command": command,
            "booking_stdout": stdout, "booking_id": booking_id,
            "booking_row": booking_row(stdout, booking_id, now),
            "observed_utc": now.isoformat(), "observed_monotonic_raw_ns": clock(),
            "owned_native_processes": leftovers}


def scope_empty(unit):
    result = subprocess.run(["systemctl", "--user", "show", unit, "-p", "ControlGroup",
                             "-p", "ActiveState", "-p", "SubState"], capture_output=True, text=True, timeout=10)
    properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    group = properties.get("ControlGroup")
    if result.returncode != 0 and not properties and "could not be found" in result.stderr:
        return {"unit": unit, "properties": {"LoadState": "not-found"}, "populated": 0}
    if properties.get("ActiveState") not in {"inactive", "failed"}:
        raise ClusterError("owned W18 scope remains active")
    if group:
        events = Path("/sys/fs/cgroup") / group.lstrip("/") / "cgroup.events"
        if events.exists() and "populated 0\n" not in events.read_text():
            raise ClusterError("owned W18 scope still contains processes")
    return {"unit": unit, "properties": properties, "populated": 0}


class ClusterLifecycle(physical.CpuQuotaLocalLifecycle):
    """Reuse physical quota bookkeeping with independent cluster scope limits."""

    def __init__(self, **arguments):
        super().__init__(**arguments)
        base_spawn = self._quota._base_spawn

        def bounded_spawn(registry, **kwargs):
            command = list(kwargs["command"])
            if command[0] != "systemd-run" or command.count("--") != 1:
                raise ClusterError("replica spawn does not have exact CPU scope grammar")
            index = command.index("--")
            command[index:index] = ["--property=RuntimeMaxSec=300s",
                "--property=KillMode=control-group", "--property=SendSIGKILL=yes"]
            return base_spawn(registry, **{**kwargs, "command": tuple(command)})

        self._quota._base_spawn = bounded_spawn

    def start_replica(self, replica_id, argv, log):
        super().start_replica(replica_id, argv, log)
        unit = cpu_quota._unit_name(self._quota.run_id, replica_id)
        raw = subprocess.check_output(["systemctl", "--user", "show", unit, "-p", "RuntimeMaxUSec"], text=True, timeout=10)
        if raw.strip() not in {"RuntimeMaxUSec=5min", "RuntimeMaxUSec=300s"}:
            raise ClusterError("replica scope does not retain fixed 300-second limit")
        write(self._root / "runtime" / f"cluster-replica-{replica_id}-scope-limit.json",
              {"unit": unit, "raw_properties": raw, "hard_timeout_s": TIMEOUT_S})


def execute_child(root, *, request_path, approval_path, approval_sha, build_receipt,
                  quota_profile, repo, tool_identity_approval, native_receipt):
    """One fresh root, real cluster admission, fixed scope and unconditional cleanup."""
    requested, request_raw = read(request_path)
    argv, _ = read(root / "private-argv.json")
    manager, replicas = argv["manager"], argv["replicas"]
    plan = backend.prepare_no_launch_backend(materialization_root=root,
        manager_argv=manager, replica_argv=replicas, quota_profile=quota_profile,
        cluster_physical_regime=requested["physical_regime"])
    request = build_request(plan, root=root, run_id=requested["run_id"],
        build_receipt=build_receipt, booking_id=requested["booking_id"])
    if request_raw != canonical(request):
        raise ClusterError("cluster request differs from current exact materialized plan")
    approved = verify_approval(request, approval_path, approval_sha)
    if physical._value(manager, "--structured-event-run-id") != request["run_id"]:
        raise ClusterError("cluster manager run ID differs from request")
    observed = collect_authority(repo=repo, revision=request["revision"],
        build_receipt=build_receipt, booking_id=request["booking_id"], plan=plan)
    unit = "kauri-w18-" + request["run_id"] + ".scope"
    cgroup = Path("/proc/self/cgroup").read_text()
    if not any(line.startswith("0::/") and line.endswith("/" + unit) for line in cgroup.splitlines()):
        raise ClusterError("cluster child is outside its exact bounded scope")
    base_name = ("n31-static-resource-cpu-sham-v1.json" if request["physical_regime"] == "heterogeneous"
                 else "n31-operator-capacity-homogeneous-control-v1.json")
    base_profile = repo / "experiments/adaptive/profiles" / base_name
    contract = profiles.load_cluster_contract(quota_profile,
        base_profile_path=base_profile, regime=request["physical_regime"])
    profiles.verify_loaded_contract(contract, request["physical_regime"])
    manifest, _ = read(root / "materialization-manifest.json")
    build, _ = read(build_receipt)
    tool_path = tool_identity_approval
    tool_raw = physical.verify_tool_identity_before_spawn(tool_path, plan=plan,
        manager_argv=manager, replica_argv=replicas)
    tool = json.loads(tool_raw)
    if any(tool["binary_sha256"].get(name) != row["sha256"] for name, row in build["binaries"].items()
           if name != "readiness_verifier"):
        raise ClusterError("native Stage-A tool approval differs from cluster build")
    physical._verify_synthetic_config_before_spawn(root, manager)
    logs, runtime = physical._verify_fresh_runtime(root)
    logs.mkdir(mode=0o700); runtime.mkdir(mode=0o700)
    write(runtime / "cluster-request.json", request)
    write(runtime / "cluster-approval.json", approved)
    write(runtime / "cluster-pre-spawn.json", {**observed, "unit": unit,
        "child_pid": os.getpid(), "child_cgroup": cgroup,
        "external_approval_sha256": approval_sha, "spawn_observed_ns": clock()})
    write(runtime / "manager-argv.json", manager)
    (runtime / "cluster-build.json").write_bytes(authority._read(build_receipt, "cluster build"))
    (runtime / "tool-identity-approval.json").write_bytes(tool_raw)
    original = native_receipt
    original_raw = authority._read(original, "original Stage-A native receipt")
    if sha(original_raw) != plan["stage_a"]["native_receipt_sha256"]:
        raise ClusterError("original signed Stage-A verification changed")
    (runtime / "stage-a-verifier-receipt.json").write_bytes(original_raw)
    frozen_raw = authority._read(quota_profile, "frozen cluster CPU profile")
    profiles.validate_quota_bytes(frozen_raw, request["physical_regime"])
    (runtime / "frozen-cpu-quota-contract.json").write_bytes(frozen_raw)
    (runtime / "cluster-base-profile.json").write_bytes(authority._read(base_profile, "pinned base profile"))
    deadline = time.monotonic() + TIMEOUT_S
    lifecycle = None; failure = None; cleanup = {}; window = None
    manager_exit = manager_after = None; terminal = False; fresh_sha = None
    try:
        fresh_sha = physical._rerun_native_stage_a_verifier(authority={
            "binaries": {name: row["path"] for name, row in build["binaries"].items()},
            "native_receipt": str(original)}, plan=plan, manager_argv=manager,
            runtime=runtime, native_verifier_run=subprocess.run)
        lifecycle = ClusterLifecycle(contract=contract, run_id=request["run_id"], root=root)
        physical._verify_synthetic_config_before_spawn(root, manager)
        for replica, command in enumerate(replicas):
            lifecycle.start_replica(replica, command, logs / f"replica-{replica}.log")
        physical._verify_synthetic_config_before_spawn(root, manager)
        lifecycle.start_manager(manager, logs / "manager.log")
        window = lifecycle.await_e1_measurement_window(deadline)
        terminal = lifecycle.manager_success_terminal_verified()
        manager_exit = lifecycle.manager_exit_status()
        if not terminal or manager_exit not in (None, 0):
            raise ClusterError("cluster manager lacks successful all-31 terminal")
    except BaseException as exc:
        failure = str(exc) or type(exc).__name__
    finally:
        if lifecycle is not None:
            for name, operation in (("stop_quota_monitor", lifecycle.stop_monitor),
                    ("terminate_manager_and_replicas", lifecycle.terminate_manager_and_replicas)):
                try:
                    operation(); cleanup[name] = "completed"
                except BaseException as exc:
                    cleanup[name] = "failed:" + str(exc); failure = failure or name
            try:
                manager_after = lifecycle.manager_exit_status()
            except BaseException as exc:
                failure = failure or str(exc)
            for name, operation in (("terminate_owned_replica_scopes", lifecycle.terminate_owned_replica_scopes),
                    ("verify_scope_cleanup", lifecycle.verify_scope_cleanup)):
                try:
                    cleanup[name] = operation(deadline)
                except BaseException as exc:
                    cleanup[name] = "failed:" + str(exc); failure = failure or name
    receipt = {"schema_version": 1, "kind": RECEIPT_KIND,
        "verdict": "ABORTED_NO_RETRY" if failure else "PROCESS_COMPLETED_PENDING_RAW_VALIDATION",
        "claim_eligible": False, "figure_eligible": False, "automatic_retries": 0,
        "execution_request_sha256": sha(request_raw), "manager_exit_code": manager_exit,
        "manager_exit_code_after_cleanup": manager_after, "manager_success_terminal_verified": terminal,
        "failure": failure, "cleanup": cleanup, "e1_measurement_window": window,
        "fresh_native_stage_a_receipt_sha256": fresh_sha, "raw_validation_required": True}
    write(runtime / ("cluster-abort.json" if failure else "cluster-receipt.json"), receipt)
    if failure:
        raise ClusterError(failure)
    return receipt


def execute(root, *, request_path, approval_path, approval_sha, build_receipt,
            quota_profile, repo, tool_identity_approval, native_receipt):
    """Bound one cluster child and force cleanup of exactly its 31 owned scopes."""
    requested, raw = read(request_path)
    verify_approval(requested, approval_path, approval_sha)
    if requested["kind"] != REQUEST_KIND or requested["root"] != str(root) or root != root.resolve():
        raise ClusterError("wrapper request root differs")
    unit = "kauri-w18-" + requested["run_id"] + ".scope"
    command = ["systemd-run", "--user", "--scope", "--quiet", "--unit", unit,
        "-p", "RuntimeMaxSec=300s", "-p", "KillMode=control-group", "-p", "SendSIGKILL=yes",
        sys.executable, "-m", "kauri_experiment.operator_capacity_v3_cluster", "child",
        "--root", str(root), "--request", str(request_path), "--approval", str(approval_path),
        "--approval-sha", approval_sha, "--build", str(build_receipt),
        "--quota", str(quota_profile), "--repo", str(repo),
        "--tools", str(tool_identity_approval), "--stage-a", str(native_receipt)]
    started = clock(); failure = None; returned = None
    try:
        returned = subprocess.run(command, capture_output=True, timeout=315)
        if returned.returncode != 0:
            raise ClusterError("bounded W18 child failed; inspect its preserved receipt and scope stderr")
    except BaseException as exc:
        failure = str(exc) or type(exc).__name__
    finally:
        # Per-replica scopes live outside the child scope. Stop only this exact
        # run's names; never terminate another user's process or another run.
        units = [cpu_quota._unit_name(requested["run_id"], i) for i in range(31)] + [unit]
        cleanup = []
        for owned in units:
            try:
                subprocess.run(["systemctl", "--user", "stop", owned], capture_output=True, timeout=10)
                cleanup.append(scope_empty(owned))
            except BaseException as exc:
                cleanup.append({"unit": owned, "error": str(exc)}); failure = failure or "scope cleanup failed"
        runtime = root / "runtime"
        if not runtime.exists():
            runtime.mkdir(mode=0o700)
        post = {"kind": "kauri-w18-cluster-post-scope-v1", "run_id": requested["run_id"],
            "unit": unit, "scope_started_ns": started, "scope_returned_ns": clock(),
            "scope_returncode": None if returned is None else returned.returncode,
            "scope_stdout": "" if returned is None else returned.stdout.decode(errors="replace"),
            "scope_stderr": "" if returned is None else returned.stderr.decode(errors="replace"),
            "cleanup": cleanup, "failure": failure, "automatic_retries": 0}
        write(runtime / "cluster-post-scope.json", post)
    if failure:
        if not (root / "runtime/cluster-abort.json").exists():
            write(root / "runtime/cluster-abort.json", {"verdict": "ABORTED_NO_RETRY", "failure": failure})
        raise ClusterError(failure)
    from .operator_capacity_v3_cluster_validation import seal
    return seal(root)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("child", "execute"))
    for name in ("root", "request", "approval", "build", "quota", "repo", "tools", "stage-a"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--approval-sha", required=True)
    args = parser.parse_args()
    operation = execute_child if args.action == "child" else execute
    operation(args.root, request_path=args.request, approval_path=args.approval,
        approval_sha=args.approval_sha, build_receipt=args.build, quota_profile=args.quota,
        repo=args.repo, tool_identity_approval=args.tools, native_receipt=args.stage_a)


if __name__ == "__main__":
    main()
