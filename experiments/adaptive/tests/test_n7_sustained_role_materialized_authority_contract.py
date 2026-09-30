"""No-launch integration boundary for W19 materialized authority inputs.

This test intentionally invokes only the key generators and the read-only E0
identity helper.  It never starts a replica, a client, or an adaptation
manager.  Its purpose is to prove that the production materializer, launcher,
and independent validator agree on the same archived E0 authority bytes.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ADAPTIVE = Path(__file__).resolve().parents[1]
STUDY = ADAPTIVE / "n7-path-timeout-quorum"
KAURI = ADAPTIVE.parents[1]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, STUDY / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


local = _load("w19_materialized_authority_local", "sustained_role_local.py")
fixed = _load("w19_materialized_authority_fixed", "sustained_role_fixed_e0_launcher.py")
adaptive = _load("w19_materialized_authority_adaptive", "sustained_role_adaptive_e1_launcher.py")
validator = _load("w19_materialized_authority_validator", "sustained_role_validator.py")


def _built_binaries() -> dict[str, Path]:
    build = KAURI / "build-adaptive"
    paths = {
        "app_binary": build / "examples/hotstuff-app",
        "manager_binary": build / "examples/adaptation-manager",
        "keygen_binary": build / "hotstuff-keygen",
        "tls_keygen_binary": build / "hotstuff-tls-keygen",
        "e0_helper_binary": build / "examples/n7-epoch0-treefile-digest",
    }
    absent = [str(path) for path in paths.values() if not path.is_file()]
    if absent:
        pytest.skip("W19 no-launch integration build artifacts are unavailable: " + ", ".join(absent))
    return paths


def _descriptor(root: Path, path: Path) -> dict[str, str]:
    return {
        "path": str(path.relative_to(root)),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def test_materialized_e0_authority_reopens_identically_in_launcher_and_validator(tmp_path: Path) -> None:
    """Exercise the real no-launch materializer through its sealed plan bridge."""
    binaries = _built_binaries()
    root = tmp_path / "w19-materialized"
    start = 1_000_000_000
    result = local.prepare_materialized_dry_run(
        root,
        arm="adaptive_e1",
        materialize=lambda owned: local.materialize_runtime_inputs(
            owned,
            arm="adaptive_e1",
            run_id="w19-no-launch-authority-contract",
            peer_port=18171,
            client_port=19171,
            manager_port=20171,
            hard_timeout_seconds=180,
            **binaries,
        ),
        window_start_monotonic_ns=start,
        window_end_monotonic_ns=(start + local.profile.COMMON_HORIZON_NS
                                 + local.profile.MINIMUM_POST_START_ANCHOR_SLACK_NS),
        repository_snapshot=lambda _path: SimpleNamespace(revision="a" * 40, worktree_clean=True),
        native_mode_revision_check=lambda _revision: True,
    )

    assert result["state"] == "PREPARED_DRY_RUN_EXTERNAL_APPROVAL_REQUIRED"
    assert not (root / "raw").exists()
    assert not (root / "logs").exists()

    plan = json.loads((root / local.PLAN).read_text(encoding="utf-8"))
    authority = json.loads((root / "local-launch-plan.json").read_text(encoding="utf-8"))
    e0_path, issuer_path = adaptive._materialized_authorities(root)
    e0_digest, source_e0_path = fixed._source_e0(root)
    helper_path = fixed._e0_identity_helper(root, source_e0_path)

    assert e0_path == source_e0_path
    assert issuer_path == root / authority["issuer_public_key"]
    assert helper_path.is_relative_to(root)
    assert plan["commands"]["manager"]["argv"][0].startswith(str(root))
    assert all(item["argv"][0].startswith(str(root)) for item in plan["commands"]["replicas"])
    manager_argv = plan["commands"]["manager"]["argv"]
    transition = manager_argv[manager_argv.index("--transition-request") + 1]
    arm_request_sha = manager_argv[manager_argv.index("--fault-window-arm-request-sha256") + 1]
    assert arm_request_sha == hashlib.sha256(transition.encode("utf-8")).hexdigest()

    artifacts = {
        "epoch0_tree": _descriptor(root, root / "config/epoch0.tree"),
        "e0_identity_receipt": _descriptor(root, e0_path),
        "e0_identity_helper": _descriptor(root, helper_path),
    }
    reopened = validator._validate_source_derived_e0(
        root, artifacts, expected_digest=e0_digest,
    )
    assert reopened["epoch_digest"] == e0_digest
    assert reopened["tree_file"] == "config/epoch0.tree"
    assert reopened["tree_file_sha256"] == plan["epoch0"]["tree"]["sha256"]
