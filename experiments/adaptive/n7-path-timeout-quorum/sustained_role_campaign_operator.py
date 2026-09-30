#!/usr/bin/env python3
"""No-launch serial operator preflight for the prospective W19 N=7 campaign.

This module never invokes either one-shot launcher.  It freezes only facts
that can honestly exist before a run: the AB/BA schedule, run identities, and
the one target host.  It then materializes exactly one current cell by calling
the existing no-launch producer.  Only after the producer writes that cell's
exact request bytes does this operator return a stage awaiting an external
approval.  It neither constructs an approval nor treats a future placeholder
as one.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import sys
import time
from typing import Any, Mapping, Sequence


HERE = Path(__file__).resolve().parent
_EVALUATOR_PATH = HERE / "sustained_role_campaign_evaluator.py"
_LOCAL_PATH = HERE / "sustained_role_local.py"
_HEX = frozenset("0123456789abcdef")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CLOCK = "CLOCK_MONOTONIC_RAW"
_HARD_TIMEOUT_SECONDS = 210
_MIN_WINDOW_NS = 70_000_000_000
_MIN_STAGE_RESERVE_NS = 30_000_000_000
_MIN_POST_MATERIALIZATION_RESERVE_NS = 45_000_000_000
_MAX_STAGE_LEAD_NS = 300_000_000_000
MANIFEST_KIND = "kauri-n7-sustained-role-serial-campaign-manifest-v1"
STAGE_KIND = "kauri-n7-sustained-role-serial-cell-preflight-v1"
STAGING_ABORT_KIND = "kauri-n7-sustained-role-serial-cell-staging-abort-v1"
STAGING_ABORT = Path("runtime/sustained-role-campaign-staging-abort.json")
STAGE_RECEIPT_KIND = "kauri-n7-sustained-role-serial-cell-stage-receipt-v1"
STAGE_RECEIPT = Path("runtime/sustained-role-campaign-stage-receipt.json")
_ALLOWED_PREPARE_KWARGS = frozenset({
    "peer_port", "client_port", "manager_port", "app_binary", "manager_binary",
    "keygen_binary", "tls_keygen_binary", "e0_helper_binary",
})


class CampaignOperatorError(ValueError):
    """A no-launch campaign manifest cannot safely be staged."""


def _load_evaluator():
    spec = importlib.util.spec_from_file_location("kauri_w19_campaign_evaluator", _EVALUATOR_PATH)
    if spec is None or spec.loader is None:
        raise CampaignOperatorError("cannot load W19 campaign evaluator")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


evaluator = _load_evaluator()


def _load_local():
    spec = importlib.util.spec_from_file_location("kauri_w19_sustained_role_local", _LOCAL_PATH)
    if spec is None or spec.loader is None:
        raise CampaignOperatorError("cannot load W19 no-launch producer")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


local = _load_local()


def _raw_clock() -> int:
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)


def _host_boot_identity() -> dict[str, str]:
    """Return the non-forgeable local Linux raw-clock domain identity."""
    hostname = socket.gethostname()
    boot_path = Path("/proc/sys/kernel/random/boot_id")
    try:
        boot_id = boot_path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise CampaignOperatorError("Linux boot ID is unavailable") from exc
    if not hostname or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", boot_id):
        raise CampaignOperatorError("host or Linux boot ID is malformed")
    return {"hostname": hostname, "linux_boot_id": boot_id}


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("ascii") + b"\n"
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CampaignOperatorError("document is not canonical ASCII JSON") from exc


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _hex(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(item not in _HEX for item in value):
        raise CampaignOperatorError(f"{label} is not a lower-case SHA-256")
    return value


def _utc(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CampaignOperatorError(f"{label} is not explicit UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise CampaignOperatorError(f"{label} is invalid") from exc
    if parsed.tzinfo != timezone.utc:
        raise CampaignOperatorError(f"{label} is not UTC")


def _relative_child(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts:
        raise CampaignOperatorError(f"{label} is not a safe relative path")
    return value


def _reject_lexical_symlink_ancestors(path: Path, label: str) -> Path:
    """Reject symlinks before resolution, including otherwise-hidden parents."""
    lexical = Path(path).absolute()
    for ancestor in (lexical, *lexical.parents):
        try:
            if ancestor.is_symlink():
                raise CampaignOperatorError(f"{label} has a symlink ancestor")
        except OSError as exc:
            raise CampaignOperatorError(f"cannot inspect {label} ancestry") from exc
    return lexical


def _lexical_child(root: Path, relative: str, label: str) -> Path:
    lexical_root = _reject_lexical_symlink_ancestors(root, f"{label} root")
    lexical_child = lexical_root / relative
    _reject_lexical_symlink_ancestors(lexical_child, label)
    resolved_root, resolved_child = lexical_root.resolve(), lexical_child.resolve()
    try:
        resolved_child.relative_to(resolved_root)
    except ValueError as exc:
        raise CampaignOperatorError(f"{label} escapes its root") from exc
    return resolved_child


def _seal_staging_abort(root: Path, *, manifest: Mapping[str, Any], freeze: Mapping[str, Any],
                        cell: Mapping[str, Any], detail: str) -> None:
    """Preserve a post-materialization failure without making it reusable."""
    path = root / STAGING_ABORT
    if path.exists() or path.is_symlink():
        return
    payload = {
        "schema_version": 1, "kind": STAGING_ABORT_KIND,
        "state": "STAGING_ABORTED_NO_LAUNCH_NO_RETRY",
        "manifest_sha256": manifest["manifest_sha256"], "freeze_sha256": freeze["freeze_sha256"],
        "ordinal": cell["ordinal"], "arm": cell["arm"], "run_id": cell["run_id"],
        "no_retry": True, "claim_eligible": False, "figure_eligible": False,
        "detail": detail[:512],
    }
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical(payload)); stream.flush(); os.fsync(stream.fileno())
    except OSError:
        pass


def _post_materialization_failure(root: Path, *, manifest: Mapping[str, Any], freeze: Mapping[str, Any],
                                  cell: Mapping[str, Any], detail: str) -> None:
    _seal_staging_abort(root, manifest=manifest, freeze=freeze, cell=cell, detail=detail)
    raise CampaignOperatorError(detail)


def _seal_stage_receipt(root: Path, *, manifest: Mapping[str, Any], freeze: Mapping[str, Any],
                        cell: Mapping[str, Any], host_identity: Mapping[str, str],
                        request_sha256: str, scheduled_window: Mapping[str, int]) -> tuple[str, str]:
    path = root / STAGE_RECEIPT
    if path.exists() or path.is_symlink():
        raise CampaignOperatorError("operator stage receipt already exists")
    payload: dict[str, Any] = {
        "schema_version": 1, "kind": STAGE_RECEIPT_KIND,
        "state": "MATERIALIZED_NO_LAUNCH_EXTERNAL_EXACT_APPROVAL_REQUIRED",
        "manifest_sha256": manifest["manifest_sha256"], "freeze_sha256": freeze["freeze_sha256"],
        "ordinal": cell["ordinal"], "pair_index": cell["pair_index"], "arm": cell["arm"],
        "run_id": cell["run_id"], "run_root": cell["run_root"], "target_host": cell["target_host"],
        "host_identity": dict(host_identity), "clock": _CLOCK,
        "scheduled_window": dict(scheduled_window), "hard_timeout_seconds": _HARD_TIMEOUT_SECONDS,
        "no_retry": True, "authorization_request_sha256": request_sha256,
        "claim_eligible": False, "figure_eligible": False,
    }
    payload["stage_receipt_sha256"] = _sha(payload)
    raw = _canonical(payload)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    except OSError as exc:
        raise CampaignOperatorError("cannot seal operator stage receipt") from exc
    return str(STAGE_RECEIPT), hashlib.sha256(raw).hexdigest()


def _cell(cell: Mapping[str, Any], *, ordinal: int, pair_index: int, arm: str) -> dict[str, Any]:
    required = {"ordinal", "pair_index", "arm", "run_id", "run_root", "target_host",
                "hard_timeout_seconds", "retry_policy"}
    if set(cell) != required:
        raise CampaignOperatorError(f"cell {ordinal} schema drifted")
    if (cell.get("ordinal"), cell.get("pair_index"), cell.get("arm")) != (ordinal, pair_index, arm):
        raise CampaignOperatorError(f"cell {ordinal} contradicts the frozen AB/BA schedule")
    run_id = cell.get("run_id")
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise CampaignOperatorError(f"cell {ordinal} run ID is invalid")
    run_root = _relative_child(cell.get("run_root"), f"cell {ordinal} run root")
    if (not isinstance(cell.get("target_host"), str) or not cell["target_host"].strip() or
            cell.get("hard_timeout_seconds") != _HARD_TIMEOUT_SECONDS or
            cell.get("retry_policy") != "none"):
        raise CampaignOperatorError(f"cell {ordinal} host, timeout, or retry contract drifted")
    return {
        "ordinal": ordinal, "pair_index": pair_index, "arm": arm, "run_id": run_id,
        "run_root": run_root, "target_host": cell["target_host"],
        "hard_timeout_seconds": _HARD_TIMEOUT_SECONDS, "retry_policy": "none",
    }


def validate_manifest(manifest: Mapping[str, Any], freeze: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate a complete no-launch W19 campaign manifest.

    Approval bytes and raw-clock windows are deliberately absent: neither is
    knowable before a particular cell is materialized on its target host.
    """
    try:
        evaluator._check_freeze(freeze)
    except Exception as exc:
        raise CampaignOperatorError("campaign freeze is invalid") from exc
    required = {"schema_version", "kind", "campaign_id", "freeze_sha256", "repository_revision",
                "prepared_utc", "cells", "manifest_sha256"}
    if set(manifest) != required or manifest.get("schema_version") != 1 or manifest.get("kind") != MANIFEST_KIND:
        raise CampaignOperatorError("campaign manifest schema drifted")
    if (manifest.get("campaign_id") != freeze.get("campaign_id") or
            manifest.get("freeze_sha256") != freeze.get("freeze_sha256") or
            manifest.get("repository_revision") != freeze.get("repository_revision")):
        raise CampaignOperatorError("campaign manifest is not bound to the supplied freeze")
    _utc(manifest.get("prepared_utc"), "manifest preparation timestamp")
    semantic = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if manifest.get("manifest_sha256") != _sha(semantic):
        raise CampaignOperatorError("campaign manifest SHA-256 does not recompute")
    cells = manifest.get("cells")
    if not isinstance(cells, list) or len(cells) != evaluator.PAIR_COUNT * 2:
        raise CampaignOperatorError("campaign manifest requires exactly twelve cells")
    parsed: list[dict[str, Any]] = []
    ordinal = 0
    for pair_index, order in enumerate(evaluator.FROZEN_PAIR_SCHEDULE, start=1):
        for arm in order:
            ordinal += 1
            value = cells[ordinal - 1]
            if not isinstance(value, Mapping):
                raise CampaignOperatorError(f"cell {ordinal} is not an object")
            parsed.append(_cell(value, ordinal=ordinal, pair_index=pair_index, arm=arm))
    if len({cell["run_id"] for cell in parsed}) != len(parsed):
        raise CampaignOperatorError("campaign manifest reuses a run ID")
    if len({cell["run_root"] for cell in parsed}) != len(parsed):
        raise CampaignOperatorError("campaign manifest reuses a run root, including any pilot root")
    if len({cell["target_host"] for cell in parsed}) != 1:
        raise CampaignOperatorError("campaign manifest does not retain one target host")
    return dict(freeze), parsed


def materialize_next_cell(
    manifest: Mapping[str, Any], freeze: Mapping[str, Any], *, ordinal: int,
    campaign_root: Path, window_start_monotonic_ns: int,
    window_end_monotonic_ns: int,
    prepare_kwargs: Mapping[str, Any],
    prior_validated_cells: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Materialize one absent cell and return its exact external-approval gate.

    ``prepare`` must be the existing no-launch
    ``sustained_role_local.prepare_production_dry_run`` (or a focused test
    double).  The caller obtains the raw-clock window immediately before this
    call.  No approval can be supplied here; it is bound later to the request
    generated below the new, exclusive cell root.
    """
    frozen, cells = validate_manifest(manifest, freeze)
    try:
        frozen_at = evaluator._check_freeze(frozen)
    except Exception as exc:
        raise CampaignOperatorError("campaign freeze is invalid") from exc
    if type(ordinal) is not int or ordinal not in range(1, len(cells) + 1):
        raise CampaignOperatorError("cell ordinal is outside the fixed campaign")
    if isinstance(prior_validated_cells, (str, bytes, Mapping)) or len(prior_validated_cells) != ordinal - 1:
        raise CampaignOperatorError("serial stage lacks exactly the preceding independently validated cells")
    root = _reject_lexical_symlink_ancestors(Path(campaign_root), "campaign root").resolve()
    if not root.is_dir():
        raise CampaignOperatorError("campaign root is not an owned regular directory")
    cell = cells[ordinal - 1]
    host_identity = _host_boot_identity()
    if host_identity["hostname"] != cell["target_host"]:
        raise CampaignOperatorError("local hostname differs from the frozen campaign target host")
    prior_accepted: list[Mapping[str, Any]] = []
    for previous_ordinal, previous in enumerate(prior_validated_cells, start=1):
        if not isinstance(previous, Mapping):
            raise CampaignOperatorError("prior cell validation record is not an object")
        expected = cells[previous_ordinal - 1]
        if set(previous) != {"pair_index", "ordinal", "arm", "root", "receipt_path", "receipt_sha256",
                             "stage_receipt_path", "stage_receipt_sha256"} or (
                previous.get("ordinal"), previous.get("arm")) != (expected["ordinal"], expected["arm"]):
            raise CampaignOperatorError("prior validation record is not bound to the frozen schedule")
        if previous.get("pair_index") != expected["pair_index"]:
            raise CampaignOperatorError("prior validation record has wrong pair index")
        prior_root = _lexical_child(root, expected["run_root"], "prior cell")
        if previous.get("root") != str(prior_root):
            raise CampaignOperatorError("prior validation record root differs from manifest root")
        try:
            evaluator._reject_negative_markers(prior_root, ordinal=expected["ordinal"])
        except Exception as exc:
            raise CampaignOperatorError("prior cell has a sealed negative marker; no progression") from exc
        receipt_relative = _relative_child(previous.get("receipt_path"), "prior receipt path")
        receipt = _lexical_child(prior_root, receipt_relative, "prior receipt")
        try:
            if not prior_root.is_dir() or not receipt.is_file():
                raise ValueError("unsafe prior root or receipt")
            receipt_bytes = receipt.read_bytes()
            if hashlib.sha256(receipt_bytes).hexdigest() != _hex(previous.get("receipt_sha256"), "prior receipt SHA-256"):
                raise ValueError("prior receipt hash drift")
            record = {"pair_index": expected["pair_index"], "ordinal": expected["ordinal"],
                      "arm": expected["arm"], "root": str(prior_root),
                      "receipt_path": str(receipt_relative), "receipt_sha256": previous["receipt_sha256"]}
            stage_record = {"stage_receipt_path": previous["stage_receipt_path"],
                            "stage_receipt_sha256": previous["stage_receipt_sha256"]}
            staged_request_sha = evaluator._stage_receipt(
                prior_root, stage_record, expected, manifest, frozen,
                required_host_identity=host_identity)
            raw_receipt = evaluator._strict_json(receipt_bytes, "prior raw receipt", canonical=True)
            artifacts = raw_receipt.get("artifacts")
            if not isinstance(artifacts, Mapping):
                raise ValueError("prior raw receipt lacks artifacts")
            _request_bytes, request_sha = evaluator._descriptor(
                prior_root, artifacts.get("authorization_request"), "prior authorization request", 256 * 1024)
            if request_sha != staged_request_sha:
                raise ValueError("prior stage receipt request differs from raw receipt request")
            accepted = evaluator._one_cell(
                record, expected_pair=expected["pair_index"], expected_ordinal=expected["ordinal"],
                expected_arm=expected["arm"], freeze=frozen, frozen_at=frozen_at)
            if (not isinstance(accepted, Mapping) or not isinstance(accepted.get("identity"), Mapping) or
                    not isinstance(accepted.get("scheduled_window"), Mapping)):
                raise ValueError("prior cell lacks campaign comparability identity")
            prior_accepted.append(accepted)
        except Exception as exc:
            raise CampaignOperatorError("prior cell did not pass independent validation") from exc
    if prior_accepted:
        identity = prior_accepted[0]["identity"]
        if any(accepted["identity"] != identity for accepted in prior_accepted[1:]):
            raise CampaignOperatorError("prior cells are not campaign-comparable")
        for earlier, later in zip(prior_accepted, prior_accepted[1:]):
            earlier_window, later_window = earlier["scheduled_window"], later["scheduled_window"]
            if (type(earlier_window.get("end_monotonic_ns")) is not int or
                    type(later_window.get("start_monotonic_ns")) is not int or
                    later_window["start_monotonic_ns"] < earlier_window["end_monotonic_ns"]):
                raise CampaignOperatorError("prior accepted cells overlap in raw-clock time")
    output = _lexical_child(root, cell["run_root"], "new cell")
    if output.exists():
        raise CampaignOperatorError("cell root already exists; no pilot or prior result may be reused")
    # The producer acquires the cell itself with an exclusive mkdir, but its
    # immutable manifest path is nested below a shared cells/ parent.
    try:
        output.parent.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise CampaignOperatorError("cannot create campaign cells parent") from exc
    if output.parent.is_symlink() or not output.parent.is_dir():
        raise CampaignOperatorError("campaign cells parent is not a regular directory")
    try:
        raw_before = _raw_clock()
    except Exception as exc:
        raise CampaignOperatorError("cannot sample CLOCK_MONOTONIC_RAW before materialization") from exc
    if type(raw_before) is not int:
        raise CampaignOperatorError("CLOCK_MONOTONIC_RAW sample is invalid")
    if (type(window_start_monotonic_ns) is not int or type(window_end_monotonic_ns) is not int or
            window_end_monotonic_ns - window_start_monotonic_ns < _MIN_WINDOW_NS):
        raise CampaignOperatorError("current raw-clock window cannot cover the frozen horizon")
    if (window_start_monotonic_ns < raw_before + _MIN_STAGE_RESERVE_NS or
            window_start_monotonic_ns > raw_before + _MAX_STAGE_LEAD_NS):
        raise CampaignOperatorError("raw-clock start is not fresh with sufficient staging reserve")
    if prior_accepted and window_start_monotonic_ns < prior_accepted[-1]["scheduled_window"]["end_monotonic_ns"]:
        raise CampaignOperatorError("new raw-clock window begins before the previous accepted cell ended")
    if not isinstance(prepare_kwargs, Mapping) or set(prepare_kwargs) - _ALLOWED_PREPARE_KWARGS:
        raise CampaignOperatorError("no-launch producer arguments are invalid")
    try:
        produced = local.prepare_production_dry_run(
            output, arm=cell["arm"], run_id=cell["run_id"],
            window_start_monotonic_ns=window_start_monotonic_ns,
            window_end_monotonic_ns=window_end_monotonic_ns,
            hard_timeout_seconds=_HARD_TIMEOUT_SECONDS, **dict(prepare_kwargs),
        )
    except Exception as exc:
        raise CampaignOperatorError("cell materialization failed without launch") from exc
    if not isinstance(produced, Mapping):
        _post_materialization_failure(output, manifest=manifest, freeze=frozen, cell=cell,
                                      detail="no-launch producer did not return a mapping")
    request_path = output / "runtime/sustained-role-authorization-request.json"
    if request_path.is_symlink() or not request_path.is_file():
        _post_materialization_failure(output, manifest=manifest, freeze=frozen, cell=cell,
                                      detail="no-launch producer did not seal the authorization request")
    try:
        request_bytes = request_path.read_bytes()
        request = json.loads(request_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _post_materialization_failure(output, manifest=manifest, freeze=frozen, cell=cell,
                                      detail="materialized authorization request is unreadable")
    if not isinstance(request, Mapping) or request_bytes != _canonical(request):
        _post_materialization_failure(output, manifest=manifest, freeze=frozen, cell=cell,
                                      detail="materialized authorization request is not canonical")
    plan_path = output / "runtime/sustained-role-execution-plan.json"
    try:
        plan_bytes = plan_path.read_bytes()
        plan = json.loads(plan_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _post_materialization_failure(output, manifest=manifest, freeze=frozen, cell=cell,
                                      detail="no-launch producer did not seal a readable execution plan")
    if (not isinstance(plan, Mapping) or plan_bytes != _canonical(plan) or
            plan.get("plan_sha256") != hashlib.sha256(_canonical(
                {key: value for key, value in plan.items() if key != "plan_sha256"})).hexdigest() or
            request.get("execution_plan_sha256") != plan.get("plan_sha256")):
        _post_materialization_failure(output, manifest=manifest, freeze=frozen, cell=cell,
                                      detail="materialized request is not bound to its sealed execution plan")
    request_sha256 = hashlib.sha256(request_bytes).hexdigest()
    if produced.get("authorization_request_sha256") != request_sha256:
        _post_materialization_failure(output, manifest=manifest, freeze=frozen, cell=cell,
                                      detail="no-launch producer request digest drifted")
    expected_window = {
        "start_monotonic_ns": window_start_monotonic_ns,
        "end_monotonic_ns": window_end_monotonic_ns,
        "argv_pinned_before_launch": True,
        "attestation": {
            "must_be_written": "after_prearm_all_seven_e0_common_commit_before_scheduled_start",
            "is_not": "an_arm_or_gate",
        },
    }
    if (request.get("arm") != cell["arm"] or request.get("no_retry") is not True or
            request.get("hard_timeout_seconds") != _HARD_TIMEOUT_SECONDS or
            request.get("scheduled_window") != expected_window or
            plan.get("scheduled_window") != expected_window):
        _post_materialization_failure(output, manifest=manifest, freeze=frozen, cell=cell,
                                      detail="materialized request drifts from the frozen cell contract")
    try:
        raw_after = _raw_clock()
    except Exception as exc:
        _post_materialization_failure(output, manifest=manifest, freeze=frozen, cell=cell,
                                      detail="cannot sample CLOCK_MONOTONIC_RAW after materialization")
    if type(raw_after) is not int or raw_after < raw_before:
        _post_materialization_failure(output, manifest=manifest, freeze=frozen, cell=cell,
                                      detail="CLOCK_MONOTONIC_RAW regressed during materialization")
    if window_start_monotonic_ns < raw_after + _MIN_POST_MATERIALIZATION_RESERVE_NS:
        _post_materialization_failure(output, manifest=manifest, freeze=frozen, cell=cell,
                                      detail="materialization consumed the required pre-launch raw-clock reserve")
    if plan.get("repository_revision") != frozen["repository_revision"]:
        _post_materialization_failure(output, manifest=manifest, freeze=frozen, cell=cell,
                                      detail="materialized plan revision differs from the campaign freeze")
    stage_window = {"start_monotonic_ns": window_start_monotonic_ns,
                    "end_monotonic_ns": window_end_monotonic_ns}
    try:
        stage_path, stage_sha256 = _seal_stage_receipt(
            output, manifest=manifest, freeze=frozen, cell=cell, host_identity=host_identity,
            request_sha256=request_sha256, scheduled_window=stage_window)
    except CampaignOperatorError as exc:
        _post_materialization_failure(output, manifest=manifest, freeze=frozen, cell=cell, detail=str(exc))
    return {
        "schema_version": 1, "kind": STAGE_KIND,
        "state": "MATERIALIZED_NO_LAUNCH_EXTERNAL_EXACT_APPROVAL_REQUIRED",
        "campaign_id": frozen["campaign_id"], "freeze_sha256": frozen["freeze_sha256"],
        "ordinal": cell["ordinal"], "pair_index": cell["pair_index"], "arm": cell["arm"],
        "run_id": cell["run_id"], "run_root": str(output),
        "target_host": cell["target_host"], "host_provenance": host_identity, "clock": _CLOCK,
        "scheduled_window": stage_window,
        "hard_timeout_seconds": _HARD_TIMEOUT_SECONDS, "no_retry": True,
        "authorization_request": {"path": str(request_path), "sha256": request_sha256,
                                  "must_bind": "external-exact-per-cell-approval"},
        "stage_receipt": {"path": stage_path, "sha256": stage_sha256},
        "launch_permitted": False,
        "next_required_action": "create external exact approval from this request, then use the arm-specific one-shot launcher once",
        "claim_eligible": False, "figure_eligible": False,
    }
