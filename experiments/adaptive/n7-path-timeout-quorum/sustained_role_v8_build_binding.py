"""No-launch binding of all archived v8 preparation binaries to a W19 receipt."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import re
from typing import Any, Mapping


HERE = Path(__file__).resolve().parent
KIND = "kauri-n7-sustained-role-v8-build-binding-v1"
_REVISION = re.compile(r"[0-9a-f]{40}")
_BINARY_ARTIFACTS = {
    "hotstuff_app": "hotstuff_app",
    "adaptation_manager": "adaptation_manager",
    "hotstuff_keygen": "hotstuff_keygen",
    "hotstuff_tls_keygen": "hotstuff_tls_keygen",
    "epoch0_treefile_digest": "e0_helper",
}


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load W19 clean-build receipt schema")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


operator = _load("w19_v8_build_operator", HERE / "sustained_role_campaign_operator.py")


class V8BuildBindingError(ValueError):
    """The archived no-launch closure differs from the external receipt."""


def _regular_descriptor(root: Path, descriptor: Mapping[str, object], *, label: str,
                        maximum_bytes: int) -> tuple[Path, bytes]:
    if set(descriptor) != {"path", "sha256", "size_bytes"}:
        raise V8BuildBindingError(f"{label} descriptor drifted")
    relative = descriptor.get("path")
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise V8BuildBindingError(f"{label} path is unsafe")
    path = root
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink():
            raise V8BuildBindingError(f"{label} traverses a symlink")
    try:
        if not path.is_file() or path.stat().st_size > maximum_bytes:
            raise V8BuildBindingError(f"{label} is not a bounded regular file")
        raw = path.read_bytes()
    except OSError as exc:
        raise V8BuildBindingError(f"cannot read {label}") from exc
    if descriptor["size_bytes"] != len(raw) or descriptor["sha256"] != hashlib.sha256(raw).hexdigest():
        raise V8BuildBindingError(f"{label} descriptor hash drifted")
    return path, raw


def bind(*, root: Path, plan: Mapping[str, object], observed_revision: str,
         target_host: str, linux_boot_id: str) -> dict[str, object]:
    """Compare all five archived preparation binaries to a strict receipt.

    The observed values are comparison inputs for a later authenticated
    host/booking join; this function does not attest them and cannot make a
    run launch eligible.
    """
    supplied_root = Path(root)
    if (not supplied_root.is_absolute() or supplied_root.is_symlink() or not supplied_root.is_dir() or
            _REVISION.fullmatch(observed_revision) is None or not isinstance(target_host, str) or
            not target_host.strip() or not isinstance(linux_boot_id, str) or not linux_boot_id):
        raise V8BuildBindingError("expected root or observed build identity is malformed")
    root = supplied_root.resolve()
    if plan.get("repository_revision") != observed_revision:
        raise V8BuildBindingError("observed revision differs from plan")
    artifacts = plan.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise V8BuildBindingError("plan artifacts are absent")
    receipt_descriptor = artifacts.get("build_receipt")
    if not isinstance(receipt_descriptor, Mapping):
        raise V8BuildBindingError("archived build receipt descriptor is absent")
    receipt_path, raw = _regular_descriptor(
        root, receipt_descriptor, label="archived build receipt",
        maximum_bytes=operator._MAX_BUILD_PROVENANCE_BYTES)
    try:
        checked = operator.read_w19_build_provenance(
            receipt_path, repository_revision=observed_revision, target_host=target_host)
        receipt = operator._build_receipt(
            checked, repository_revision=observed_revision, target_host=target_host)
    except Exception as exc:
        raise V8BuildBindingError("strict W19 build receipt rejected") from exc
    if receipt["linux_boot_id"] != linux_boot_id:
        raise V8BuildBindingError("build receipt boot identity differs")

    joined: dict[str, dict[str, object]] = {}
    for receipt_name, artifact_name in _BINARY_ARTIFACTS.items():
        descriptor = artifacts.get(artifact_name)
        if not isinstance(descriptor, Mapping):
            raise V8BuildBindingError(f"missing archived binary descriptor: {artifact_name}")
        _path, binary = _regular_descriptor(
            root, descriptor, label=f"archived binary {receipt_name}", maximum_bytes=128 * 1024 * 1024)
        expected = receipt["binaries"][receipt_name]
        if len(binary) != expected["size_bytes"] or hashlib.sha256(binary).hexdigest() != expected["sha256"]:
            raise V8BuildBindingError("build receipt binary differs from materialized archive")
        joined[receipt_name] = {"sha256": expected["sha256"], "size_bytes": expected["size_bytes"],
                                "path": descriptor["path"]}
    return {
        "kind": KIND,
        "state": "BUILD_COMPONENT_VERIFIED_NOT_LIVE_ATTESTED",
        "repository_revision": observed_revision,
        "target_host": target_host,
        "linux_boot_id": linux_boot_id,
        "receipt_sha256": hashlib.sha256(raw).hexdigest(),
        "binaries": joined,
        "live_attested": False,
        "launch_eligible": False,
    }
