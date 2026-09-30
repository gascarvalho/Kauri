from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "n7-path-timeout-quorum" / "sustained_role_campaign_operator.py"
SPEC = importlib.util.spec_from_file_location("n7_sustained_role_campaign_operator", MODULE)
assert SPEC is not None and SPEC.loader is not None
subject = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = subject
SPEC.loader.exec_module(subject)
REAL_VERIFY_FROZEN_BUILD = subject._verify_frozen_build_provenance
REAL_POST_MATERIALIZATION_BUILD = subject._post_materialization_build_binding


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"


def _freeze() -> dict[str, object]:
    return subject.evaluator.build_campaign_freeze(
        campaign_id="w19-serial", frozen_utc="2026-09-30T12:00:00Z",
        campaign_approval_reference="user-approval-ref", repository_revision="a" * 40,
        build_provenance={
            "raw_sha256": "a" * 64, "repository_revision": "a" * 40,
            "repository_branch": "feature/adaptive-epoch-throughput",
            "host_identity": {"hostname": "proteina02", "linux_boot_id": "12345678-1234-1234-1234-123456789abc"},
            "binary_sha256": {name: "a" * 64 for name in subject._BUILD_BINARY_NAMES},
        },
    )


def _manifest(freeze: dict[str, object]) -> dict[str, object]:
    cells = []
    ordinal = 0
    for pair, order in enumerate(subject.evaluator.FROZEN_PAIR_SCHEDULE, start=1):
        for arm in order:
            ordinal += 1
            cells.append({
                "ordinal": ordinal, "pair_index": pair, "arm": arm,
                "run_id": f"w19-cell-{ordinal:02d}", "run_root": f"cells/cell-{ordinal:02d}",
                "target_host": "proteina02",
                "hard_timeout_seconds": 210, "retry_policy": "none",
            })
    result = {"schema_version": 1, "kind": subject.MANIFEST_KIND,
              "campaign_id": freeze["campaign_id"], "freeze_sha256": freeze["freeze_sha256"],
              "repository_revision": freeze["repository_revision"], "prepared_utc": "2026-09-30T12:01:00Z",
              "cells": cells}
    result["manifest_sha256"] = subject._sha(result)
    return result


@pytest.fixture(autouse=True)
def _local_linux_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subject, "_host_boot_identity", lambda: {
        "hostname": "proteina02", "linux_boot_id": "12345678-1234-1234-1234-123456789abc",
    })
    monkeypatch.setattr(subject, "_raw_clock", lambda: 90_000_000_000)
    monkeypatch.setattr(subject.local, "prepare_production_dry_run", _prepare)
    monkeypatch.setattr(subject.evaluator, "_wrapper_success_receipt", lambda *_args, **_kwargs: None)
    def verify(root: Path, freeze: dict[str, object], *args: object, **kwargs: object) -> dict[str, object]:
        if freeze["campaign_id"] == "w19-serial-v6" or (root / subject.BUILD_PROVENANCE_NAME).is_file():
            return REAL_VERIFY_FROZEN_BUILD(root, freeze, *args, **kwargs)
        return dict(freeze["build_provenance"])
    monkeypatch.setattr(subject, "_verify_frozen_build_provenance", verify)
    def post(root: Path, *, campaign_root: Path, freeze: dict[str, object], **kwargs: object) -> None:
        if freeze["campaign_id"] == "w19-serial-v6":
            REAL_POST_MATERIALIZATION_BUILD(root, campaign_root=campaign_root,
                                            freeze=freeze, **kwargs)
        elif (campaign_root / subject.BUILD_PROVENANCE_NAME).is_file():
            REAL_POST_MATERIALIZATION_BUILD(root, campaign_root=campaign_root,
                                            freeze=freeze, **kwargs)
    monkeypatch.setattr(subject, "_post_materialization_build_binding", post)


def _prepare(root: Path, **kwargs: object) -> dict[str, object]:
    # Mirror the real producer: it exclusively creates the cell, not parents.
    root.mkdir(mode=0o700)
    window = {"start_monotonic_ns": kwargs["window_start_monotonic_ns"],
              "end_monotonic_ns": kwargs["window_end_monotonic_ns"],
              "argv_pinned_before_launch": True,
              "attestation": {"must_be_written": "after_prearm_all_seven_e0_common_commit_before_scheduled_start",
                              "is_not": "an_arm_or_gate"}}
    plan = {"schema_version": 1, "repository_revision": "a" * 40, "scheduled_window": window}
    if all(name in kwargs for name in ("app_binary", "manager_binary", "e0_helper_binary")):
        archives = root / "materialization-binaries"; archives.mkdir()
        mapped = {"app_binary": "hotstuff-app", "manager_binary": "adaptation-manager",
                  "e0_helper_binary": "e0-identity-helper"}
        digests = {}
        for source_name, archive_name in mapped.items():
            source = Path(str(kwargs[source_name])); payload = source.read_bytes()
            target = archives / archive_name; target.write_bytes(payload); target.chmod(0o700)
            digests[source_name] = __import__("hashlib").sha256(payload).hexdigest()
        plan["commands"] = {
            "manager": {"executable_sha256": digests["manager_binary"]},
            "replicas": [{"executable_sha256": digests["app_binary"]} for _ in range(7)],
        }
    plan["plan_sha256"] = __import__("hashlib").sha256(_canonical(plan)).hexdigest()
    request = {
        "schema_version": 1, "kind": "request", "execution_plan_sha256": plan["plan_sha256"], "arm": kwargs["arm"],
        "no_retry": True, "hard_timeout_seconds": kwargs["hard_timeout_seconds"],
        "scheduled_window": window,
    }
    raw = _canonical(request)
    path = root / "runtime/sustained-role-authorization-request.json"
    path.parent.mkdir(parents=True)
    (root / "runtime/sustained-role-execution-plan.json").write_bytes(_canonical(plan))
    path.write_bytes(raw)
    return {"authorization_request_sha256": __import__("hashlib").sha256(raw).hexdigest()}


def _stub_prior_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subject.evaluator, "_stage_receipt", lambda *_args, **_kwargs: "a" * 64)
    monkeypatch.setattr(subject.evaluator, "_strict_json", lambda *_args, **_kwargs: {
        "artifacts": {"authorization_request": {"path": "runtime/request.json", "sha256": "a" * 64}},
    })
    monkeypatch.setattr(subject.evaluator, "_descriptor", lambda *_args: (b"{}\n", "a" * 64))


def _v6_build_receipt(root: Path) -> tuple[bytes, dict[str, object], dict[str, Path]]:
    binaries: dict[str, dict[str, object]] = {}
    paths: dict[str, Path] = {}
    for receipt_name, prepare_name in subject._BUILD_PREPARE_NAMES.items():
        path = root / f"build-{receipt_name}"
        payload = receipt_name.encode("ascii")
        path.write_bytes(payload); path.chmod(0o700)
        binaries[receipt_name] = {"path": str(path.resolve()),
                                  "sha256": hashlib.sha256(payload).hexdigest(),
                                  "size_bytes": len(payload)}
        paths[prepare_name] = path
    log = root / "build.log"; log.write_bytes(b"clean build\n")
    receipt = {
        "schema_version": 1, "kind": "kauri-w19-cluster-build-provenance-v1",
        "repository_revision": "a" * 40, "origin_revision": "a" * 40,
        "repository_branch": "feature/adaptive-epoch-throughput",
        "repository_clean_after_build": True, "host": "proteina02",
        "linux_boot_id": "12345678-1234-1234-1234-123456789abc",
        "build_exit_code": 0, "build_command": list(subject._BUILD_COMMAND),
        "build_type": "RelWithDebInfo", "cmake_version": "3.30.0",
        "cxx_compiler": "clang++", "cxx_compiler_version": "18.0.0",
        "recorded_utc": "2026-09-30T22:00:00Z", "submodule_status": ["", ""],
        "build_log_path": str(log.resolve()),
        "build_log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(), "binaries": binaries,
    }
    raw = _canonical(receipt)
    return raw, subject.build_provenance_binding(raw, repository_revision="a" * 40,
                                                  target_host="proteina02"), paths


def _v6_freeze_and_manifest(root: Path) -> tuple[dict[str, object], dict[str, object], dict[str, Path]]:
    raw, binding, paths = _v6_build_receipt(root)
    (root / subject.BUILD_PROVENANCE_NAME).write_bytes(raw)
    subject.archive_w19_build_inputs(root, raw, repository_revision="a" * 40,
                                     target_host="proteina02")
    freeze = subject.evaluator.build_campaign_freeze(
        campaign_id="w19-serial-v6", frozen_utc="2026-09-30T22:00:00Z",
        campaign_approval_reference="user-approval-ref", repository_revision="a" * 40,
        build_provenance=binding)
    snapshots = {prepare: root / subject.BUILD_SNAPSHOT_DIRECTORY / receipt
                 for receipt, prepare in subject._BUILD_PREPARE_NAMES.items()}
    return freeze, _manifest(freeze), snapshots


def test_materialization_returns_fresh_request_but_never_launches(tmp_path: Path) -> None:
    freeze = _freeze(); manifest = _manifest(freeze)
    stage = subject.materialize_next_cell(
        manifest, freeze, ordinal=1, campaign_root=tmp_path,
        window_start_monotonic_ns=140_000_000_000,
        window_end_monotonic_ns=210_000_000_000, prepare_kwargs={}, prior_validated_cells=(),
    )
    assert stage["state"] == "MATERIALIZED_NO_LAUNCH_EXTERNAL_EXACT_APPROVAL_REQUIRED"
    assert stage["arm"] == "fixed_e0"
    assert stage["launch_permitted"] is False
    assert stage["no_retry"] is True
    assert stage["authorization_request"]["must_bind"] == "external-exact-per-cell-approval"
    assert (tmp_path / "cells/cell-01/runtime/sustained-role-authorization-request.json").is_file()
    sealed = json.loads((tmp_path / "cells/cell-01/runtime/sustained-role-campaign-stage-receipt.json").read_text())
    assert sealed["manifest_sha256"] == manifest["manifest_sha256"]
    assert sealed["host_identity"]["hostname"] == "proteina02"
    assert stage["stage_receipt"]["path"] == "runtime/sustained-role-campaign-stage-receipt.json"


def test_v6_stage_embeds_frozen_build_binding_after_live_byte_check(tmp_path: Path) -> None:
    freeze, manifest, paths = _v6_freeze_and_manifest(tmp_path)
    stage = subject.materialize_next_cell(
        manifest, freeze, ordinal=1, campaign_root=tmp_path,
        window_start_monotonic_ns=140_000_000_000,
        window_end_monotonic_ns=210_000_000_000, prepare_kwargs=paths,
        prior_validated_cells=(), build_provenance=freeze["build_provenance"])
    sealed = json.loads((tmp_path / "cells/cell-01" / subject.STAGE_RECEIPT).read_text())
    assert sealed["schema_version"] == 2
    assert sealed["build_provenance_raw_sha256"] == freeze["build_provenance"]["raw_sha256"]
    assert sealed["build_provenance_binary_sha256"] == freeze["build_provenance"]["binary_sha256"]
    assert stage["launch_permitted"] is False


def test_v6_live_binary_drift_rejects_before_creating_cell(tmp_path: Path) -> None:
    freeze, manifest, paths = _v6_freeze_and_manifest(tmp_path)
    paths["app_binary"].write_bytes(b"drifted")
    with pytest.raises(subject.CampaignOperatorError, match="live frozen build binary drifted"):
        subject.materialize_next_cell(
            manifest, freeze, ordinal=1, campaign_root=tmp_path,
            window_start_monotonic_ns=140_000_000_000,
            window_end_monotonic_ns=210_000_000_000, prepare_kwargs=paths,
            prior_validated_cells=(), build_provenance=freeze["build_provenance"])
    assert not (tmp_path / "cells").exists()


def test_v6_snapshot_mutation_during_prepare_seals_abort_without_stage_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze, manifest, paths = _v6_freeze_and_manifest(tmp_path)
    def mutate(root: Path, **kwargs: object) -> dict[str, object]:
        result = _prepare(root, **kwargs)
        Path(str(kwargs["app_binary"])).write_bytes(b"mutated-during-prepare")
        return result
    monkeypatch.setattr(subject.local, "prepare_production_dry_run", mutate)
    with pytest.raises(subject.CampaignOperatorError, match="frozen build"):
        subject.materialize_next_cell(
            manifest, freeze, ordinal=1, campaign_root=tmp_path,
            window_start_monotonic_ns=140_000_000_000,
            window_end_monotonic_ns=210_000_000_000, prepare_kwargs=paths,
            prior_validated_cells=(), build_provenance=freeze["build_provenance"])
    root = tmp_path / "cells/cell-01/runtime"
    assert (root / "sustained-role-campaign-staging-abort.json").is_file()
    assert not (root / "sustained-role-campaign-stage-receipt.json").exists()


def test_v6_receipt_removal_during_prepare_seals_abort_without_stage_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze, manifest, paths = _v6_freeze_and_manifest(tmp_path)
    def remove_receipt(root: Path, **kwargs: object) -> dict[str, object]:
        result = _prepare(root, **kwargs)
        (tmp_path / subject.BUILD_PROVENANCE_NAME).unlink()
        return result
    monkeypatch.setattr(subject.local, "prepare_production_dry_run", remove_receipt)
    with pytest.raises(subject.CampaignOperatorError, match="provenance"):
        subject.materialize_next_cell(
            manifest, freeze, ordinal=1, campaign_root=tmp_path,
            window_start_monotonic_ns=140_000_000_000,
            window_end_monotonic_ns=210_000_000_000, prepare_kwargs=paths,
            prior_validated_cells=(), build_provenance=freeze["build_provenance"])
    root = tmp_path / "cells/cell-01/runtime"
    assert (root / "sustained-role-campaign-staging-abort.json").is_file()
    assert not (root / "sustained-role-campaign-stage-receipt.json").exists()


def test_v6_second_cell_requires_prior_wrapper_success_receipt_pin(tmp_path: Path) -> None:
    freeze, manifest, paths = _v6_freeze_and_manifest(tmp_path)
    legacy_prior = {"pair_index": 1, "ordinal": 1, "arm": "fixed_e0",
                    "root": str((tmp_path / "cells/cell-01").resolve()),
                    "receipt_path": "raw.json", "receipt_sha256": "a" * 64,
                    "stage_receipt_path": "runtime/stage.json", "stage_receipt_sha256": "a" * 64}
    with pytest.raises(subject.CampaignOperatorError, match="prior validation record"):
        subject.materialize_next_cell(
            manifest, freeze, ordinal=2, campaign_root=tmp_path,
            window_start_monotonic_ns=140_000_000_000,
            window_end_monotonic_ns=210_000_000_000, prepare_kwargs=paths,
            prior_validated_cells=(legacy_prior,), build_provenance=freeze["build_provenance"])
    assert not (tmp_path / "cells").exists()


def test_legacy_v1_freeze_is_read_only_before_cell_creation(tmp_path: Path) -> None:
    freeze = subject.evaluator.build_campaign_freeze(
        campaign_id="legacy", frozen_utc="2026-09-30T12:00:00Z",
        campaign_approval_reference="historical", repository_revision="a" * 40)
    with pytest.raises(subject.CampaignOperatorError, match="read-only"):
        subject.materialize_next_cell(
            _manifest(freeze), freeze, ordinal=1, campaign_root=tmp_path,
            window_start_monotonic_ns=140_000_000_000,
            window_end_monotonic_ns=210_000_000_000, prepare_kwargs={}, prior_validated_cells=())
    assert not (tmp_path / "cells").exists()


def test_materialization_with_only_29_seconds_left_seals_staging_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    samples = iter((100_000_000_000, 111_000_000_000))
    monkeypatch.setattr(subject, "_raw_clock", lambda: next(samples))
    freeze = _freeze(); manifest = _manifest(freeze)
    with pytest.raises(subject.CampaignOperatorError, match="pre-launch raw-clock reserve"):
        subject.materialize_next_cell(
            manifest, freeze, ordinal=1, campaign_root=tmp_path,
            window_start_monotonic_ns=140_000_000_000,
            window_end_monotonic_ns=210_000_000_000, prepare_kwargs={}, prior_validated_cells=(),
        )
    abort = tmp_path / "cells/cell-01" / subject.STAGING_ABORT
    assert json.loads(abort.read_text())["state"] == "STAGING_ABORTED_NO_LAUNCH_NO_RETRY"
    assert not (tmp_path / "cells/cell-01" / subject.STAGE_RECEIPT).exists()


def test_preflight_rejects_schedule_drift_and_existing_root(tmp_path: Path) -> None:
    freeze = _freeze(); manifest = _manifest(freeze)
    manifest["cells"][0]["arm"] = "adaptive_e1"
    manifest["manifest_sha256"] = subject._sha({key: value for key, value in manifest.items() if key != "manifest_sha256"})
    with pytest.raises(subject.CampaignOperatorError, match="AB/BA"):
        subject.materialize_next_cell(manifest, freeze, ordinal=1, campaign_root=tmp_path,
                                      window_start_monotonic_ns=1, window_end_monotonic_ns=70_000_000_001,
                                      prepare_kwargs={}, prior_validated_cells=())
    manifest = _manifest(freeze)
    (tmp_path / "cells/cell-01").mkdir(parents=True)
    with pytest.raises(subject.CampaignOperatorError, match="already exists"):
        subject.materialize_next_cell(manifest, freeze, ordinal=1, campaign_root=tmp_path,
                                      window_start_monotonic_ns=1, window_end_monotonic_ns=70_000_000_001,
                                      prepare_kwargs={}, prior_validated_cells=())


def test_manifest_rejects_impossible_precomputed_approval_and_raw_clock_values(tmp_path: Path) -> None:
    freeze = _freeze(); manifest = _manifest(freeze)
    manifest["cells"][0]["approval_path"] = "/external/pretend.json"
    manifest["manifest_sha256"] = subject._sha({key: value for key, value in manifest.items() if key != "manifest_sha256"})
    with pytest.raises(subject.CampaignOperatorError, match="schema drifted"):
        subject.materialize_next_cell(manifest, freeze, ordinal=1, campaign_root=tmp_path,
                                      window_start_monotonic_ns=1, window_end_monotonic_ns=70_000_000_001,
                                      prepare_kwargs={}, prior_validated_cells=())


def test_second_cell_requires_first_independent_validation(tmp_path: Path) -> None:
    freeze = _freeze(); manifest = _manifest(freeze)
    with pytest.raises(subject.CampaignOperatorError, match="preceding independently validated"):
        subject.materialize_next_cell(
            manifest, freeze, ordinal=2, campaign_root=tmp_path,
            window_start_monotonic_ns=1, window_end_monotonic_ns=70_000_000_001,
            prepare_kwargs={}, prior_validated_cells=(),
        )


def test_second_cell_calls_the_builtin_independent_validator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze = _freeze(); manifest = _manifest(freeze)
    prior_root = tmp_path / "cells/cell-01"; prior_root.mkdir(parents=True)
    receipt = prior_root / "fixed-receipt.json"; receipt.write_text("sealed\n")
    calls: list[tuple[Path, Path]] = []

    def accepted(record: dict[str, object], **_kwargs: object) -> dict[str, object]:
        assert isinstance(record["receipt_path"], str)
        calls.append((Path(str(record["root"])), Path(str(record["receipt_path"]))))
        return {"identity": {"same": "fixture"},
                "scheduled_window": {"start_monotonic_ns": 1, "end_monotonic_ns": 2}}

    monkeypatch.setattr(subject.evaluator, "_one_cell", accepted)
    _stub_prior_provenance(monkeypatch)
    stage = subject.materialize_next_cell(
        manifest, freeze, ordinal=2, campaign_root=tmp_path,
        window_start_monotonic_ns=140_000_000_000,
        window_end_monotonic_ns=210_000_000_000, prepare_kwargs={},
        prior_validated_cells=({"pair_index": 1, "ordinal": 1, "arm": "fixed_e0",
                                "root": str(prior_root.resolve()), "receipt_path": "fixed-receipt.json",
                                "receipt_sha256": __import__("hashlib").sha256(receipt.read_bytes()).hexdigest(),
                                "stage_receipt_path": "runtime/stage.json", "stage_receipt_sha256": "a" * 64,
                                "success_receipt_path": "runtime/success.json", "success_receipt_sha256": "a" * 64},),
    )
    assert stage["ordinal"] == 2
    assert calls == [(prior_root.resolve(), Path("fixed-receipt.json"))]


def test_new_window_must_follow_previous_accepted_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze = _freeze(); manifest = _manifest(freeze)
    prior_root = tmp_path / "cells/cell-01"; prior_root.mkdir(parents=True)
    receipt = prior_root / "fixed-receipt.json"; receipt.write_text("sealed\n")

    def accepted(_record: object, **_kwargs: object) -> dict[str, object]:
        return {"identity": {"same": "fixture"},
                "scheduled_window": {"start_monotonic_ns": 100_000_000_000,
                                     "end_monotonic_ns": 200_000_000_000}}

    monkeypatch.setattr(subject.evaluator, "_one_cell", accepted)
    _stub_prior_provenance(monkeypatch)
    with pytest.raises(subject.CampaignOperatorError, match="begins before"):
        subject.materialize_next_cell(
            manifest, freeze, ordinal=2, campaign_root=tmp_path,
            window_start_monotonic_ns=140_000_000_000,
            window_end_monotonic_ns=210_000_000_000, prepare_kwargs={},
            prior_validated_cells=({"pair_index": 1, "ordinal": 1, "arm": "fixed_e0",
                                    "root": str(prior_root.resolve()), "receipt_path": "fixed-receipt.json",
                                    "receipt_sha256": __import__("hashlib").sha256(receipt.read_bytes()).hexdigest(),
                                    "stage_receipt_path": "runtime/stage.json", "stage_receipt_sha256": "a" * 64,
                                    "success_receipt_path": "runtime/success.json", "success_receipt_sha256": "a" * 64},),
        )
    assert not (tmp_path / "cells/cell-02").exists()


def test_cell1_launch_record_binds_real_stage_and_request_before_cell2_materializes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use the launch record shape, real stage receipt, and raw request descriptor."""
    freeze = _freeze(); manifest = _manifest(freeze)
    first = subject.materialize_next_cell(
        manifest, freeze, ordinal=1, campaign_root=tmp_path,
        window_start_monotonic_ns=140_000_000_000,
        window_end_monotonic_ns=210_000_000_000, prepare_kwargs={}, prior_validated_cells=(),
    )
    first_root = tmp_path / "cells/cell-01"
    raw_receipt = {"artifacts": {"authorization_request": {
        "path": "runtime/sustained-role-authorization-request.json",
        "sha256": first["authorization_request"]["sha256"],
    }}}
    raw_path = first_root / "synthetic-launch-raw-receipt.json"
    raw_path.write_bytes(_canonical(raw_receipt))
    record = {"pair_index": 1, "ordinal": 1, "arm": "fixed_e0", "root": str(first_root.resolve()),
              "receipt_path": raw_path.name,
              "receipt_sha256": __import__("hashlib").sha256(raw_path.read_bytes()).hexdigest(),
              "stage_receipt_path": first["stage_receipt"]["path"],
              "stage_receipt_sha256": first["stage_receipt"]["sha256"],
              "success_receipt_path": "runtime/success.json", "success_receipt_sha256": "a" * 64}

    def accepted(_record: object, **_kwargs: object) -> dict[str, object]:
        return {"identity": {"fixture": "same"},
                "scheduled_window": {"start_monotonic_ns": 140_000_000_000,
                                     "end_monotonic_ns": 210_000_000_000}}

    monkeypatch.setattr(subject.evaluator, "_one_cell", accepted)
    second = subject.materialize_next_cell(
        manifest, freeze, ordinal=2, campaign_root=tmp_path,
        window_start_monotonic_ns=250_000_000_000,
        window_end_monotonic_ns=320_000_000_000, prepare_kwargs={}, prior_validated_cells=(record,),
    )
    assert second["ordinal"] == 2
    assert (tmp_path / "cells/cell-02/runtime/sustained-role-authorization-request.json").is_file()


def test_changed_linux_boot_rejects_prior_cell_before_staging_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze = _freeze(); manifest = _manifest(freeze)
    first = subject.materialize_next_cell(
        manifest, freeze, ordinal=1, campaign_root=tmp_path,
        window_start_monotonic_ns=140_000_000_000,
        window_end_monotonic_ns=210_000_000_000, prepare_kwargs={}, prior_validated_cells=(),
    )
    first_root = tmp_path / "cells/cell-01"
    receipt = first_root / "synthetic-launch-raw-receipt.json"
    receipt.write_bytes(_canonical({"artifacts": {"authorization_request": {
        "path": "runtime/sustained-role-authorization-request.json",
        "sha256": first["authorization_request"]["sha256"],
    }}}))
    record = {"pair_index": 1, "ordinal": 1, "arm": "fixed_e0", "root": str(first_root.resolve()),
              "receipt_path": receipt.name,
              "receipt_sha256": __import__("hashlib").sha256(receipt.read_bytes()).hexdigest(),
              "stage_receipt_path": first["stage_receipt"]["path"],
              "stage_receipt_sha256": first["stage_receipt"]["sha256"],
              "success_receipt_path": "runtime/success.json", "success_receipt_sha256": "a" * 64}
    monkeypatch.setattr(subject, "_host_boot_identity", lambda: {
        "hostname": "proteina02", "linux_boot_id": "12345678-1234-1234-1234-123456789abd",
    })
    with pytest.raises(subject.CampaignOperatorError, match="prior cell did not pass independent validation"):
        subject.materialize_next_cell(
            manifest, freeze, ordinal=2, campaign_root=tmp_path,
            window_start_monotonic_ns=250_000_000_000,
            window_end_monotonic_ns=320_000_000_000, prepare_kwargs={}, prior_validated_cells=(record,),
        )
    assert not (tmp_path / "cells/cell-02").exists()


def test_outer_abort_blocks_progress_even_when_prior_raw_receipt_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze = _freeze(); manifest = _manifest(freeze)
    prior_root = tmp_path / "cells/cell-01"
    prior_root.mkdir(parents=True)
    receipt = prior_root / "fixed-receipt.json"
    receipt.write_bytes(b"sealed raw receipt\n")
    abort = prior_root / "runtime/sustained-role-campaign-launch-abort.json"
    abort.parent.mkdir()
    abort.write_bytes(_canonical({"state": "ABORTED_NO_RETRY_SCOPE_STATUS_UNVERIFIED"}))
    record = {"pair_index": 1, "ordinal": 1, "arm": "fixed_e0", "root": str(prior_root.resolve()),
              "receipt_path": receipt.name,
              "receipt_sha256": __import__("hashlib").sha256(receipt.read_bytes()).hexdigest(),
              "stage_receipt_path": "runtime/stage.json", "stage_receipt_sha256": "a" * 64,
              "success_receipt_path": "runtime/success.json", "success_receipt_sha256": "a" * 64}
    monkeypatch.setattr(subject.evaluator, "_one_cell", lambda *_args, **_kwargs: pytest.fail(
        "raw validation must not run after a sealed abort"))
    with pytest.raises(subject.CampaignOperatorError, match="negative marker"):
        subject.materialize_next_cell(
            manifest, freeze, ordinal=2, campaign_root=tmp_path,
            window_start_monotonic_ns=140_000_000_000,
            window_end_monotonic_ns=210_000_000_000, prepare_kwargs={}, prior_validated_cells=(record,),
        )
    assert not (tmp_path / "cells/cell-02").exists()


@pytest.mark.parametrize(("second_identity", "second_start", "message"), [
    ({"same": "different"}, 20, "not campaign-comparable"),
    ({"same": "fixture"}, 5, "overlap"),
])
def test_prior_cells_must_match_identity_and_not_overlap_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, second_identity: dict[str, str],
    second_start: int, message: str,
) -> None:
    freeze = _freeze(); manifest = _manifest(freeze)
    records = []
    for ordinal, arm in ((1, "fixed_e0"), (2, "adaptive_e1")):
        root = tmp_path / f"cells/cell-{ordinal:02d}"; root.mkdir(parents=True)
        receipt = root / "receipt.json"; receipt.write_text(f"sealed-{ordinal}\n")
        records.append({"pair_index": (ordinal + 1) // 2, "ordinal": ordinal, "arm": arm,
                        "root": str(root.resolve()), "receipt_path": "receipt.json",
                        "receipt_sha256": __import__("hashlib").sha256(receipt.read_bytes()).hexdigest(),
                        "stage_receipt_path": "runtime/stage.json", "stage_receipt_sha256": "a" * 64,
                        "success_receipt_path": "runtime/success.json", "success_receipt_sha256": "a" * 64})

    def accepted(record: dict[str, object], **_kwargs: object) -> dict[str, object]:
        ordinal = int(record["ordinal"])
        return {"identity": {"same": "fixture"} if ordinal == 1 else second_identity,
                "scheduled_window": {"start_monotonic_ns": 0 if ordinal == 1 else second_start,
                                     "end_monotonic_ns": 10 if ordinal == 1 else 30}}

    monkeypatch.setattr(subject.evaluator, "_one_cell", accepted)
    _stub_prior_provenance(monkeypatch)
    with pytest.raises(subject.CampaignOperatorError, match=message):
        subject.materialize_next_cell(
            manifest, freeze, ordinal=3, campaign_root=tmp_path,
            window_start_monotonic_ns=140_000_000_000,
            window_end_monotonic_ns=210_000_000_000, prepare_kwargs={},
            prior_validated_cells=records,
        )
    assert not (tmp_path / "cells/cell-03").exists()


def test_rejects_stale_raw_clock_window_before_creating_a_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    freeze = _freeze(); manifest = _manifest(freeze)
    with pytest.raises(subject.CampaignOperatorError, match="not fresh"):
        subject.materialize_next_cell(
            manifest, freeze, ordinal=1, campaign_root=tmp_path,
            window_start_monotonic_ns=119_000_000_000,
            window_end_monotonic_ns=189_000_000_000, prepare_kwargs={}, prior_validated_cells=(),
        )
    assert not (tmp_path / "cells/cell-01").exists()


def test_rejects_plan_revision_drift_from_frozen_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def wrong_revision(root: Path, **kwargs: object) -> dict[str, object]:
        result = _prepare(root, **kwargs)
        plan_path = root / "runtime/sustained-role-execution-plan.json"
        plan = json.loads(plan_path.read_text()); plan["repository_revision"] = "b" * 40
        plan["plan_sha256"] = __import__("hashlib").sha256(_canonical(
            {key: value for key, value in plan.items() if key != "plan_sha256"})).hexdigest()
        plan_path.write_bytes(_canonical(plan))
        request_path = root / "runtime/sustained-role-authorization-request.json"
        request = json.loads(request_path.read_text()); request["execution_plan_sha256"] = plan["plan_sha256"]
        request_path.write_bytes(_canonical(request))
        result["authorization_request_sha256"] = __import__("hashlib").sha256(request_path.read_bytes()).hexdigest()
        return result

    freeze = _freeze(); manifest = _manifest(freeze)
    monkeypatch.setattr(subject.local, "prepare_production_dry_run", wrong_revision)
    with pytest.raises(subject.CampaignOperatorError, match="revision differs"):
        subject.materialize_next_cell(
            manifest, freeze, ordinal=1, campaign_root=tmp_path,
            window_start_monotonic_ns=140_000_000_000,
            window_end_monotonic_ns=210_000_000_000, prepare_kwargs={}, prior_validated_cells=(),
        )
    abort = json.loads((tmp_path / "cells/cell-01/runtime/sustained-role-campaign-staging-abort.json").read_text())
    assert abort["state"] == "STAGING_ABORTED_NO_LAUNCH_NO_RETRY"
    assert abort["freeze_sha256"] == freeze["freeze_sha256"]


def test_rejects_producer_test_seams_and_wrong_host_before_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze = _freeze(); manifest = _manifest(freeze)
    with pytest.raises(subject.CampaignOperatorError, match="producer arguments"):
        subject.materialize_next_cell(
            manifest, freeze, ordinal=1, campaign_root=tmp_path,
            window_start_monotonic_ns=140_000_000_000,
            window_end_monotonic_ns=210_000_000_000,
            prepare_kwargs={"repository_snapshot": object()}, prior_validated_cells=(),
        )
    monkeypatch.setattr(subject, "_host_boot_identity", lambda: {
        "hostname": "other-host", "linux_boot_id": "12345678-1234-1234-1234-123456789abc",
    })
    with pytest.raises(subject.CampaignOperatorError, match="hostname differs"):
        subject.materialize_next_cell(
            manifest, freeze, ordinal=1, campaign_root=tmp_path,
            window_start_monotonic_ns=140_000_000_000,
            window_end_monotonic_ns=210_000_000_000, prepare_kwargs={}, prior_validated_cells=(),
        )
    assert not (tmp_path / "cells/cell-01").exists()


def test_rejects_lexical_symlink_ancestor_before_resolving_new_root(tmp_path: Path) -> None:
    freeze = _freeze(); manifest = _manifest(freeze)
    outside = tmp_path / "outside"; outside.mkdir()
    (tmp_path / "cells").symlink_to(outside, target_is_directory=True)
    with pytest.raises(subject.CampaignOperatorError, match="symlink ancestor"):
        subject.materialize_next_cell(
            manifest, freeze, ordinal=1, campaign_root=tmp_path,
            window_start_monotonic_ns=140_000_000_000,
            window_end_monotonic_ns=210_000_000_000, prepare_kwargs={}, prior_validated_cells=(),
        )
    assert not (outside / "cell-01").exists()
