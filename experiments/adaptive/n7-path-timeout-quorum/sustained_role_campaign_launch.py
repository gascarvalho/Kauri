#!/usr/bin/env python3
"""One-cell, cgroup-bounded launch boundary for the frozen W19 campaign.

The launcher process itself stays outside the transient user scope.  The
scope owns the arm launcher and every process it creates, so systemd's
``RuntimeMaxSec`` and ``KillMode=control-group`` also cover the separate
sessions created by the native process helper.  This file never materializes
an input, creates an approval, retries, or advances a thesis claim.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from typing import Any, Callable, Mapping, Sequence

HERE = Path(__file__).resolve().parent
KAURI = HERE.parents[2]
SCOPE_SECONDS = 210
STAGE_PATH = Path("runtime/sustained-role-campaign-stage-receipt.json")
ABORT_PATH = Path("runtime/sustained-role-campaign-launch-abort.json")
RECEIPTS = {"fixed_e0": "sustained-role-fixed-e0-raw-bundle-receipt.json",
            "adaptive_e1": "sustained-role-adaptive-e1-raw-bundle-receipt.json"}


class CampaignLaunchError(RuntimeError):
    pass


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    if spec is None or spec.loader is None:
        raise CampaignLaunchError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


operator = _load("w19_campaign_operator_for_launch", "sustained_role_campaign_operator.py")
evaluator = _load("w19_campaign_evaluator_for_launch", "sustained_role_campaign_evaluator.py")
fixed = _load("w19_fixed_launcher_for_campaign", "sustained_role_fixed_e0_launcher.py")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 512 * 1024:
        raise CampaignLaunchError(f"{label} is not a bounded regular file")
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CampaignLaunchError(f"{label} is not JSON") from exc
    if not isinstance(value, dict) or raw != _canonical(value):
        raise CampaignLaunchError(f"{label} is not canonical JSON")
    return value, raw


def _git_clean_pinned(revision: str, runner: Callable[..., Any] = subprocess.run) -> None:
    def run(*args: str) -> str:
        completed = runner(["git", "-C", str(KAURI), *args], text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if completed.returncode != 0:
            raise CampaignLaunchError("cannot verify pinned Kauri revision")
        return completed.stdout
    if run("rev-parse", "HEAD").strip() != revision:
        raise CampaignLaunchError("active Kauri HEAD differs from campaign freeze")
    if run("status", "--porcelain=v1").strip():
        raise CampaignLaunchError("active Kauri worktree is not clean")


def _raw_clock() -> int:
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)


def verify_one_cell(freeze: Mapping[str, Any], manifest: Mapping[str, Any], *, campaign_root: Path,
                    ordinal: int, authorization: Path, git_runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """Validate every pre-spawn binding; this function has no side effects."""
    frozen, cells = operator.validate_manifest(manifest, freeze)
    if type(ordinal) is not int or ordinal not in range(1, len(cells) + 1):
        raise CampaignLaunchError("ordinal is outside the frozen campaign")
    _git_clean_pinned(frozen["repository_revision"], git_runner)
    cell = cells[ordinal - 1]
    root = operator._reject_lexical_symlink_ancestors(Path(campaign_root), "campaign root")
    if not root.is_dir():
        raise CampaignLaunchError("campaign root is unavailable")
    run_root = operator._lexical_child(root, cell["run_root"], "manifest cell")
    if not run_root.is_dir():
        raise CampaignLaunchError("materialized cell root is unavailable")
    reuse_markers = (*evaluator.NEGATIVE_MARKERS, *RECEIPTS.values(),
                     "runtime/sustained-role-fixed-e0-launch-authorization.json",
                     "runtime/sustained-role-adaptive-e1-launch-authorization.json")
    if any((run_root / name).exists() or (run_root / name).is_symlink() for name in reuse_markers):
        raise CampaignLaunchError("cell has an abort or prior receipt; reuse is forbidden")
    expected = evaluator._check_campaign_manifest(frozen, manifest)[ordinal - 1]
    stage_record = {"stage_receipt_path": str(STAGE_PATH),
                    "stage_receipt_sha256": hashlib.sha256((run_root / STAGE_PATH).read_bytes()).hexdigest()}
    request_sha = evaluator._stage_receipt(run_root, stage_record, expected, manifest, frozen)
    stage, _stage_raw = _read_json(run_root / STAGE_PATH, "campaign stage receipt")
    if stage.get("host_identity") != operator._host_boot_identity():
        raise CampaignLaunchError("campaign stage host or Linux boot differs from launch host")
    plan, _plan_raw = _read_json(run_root / "runtime/sustained-role-execution-plan.json", "execution plan")
    request, request_raw = _read_json(run_root / "runtime/sustained-role-authorization-request.json", "authorization request")
    if (request.get("execution_plan_sha256") != plan.get("plan_sha256") or
            hashlib.sha256(request_raw).hexdigest() != request_sha or
            plan.get("repository_revision") != frozen["repository_revision"] or
            request.get("arm") != cell["arm"] or request.get("no_retry") is not True):
        raise CampaignLaunchError("stage/request/plan identity drift")
    window = plan.get("scheduled_window")
    if (not isinstance(window, Mapping) or
            type(window.get("start_monotonic_ns")) is not int or
            type(window.get("end_monotonic_ns")) is not int or
            window["end_monotonic_ns"] - window["start_monotonic_ns"] != 70_000_000_000):
        raise CampaignLaunchError("execution plan lacks the frozen RAW-clock horizon")
    try:
        now = _raw_clock()
    except Exception as exc:
        raise CampaignLaunchError("cannot sample launch RAW clock") from exc
    if (type(now) is not int or
            window["start_monotonic_ns"] - now < 30_000_000_000 or
            window["end_monotonic_ns"] - now > 180_000_000_000):
        raise CampaignLaunchError("launch window lacks prearm or scope cleanup reserve")
    authorization = Path(authorization)
    if ".." in authorization.parts:
        raise CampaignLaunchError("external authorization path may not traverse parent directories")
    authorization = operator._reject_lexical_symlink_ancestors(
        authorization, "external authorization").resolve(strict=True)
    try:
        authorization.relative_to(run_root)
    except ValueError:
        pass
    else:
        raise CampaignLaunchError("authorization must remain external to the run archive")
    approval, approval_raw = _read_json(authorization, "external authorization")
    kind = ("kauri-n7-sustained-role-fixed-e0-launch-authorization-v1" if cell["arm"] == "fixed_e0"
            else "kauri-n7-sustained-role-adaptive-e1-launch-authorization-v1")
    expected_approval = {"schema_version", "kind", "request_sha256", "plan_sha256", "approval_reference", "approved_utc", "no_retry"}
    if (set(approval) != expected_approval or approval.get("schema_version") != 1 or
            approval.get("kind") != kind or approval.get("request_sha256") != request_sha or
            approval.get("plan_sha256") != plan.get("plan_sha256") or approval.get("no_retry") is not True or
            approval.get("approval_reference") != frozen["campaign_approval_reference"] or
            not fixed._utc_timestamp(approval.get("approved_utc"))):
        raise CampaignLaunchError("external approval is not exact or not bound to campaign approval")
    approval_time = datetime.fromisoformat(approval["approved_utc"].removesuffix("Z") + "+00:00")
    if approval_time.tzinfo != timezone.utc or approval_time <= evaluator._check_freeze(frozen):
        raise CampaignLaunchError("external approval does not postdate the campaign freeze")
    return {"cell": cell, "run_root": run_root, "authorization_path": authorization,
            "stage_receipt_path": str(STAGE_PATH),
            "stage_receipt_sha256": stage_record["stage_receipt_sha256"],
            "authorization_sha256": hashlib.sha256(approval_raw).hexdigest()}


def scope_command(*, unit: str, arm: str, run_root: Path, authorization: Path,
                  authorization_sha256: str) -> list[str]:
    launcher = HERE / ("sustained_role_fixed_e0_launcher.py" if arm == "fixed_e0" else "sustained_role_adaptive_e1_launcher.py")
    # systemd 249 rejects --wait together with --scope.  The supervisor is
    # deliberately outside this scope and polls its cgroup state below.
    return ["systemd-run", "--user", "--scope", "--unit", unit,
            "-p", f"RuntimeMaxSec={SCOPE_SECONDS}s", "-p", "KillMode=control-group",
            "-p", "SendSIGKILL=yes", str(sys.executable), str(launcher),
            "--run-root", str(run_root), "--authorization", str(authorization),
            "--expected-authorization-sha256", authorization_sha256, "--execute"]


def _cgroup_empty(control_group: str) -> bool:
    """Read cgroup-v2 descendant population directly; errors are never empty."""
    if not control_group.startswith("/") or ".." in Path(control_group).parts:
        raise CampaignLaunchError("systemd reported an unsafe control-group path")
    root = Path("/sys/fs/cgroup").resolve()
    path = (root / control_group.lstrip("/") / "cgroup.events").resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise CampaignLaunchError("systemd control-group path escapes cgroup root") from exc
    if path.is_symlink() or not path.is_file():
        raise CampaignLaunchError("scope cgroup events file is unavailable")
    try:
        events = dict(line.split(" ", 1) for line in path.read_text(encoding="ascii").splitlines() if " " in line)
    except OSError as exc:
        raise CampaignLaunchError("cannot read scope cgroup events") from exc
    if set(events) != {"populated", "frozen"} or events["populated"] not in {"0", "1"}:
        raise CampaignLaunchError("scope cgroup events are malformed")
    return events["populated"] == "0"


def _scope_empty(unit: str, runner: Callable[..., Any], *, monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 cgroup_empty: Callable[[str], bool] = _cgroup_empty) -> str:
    """Wait for terminal scope state *and* an empty scope cgroup.

    ``ControlGroup=`` names a cgroup even after it is empty, so it is not
    evidence by itself.  The cgroup-v2 ``cgroup.events`` population bit is
    read directly on each terminal sample; it includes descendants.
    A query error is never treated as garbage collection.  A collected scope
    must be reported by systemctl itself as inactive with no ControlGroup.
    """
    deadline = monotonic() + 8.0
    last = ""
    while True:
        completed = runner(["systemctl", "--user", "show", f"{unit}.scope", "--property=ActiveState",
                            "--property=SubState", "--property=Result", "--property=ControlGroup"],
                           text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        last = completed.stdout if isinstance(completed.stdout, str) else ""
        terminal = ("ActiveState=inactive" in last or
                    ("ActiveState=failed" in last and "Result=timeout" in last))
        if completed.returncode != 0:
            raise CampaignLaunchError("cannot verify transient scope state")
        if completed.returncode == 0 and terminal:
            group_line = next((line for line in last.splitlines() if line.startswith("ControlGroup=")), None)
            if group_line == "ControlGroup=":
                return last
            group = group_line.split("=", 1)[1] if group_line is not None else None
            if not group:
                sleep(0.05)
                continue
            if cgroup_empty(group):
                return last
        if monotonic() >= deadline:
            raise CampaignLaunchError("transient scope did not become inactive")
        sleep(0.05)
    # An inactive systemd scope has no remaining member processes.  This is
    # systemd's cgroup lifecycle invariant, rather than a PID-racy ps scan.


def _seal_abort(root: Path, *, cell: Mapping[str, Any], detail: str,
                state: str = "ABORTED_NO_RETRY_SCOPE_CLEAN",
                diagnostics: Mapping[str, Any] | None = None) -> None:
    """Leave an immutable no-retry outcome when the scoped launcher fails."""
    path = root / ABORT_PATH
    if path.exists() or path.is_symlink():
        return
    if state not in {"ABORTED_NO_RETRY_SCOPE_CLEAN", "ABORTED_NO_RETRY_SCOPE_STATUS_UNVERIFIED",
                     "ABORTED_NO_RETRY_VALIDATION_REJECTED_SCOPE_CLEAN"}:
        raise CampaignLaunchError("invalid campaign launch abort state")
    payload = {"schema_version": 1, "kind": "kauri-n7-sustained-role-campaign-launch-abort-v1",
               "state": state, "ordinal": cell["ordinal"],
               "arm": cell["arm"], "run_id": cell["run_id"], "no_retry": True,
               "claim_eligible": False, "figure_eligible": False, "detail": detail[:512]}
    if diagnostics is not None:
        payload["scope_diagnostics"] = dict(diagnostics)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(_canonical(payload)); stream.flush(); os.fsync(stream.fileno())


def _scope_diagnostics(completed: Any, unit: str) -> dict[str, Any]:
    stderr = str(completed.stderr or "")
    escaped_tail = repr(stderr[-256:])[-256:]
    return {"scope_unit": f"{unit}.scope", "exit_code": completed.returncode,
            "stderr_sha256": hashlib.sha256(stderr.encode("utf-8", errors="replace")).hexdigest(),
            "stderr_tail_escaped": escaped_tail}


def execute_one_cell(freeze: Mapping[str, Any], manifest: Mapping[str, Any], *, campaign_root: Path,
                     ordinal: int, authorization: Path, runner: Callable[..., Any] = subprocess.run,
                     git_runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    prepared = verify_one_cell(freeze, manifest, campaign_root=campaign_root, ordinal=ordinal,
                               authorization=authorization, git_runner=git_runner)
    cell, root = prepared["cell"], prepared["run_root"]
    unit = f"kauri-w19-{cell['run_id']}-{uuid.uuid4().hex[:12]}"
    try:
        completed = runner(scope_command(unit=unit, arm=cell["arm"], run_root=root,
                                         authorization=prepared["authorization_path"],
                                         authorization_sha256=prepared["authorization_sha256"]),
                           text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except BaseException as exc:
        _seal_abort(root, cell=cell, state="ABORTED_NO_RETRY_SCOPE_STATUS_UNVERIFIED",
                    detail="scope supervisor did not return; process state requires manual verification")
        raise CampaignLaunchError("scoped launch supervisor did not return; no retry") from exc
    launch_output = str(completed.stdout or "") + "\n" + str(completed.stderr or "")
    diagnostics = _scope_diagnostics(completed, unit)
    if f"Running scope as unit: {unit}.scope" not in launch_output:
        _seal_abort(root, cell=cell, state="ABORTED_NO_RETRY_SCOPE_STATUS_UNVERIFIED",
                    detail="systemd did not confirm the exact transient scope",
                    diagnostics=diagnostics)
        raise CampaignLaunchError("systemd did not confirm the exact transient scope; no retry")
    try:
        scope_status = _scope_empty(unit, runner)
    except BaseException as exc:
        _seal_abort(root, cell=cell, state="ABORTED_NO_RETRY_SCOPE_STATUS_UNVERIFIED",
                    detail="scope cleanup could not be verified; process state requires manual verification",
                    diagnostics=diagnostics)
        raise CampaignLaunchError("scope cleanup could not be verified; no retry") from exc
    receipt_path = root / RECEIPTS[cell["arm"]]
    if completed.returncode != 0 or receipt_path.is_symlink() or not receipt_path.is_file():
        _seal_abort(root, cell=cell, detail=("scoped launcher failed or lacked raw receipt; " +
                                               scope_status.replace("\n", "; ")[:300]),
                    diagnostics=diagnostics)
        raise CampaignLaunchError("scope failed or launcher did not seal its raw receipt; no retry")
    raw = receipt_path.read_bytes()
    record = {"pair_index": cell["pair_index"], "ordinal": cell["ordinal"], "arm": cell["arm"],
              "root": str(root), "receipt_path": receipt_path.name,
              "receipt_sha256": hashlib.sha256(raw).hexdigest(),
              "stage_receipt_path": prepared["stage_receipt_path"],
              "stage_receipt_sha256": prepared["stage_receipt_sha256"]}
    # One-arm replay immediately follows launch.  It remains component-only.
    try:
        evaluator._one_cell({key: record[key] for key in ("pair_index", "ordinal", "arm", "root", "receipt_path", "receipt_sha256")},
                            expected_pair=cell["pair_index"], expected_ordinal=cell["ordinal"],
                            expected_arm=cell["arm"], freeze=freeze, frozen_at=evaluator._check_freeze(freeze))
    except BaseException as exc:
        _seal_abort(root, cell=cell, state="ABORTED_NO_RETRY_VALIDATION_REJECTED_SCOPE_CLEAN",
                    detail="independent immediate per-cell replay rejected the sealed raw bundle",
                    diagnostics=diagnostics)
        raise CampaignLaunchError("independent per-cell replay rejected the raw bundle; no retry") from exc
    return record


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", type=Path, required=True); parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--campaign-root", type=Path, required=True); parser.add_argument("--ordinal", type=int, required=True)
    parser.add_argument("--authorization", type=Path, required=True); parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute: parser.error("refusing to launch without --execute")
    try:
        freeze, _ = _read_json(args.freeze, "campaign freeze"); manifest, _ = _read_json(args.manifest, "campaign manifest")
        print(json.dumps(execute_one_cell(freeze, manifest, campaign_root=args.campaign_root, ordinal=args.ordinal,
                                           authorization=args.authorization), sort_keys=True, separators=(",", ":")))
    except (CampaignLaunchError, OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__": raise SystemExit(main())
