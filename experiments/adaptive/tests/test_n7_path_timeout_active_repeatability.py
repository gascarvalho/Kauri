from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "n7-path-timeout-quorum" / "repeatability" / "collector.py"
spec = importlib.util.spec_from_file_location("n7_active_repeatability_test", PATH)
assert spec and spec.loader
repeatability = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repeatability)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"


def _write(path: Path, value: object) -> str:
    raw = _canonical(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _manifest():
    manifest = repeatability.load_manifest()
    manifest["manifest_sha256"] = repeatability.manifest_digest(manifest)
    return manifest


def _scheduled(manifest, slot_id: str):
    _, root_id, _ = next(item for item in repeatability._EXPECTED_SLOTS if item[0] == slot_id)
    return {"slot_id": slot_id, "root_id": root_id, "manifest_sha256": manifest["manifest_sha256"]}


def _terminal_root(tmp_path: Path, manifest, slot_id: str, *, status: str = "ABORTED", run_id: str | None = None) -> Path:
    root = tmp_path / slot_id
    run_id = run_id or f"source-{slot_id.lower()}"
    authorization = root / "runtime/approved-execution-authorization.json"
    authorization.parent.mkdir(parents=True, exist_ok=True)
    authorization.write_bytes(b"authorized source bytes")
    artifact_path = {
        "RAW_BUNDLE_VALIDATED": "raw-bundle-verdict.json",
        "ABORTED": "local-run-abort.json",
        "INCOMPLETE": "local-run-incomplete.json",
    }[status]
    artifact_digest = _write(root / artifact_path, {"run_id": run_id, "status": status})
    _, root_id, _ = next(item for item in repeatability._EXPECTED_SLOTS if item[0] == slot_id)
    _write(root / "repeatability-terminal-receipt.json", {
        "schema_version": 1,
        "kind": "n7-active-repeatability-terminal-v1",
        "study_id": manifest["study_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "slot_id": slot_id,
        "source_root_id": root_id,
        "source_run_id": run_id,
        "status": status,
        "execution_authorization_sha256": hashlib.sha256(authorization.read_bytes()).hexdigest(),
        "terminal_artifact_path": artifact_path,
        "terminal_artifact_sha256": artifact_digest,
    })
    return root


def test_three_slot_active_only_freeze_is_valid_and_no_launch():
    result = repeatability.validate_manifest(_manifest())
    assert result == {"verdict": "NON_EVIDENTIARY_REPEATABILITY_PLAN_VALID", "study_id": "n7-path-local-timeout-quorum-v4-active-repeatability-v1", "scheduled_slots": 3}


@pytest.mark.parametrize("mutation", ["profile", "seed", "tree", "actor", "timeout", "retry", "replacement", "root"])
def test_freeze_rejects_v4_input_or_no_retry_drift(mutation: str):
    manifest = _manifest()
    if mutation == "profile":
        manifest["frozen_profile"]["profile_sha256"] = "a" * 64
    elif mutation == "seed":
        manifest["frozen_profile"]["snapshot_seed"] = 41720
    elif mutation == "tree":
        manifest["frozen_profile"]["tree_ids"] = [0, 1, 2]
    elif mutation == "actor":
        manifest["frozen_profile"]["omitting_replica"] = 2
    elif mutation == "timeout":
        manifest["execution_policy"]["hard_timeout_seconds"] = 1
    elif mutation == "retry":
        manifest["execution_policy"]["no_retry"] = False
    elif mutation == "replacement":
        manifest["execution_policy"]["replacement_runs"] = True
    else:
        manifest["slots"][1]["root_id"] = manifest["slots"][0]["root_id"]
    manifest["manifest_sha256"] = repeatability.manifest_digest(manifest)
    with pytest.raises(repeatability.ValidationError):
        repeatability.validate_manifest(manifest)


def test_missing_slot_is_rejected_from_fixed_denominator(tmp_path: Path):
    manifest = _manifest()
    roots = {"R1": _terminal_root(tmp_path, manifest, "R1"), "R2": _terminal_root(tmp_path, manifest, "R2")}
    with pytest.raises(repeatability.ValidationError, match="all three"):
        repeatability.collect_terminal_slots(manifest, [_scheduled(manifest, "R1"), _scheduled(manifest, "R2")], run_roots=roots, archive_roots={})


def test_failed_slots_are_source_receipt_derived_and_retained(tmp_path: Path):
    manifest = _manifest()
    roots = {
        "R1": _terminal_root(tmp_path, manifest, "R1", status="ABORTED"),
        "R2": _terminal_root(tmp_path, manifest, "R2", status="INCOMPLETE"),
        "R3": _terminal_root(tmp_path, manifest, "R3", status="ABORTED"),
    }
    result = repeatability.collect_terminal_slots(manifest, [_scheduled(manifest, "R1"), _scheduled(manifest, "R2"), _scheduled(manifest, "R3")], run_roots=roots, archive_roots={})
    assert result["verdict"] == "REPEATABILITY_DENOMINATOR_RETAINED"
    assert [item["status"] for item in result["terminal_slots"]] == ["ABORTED", "INCOMPLETE", "ABORTED"]
    assert "all_raw_bundles_validated" not in result


def test_cloned_source_run_is_rejected_even_if_slot_labels_differ(tmp_path: Path):
    manifest = _manifest()
    roots = {
        "R1": _terminal_root(tmp_path, manifest, "R1", run_id="cloned-accepted-run"),
        "R2": _terminal_root(tmp_path, manifest, "R2", run_id="cloned-accepted-run"),
        "R3": _terminal_root(tmp_path, manifest, "R3", run_id="fresh-r3"),
    }
    slots = [_scheduled(manifest, "R1"), _scheduled(manifest, "R2"), _scheduled(manifest, "R3")]
    with pytest.raises(repeatability.ValidationError, match="source run IDs must be unique"):
        repeatability.collect_terminal_slots(manifest, slots, run_roots=roots, archive_roots={})


def test_forged_abort_status_or_terminal_artifact_is_rejected(tmp_path: Path):
    manifest = _manifest()
    roots = {slot_id: _terminal_root(tmp_path, manifest, slot_id) for slot_id in ("R1", "R2", "R3")}
    _write(roots["R2"] / "local-run-abort.json", {"run_id": "source-r2", "status": "INCOMPLETE"})
    slots = [_scheduled(manifest, "R1"), _scheduled(manifest, "R2"), _scheduled(manifest, "R3")]
    with pytest.raises(repeatability.ValidationError, match="does not bind the actual terminal artifact"):
        repeatability.collect_terminal_slots(manifest, slots, run_roots=roots, archive_roots={})


def test_archive_receipt_requires_actual_archive_manifest_bytes(tmp_path: Path):
    manifest = _manifest()
    archive = tmp_path / "archive"
    archive.mkdir()
    archive_manifest_digest = _write(archive / "archive-file-manifest.json", {"files": ["raw-bundle-receipt.json"]})
    _write(archive / "repeatability-archive-receipt.json", {
        "schema_version": 1,
        "kind": "n7-active-repeatability-archive-v1",
        "study_id": manifest["study_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "slot_id": "R1",
        "source_root_id": "n7-path-quorum-v4-repeatability-r1",
        "source_run_id": "source-r1",
        "source_receipt_sha256": "a" * 64,
        "source_verdict_sha256": "b" * 64,
        "archive_root_id": "n7-path-quorum-v4-repeatability-r1-archive",
        "archive_file_manifest_sha256": archive_manifest_digest,
    })
    _write(archive / "archive-file-manifest.json", {"files": ["forged"]})
    with pytest.raises(repeatability.ValidationError, match="actual archive file manifest"):
        repeatability._validate_archive_receipt(archive, manifest=manifest, slot_id="R1", root_id="n7-path-quorum-v4-repeatability-r1", archive_root_id="n7-path-quorum-v4-repeatability-r1-archive", run_id="source-r1", receipt_sha256="a" * 64, verdict_sha256="b" * 64)


def test_terminal_slot_cannot_drift_root_or_manifest_binding(tmp_path: Path):
    manifest = _manifest()
    roots = {slot_id: _terminal_root(tmp_path, manifest, slot_id) for slot_id in ("R1", "R2", "R3")}
    slots = [_scheduled(manifest, "R1"), _scheduled(manifest, "R2"), _scheduled(manifest, "R3")]
    broken = deepcopy(slots)
    broken[1]["root_id"] = "replacement-root"
    with pytest.raises(repeatability.ValidationError, match="frozen root"):
        repeatability.collect_terminal_slots(manifest, broken, run_roots=roots, archive_roots={})
    broken = deepcopy(slots)
    broken[2]["manifest_sha256"] = "a" * 64
    with pytest.raises(repeatability.ValidationError, match="manifest binding"):
        repeatability.collect_terminal_slots(manifest, broken, run_roots=roots, archive_roots={})
