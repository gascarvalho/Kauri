#!/usr/bin/env python3
"""Create and stage, but never launch, a frozen W19 serial campaign.

The two commands deliberately stop before the external approval boundary.
``freeze`` creates an exclusive campaign root containing canonical freeze and
manifest documents. ``stage`` materializes exactly one scheduled cell and
prints its request digest. Neither command invokes a launcher, writes an
approval, retries a cell, or marks a result eligible for a claim or figure.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence


HERE = Path(__file__).resolve().parent
KAURI = HERE.parents[2]
FREEZE_NAME = "campaign-freeze.json"
MANIFEST_NAME = "campaign-manifest.json"
_CAMPAIGN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_CELL_WINDOW_NS = 70_000_000_000
_STAGE_LEAD_NS = 90_000_000_000
_REPLICA_PORT_SPAN = 7


class CampaignPreflightError(ValueError):
    pass


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    if spec is None or spec.loader is None:
        raise CampaignPreflightError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


evaluator = _load("kauri_w19_campaign_evaluator_preflight", "sustained_role_campaign_evaluator.py")
operator = _load("kauri_w19_campaign_operator_preflight", "sustained_role_campaign_operator.py")


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("ascii") + b"\n"
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CampaignPreflightError("document is not canonical ASCII JSON") from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_root(path: Path, label: str) -> Path:
    if ".." in Path(path).parts:
        raise CampaignPreflightError(f"{label} may not traverse parent directories")
    try:
        return operator._reject_lexical_symlink_ancestors(Path(path), label).resolve(strict=False)
    except Exception as exc:
        raise CampaignPreflightError(f"cannot validate {label}") from exc


def _write_new(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise CampaignPreflightError(f"refusing to overwrite {path.name}")
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise CampaignPreflightError(f"cannot create {path.name}") from exc


def _read_canonical(path: Path, label: str) -> Any:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise CampaignPreflightError(f"{label} is not a bounded regular file")
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CampaignPreflightError(f"{label} is not readable canonical JSON") from exc
    if raw != _canonical(value):
        raise CampaignPreflightError(f"{label} is not canonical JSON")
    return value


def _campaign_id(value: str) -> str:
    if _CAMPAIGN_ID.fullmatch(value) is None:
        raise CampaignPreflightError("campaign ID must be a short safe identifier")
    return value


def _git_clean_pinned(revision: str, runner: callable = subprocess.run) -> None:
    """Check the fixed, clean branch and its pushed tracking revision before any write."""
    branch = "feature/adaptive-epoch-throughput"
    def read(*args: str) -> str:
        completed = runner(["git", "-C", str(KAURI), *args], text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if completed.returncode != 0:
            raise CampaignPreflightError("cannot verify the pinned pushed Kauri checkout")
        return completed.stdout.strip()
    if (read("branch", "--show-current") != branch or
            read("rev-parse", "HEAD") != revision or
            read("rev-parse", f"refs/remotes/origin/{branch}") != revision or
            read("status", "--porcelain=v1")):
        raise CampaignPreflightError("Kauri branch, clean HEAD, or pushed revision differs from freeze")


def _manifest(freeze: Mapping[str, Any], *, target_host: str, prepared_utc: str) -> dict[str, Any]:
    if not isinstance(target_host, str) or not target_host.strip() or any(item.isspace() for item in target_host):
        raise CampaignPreflightError("target host is invalid")
    try:
        operator._utc(prepared_utc, "manifest preparation timestamp")
    except Exception as exc:
        raise CampaignPreflightError("manifest preparation timestamp is invalid") from exc
    cells: list[dict[str, Any]] = []
    ordinal = 0
    for pair_index, order in enumerate(evaluator.FROZEN_PAIR_SCHEDULE, start=1):
        for arm in order:
            ordinal += 1
            cells.append({
                "ordinal": ordinal, "pair_index": pair_index, "arm": arm,
                "run_id": f"{freeze['campaign_id']}-cell-{ordinal:02d}",
                "run_root": f"cells/cell-{ordinal:02d}", "target_host": target_host,
                "hard_timeout_seconds": 210, "retry_policy": "none",
            })
    manifest: dict[str, Any] = {
        "schema_version": 1, "kind": operator.MANIFEST_KIND,
        "campaign_id": freeze["campaign_id"], "freeze_sha256": freeze["freeze_sha256"],
        "repository_revision": freeze["repository_revision"], "prepared_utc": prepared_utc,
        "cells": cells,
    }
    manifest["manifest_sha256"] = hashlib.sha256(_canonical(manifest)).hexdigest()
    try:
        operator.validate_manifest(manifest, freeze)
    except Exception as exc:
        raise CampaignPreflightError("generated campaign manifest did not validate") from exc
    return manifest


def create_campaign(*, campaign_root: Path, campaign_id: str, approval_reference: str,
                    repository_revision: str, target_host: str, frozen_utc: str | None = None) -> dict[str, Any]:
    """Create a root plus the only two immutable pre-launch documents."""
    root = _safe_root(campaign_root, "campaign root")
    if root.exists():
        raise CampaignPreflightError("campaign root already exists; no campaign inputs may be replaced")
    campaign_id = _campaign_id(campaign_id)
    if not isinstance(approval_reference, str) or not approval_reference.strip():
        raise CampaignPreflightError("campaign approval reference is empty")
    frozen_utc = frozen_utc or _utc_now()
    try:
        freeze = evaluator.build_campaign_freeze(
            campaign_id=campaign_id, frozen_utc=frozen_utc,
            campaign_approval_reference=approval_reference,
            repository_revision=repository_revision,
        )
    except Exception as exc:
        raise CampaignPreflightError("campaign freeze inputs are invalid") from exc
    manifest = _manifest(freeze, target_host=target_host, prepared_utc=_utc_now())
    _git_clean_pinned(repository_revision)
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
    except OSError as exc:
        raise CampaignPreflightError("cannot create exclusive campaign root") from exc
    try:
        _write_new(root / FREEZE_NAME, _canonical(freeze))
        _write_new(root / MANIFEST_NAME, _canonical(manifest))
    except Exception:
        # Preserve a partial root rather than silently deleting evidence of a
        # failed freeze attempt. It can never be reused by this tool.
        raise
    return {"state": "FROZEN_NO_LAUNCH", "campaign_root": str(root),
            "freeze_path": str(root / FREEZE_NAME), "freeze_sha256": freeze["freeze_sha256"],
            "manifest_path": str(root / MANIFEST_NAME), "manifest_sha256": manifest["manifest_sha256"],
            "cell_count": len(manifest["cells"]), "launch_permitted": False,
            "claim_eligible": False, "figure_eligible": False}


def _port_bases(ordinal: int, peer_port: int | None, client_port: int | None,
                manager_port: int | None) -> dict[str, int]:
    if type(ordinal) is not int or ordinal not in range(1, evaluator.PAIR_COUNT * 2 + 1):
        raise CampaignPreflightError("cell ordinal is outside the frozen campaign")
    offset = (ordinal - 1) * 32
    ports = {"peer_port": 18000 + offset if peer_port is None else peer_port,
             "client_port": 19000 + offset if client_port is None else client_port,
             "manager_port": 20000 + offset if manager_port is None else manager_port}
    values = list(ports.values())
    if any(type(port) is not int or port <= 1024 or port + _REPLICA_PORT_SPAN - 1 > 65535 for port in values):
        raise CampaignPreflightError("port bases cannot provide seven legal replica ports")
    ranges = [set(range(port, port + _REPLICA_PORT_SPAN)) for port in values]
    if any(left & right for index, left in enumerate(ranges) for right in ranges[index + 1:]):
        raise CampaignPreflightError("peer, client, and manager port ranges overlap")
    return ports


def _binary(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise CampaignPreflightError(f"{label} is not a regular file")
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise CampaignPreflightError(f"cannot inspect {label}") from exc
    if not stat.S_ISREG(mode) or not os.access(path, os.X_OK):
        raise CampaignPreflightError(f"{label} is not executable")
    return path.resolve(strict=True)


def _prior_records(path: Path | None, ordinal: int) -> list[Mapping[str, Any]]:
    if ordinal == 1:
        if path is not None:
            value = _read_canonical(path, "prior records")
            if value != []:
                raise CampaignPreflightError("first cell cannot have prior records")
        return []
    if path is None:
        raise CampaignPreflightError("later serial cells require canonical prior launch records")
    value = _read_canonical(path, "prior records")
    if not isinstance(value, list) or len(value) != ordinal - 1 or not all(isinstance(item, Mapping) for item in value):
        raise CampaignPreflightError("prior launch records do not match the preceding serial cells")
    return list(value)


def stage_campaign_cell(*, campaign_root: Path, ordinal: int, prior_records: Path | None,
                        peer_port: int | None = None, client_port: int | None = None,
                        manager_port: int | None = None, app_binary: Path | None = None,
                        manager_binary: Path | None = None, keygen_binary: Path | None = None,
                        tls_keygen_binary: Path | None = None, e0_helper_binary: Path | None = None,
                        raw_clock: callable = time.clock_gettime_ns) -> dict[str, Any]:
    """Materialize precisely one fresh cell and return its external approval gate."""
    root = _safe_root(campaign_root, "campaign root")
    if not root.is_dir():
        raise CampaignPreflightError("campaign root is unavailable")
    freeze = _read_canonical(root / FREEZE_NAME, "campaign freeze")
    manifest = _read_canonical(root / MANIFEST_NAME, "campaign manifest")
    if not isinstance(freeze, Mapping) or not isinstance(manifest, Mapping):
        raise CampaignPreflightError("frozen campaign documents are not objects")
    try:
        operator.validate_manifest(manifest, freeze)
    except Exception as exc:
        raise CampaignPreflightError("frozen campaign documents do not validate") from exc
    _git_clean_pinned(freeze["repository_revision"])
    records = _prior_records(prior_records, ordinal)
    ports = _port_bases(ordinal, peer_port, client_port, manager_port)
    defaults = {
        "app_binary": KAURI / "build-adaptive/examples/hotstuff-app",
        "manager_binary": KAURI / "build-adaptive/examples/adaptation-manager",
        "keygen_binary": KAURI / "build-adaptive/hotstuff-keygen",
        "tls_keygen_binary": KAURI / "build-adaptive/hotstuff-tls-keygen",
        "e0_helper_binary": KAURI / "build-adaptive/examples/n7-epoch0-treefile-digest",
    }
    supplied = {"app_binary": app_binary, "manager_binary": manager_binary,
                "keygen_binary": keygen_binary, "tls_keygen_binary": tls_keygen_binary,
                "e0_helper_binary": e0_helper_binary}
    binaries = {key: _binary(value if value is not None else defaults[key], key.replace("_", " "))
                for key, value in supplied.items()}
    try:
        now = raw_clock(time.CLOCK_MONOTONIC_RAW)
    except Exception as exc:
        raise CampaignPreflightError("cannot sample CLOCK_MONOTONIC_RAW") from exc
    if type(now) is not int or now <= 0:
        raise CampaignPreflightError("CLOCK_MONOTONIC_RAW sample is invalid")
    start = now + _STAGE_LEAD_NS
    result = operator.materialize_next_cell(
        manifest, freeze, ordinal=ordinal, campaign_root=root,
        window_start_monotonic_ns=start, window_end_monotonic_ns=start + _CELL_WINDOW_NS,
        prepare_kwargs={**ports, **binaries}, prior_validated_cells=records,
    )
    if not isinstance(result, Mapping) or result.get("launch_permitted") is not False:
        raise CampaignPreflightError("operator returned an invalid pre-launch state")
    return dict(result)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze", help="create one immutable no-launch campaign root")
    freeze.add_argument("--campaign-root", type=Path, required=True)
    freeze.add_argument("--campaign-id", required=True)
    freeze.add_argument("--approval-reference", required=True)
    freeze.add_argument("--repository-revision", required=True)
    freeze.add_argument("--target-host", required=True)
    freeze.add_argument("--frozen-utc")
    stage = commands.add_parser("stage", help="materialize one fresh cell without launch")
    stage.add_argument("--campaign-root", type=Path, required=True)
    stage.add_argument("--ordinal", type=int, required=True)
    stage.add_argument("--prior-records", type=Path)
    for option in ("peer", "client", "manager"):
        stage.add_argument(f"--{option}-port", type=int)
    stage.add_argument("--app-binary", type=Path)
    stage.add_argument("--manager-binary", type=Path)
    stage.add_argument("--keygen-binary", type=Path)
    stage.add_argument("--tls-keygen-binary", type=Path)
    stage.add_argument("--e0-helper-binary", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "freeze":
            result = create_campaign(campaign_root=args.campaign_root, campaign_id=args.campaign_id,
                                     approval_reference=args.approval_reference,
                                     repository_revision=args.repository_revision,
                                     target_host=args.target_host, frozen_utc=args.frozen_utc)
        else:
            result = stage_campaign_cell(
                campaign_root=args.campaign_root, ordinal=args.ordinal, prior_records=args.prior_records,
                peer_port=args.peer_port, client_port=args.client_port, manager_port=args.manager_port,
                app_binary=args.app_binary, manager_binary=args.manager_binary,
                keygen_binary=args.keygen_binary, tls_keygen_binary=args.tls_keygen_binary,
                e0_helper_binary=args.e0_helper_binary)
    except (CampaignPreflightError, OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
