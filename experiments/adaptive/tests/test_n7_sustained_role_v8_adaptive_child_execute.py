from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


PATH = Path(__file__).resolve().parents[1] / "n7-path-timeout-quorum" / "sustained_role_v8_adaptive_child_execute.py"
spec = importlib.util.spec_from_file_location("w19_v8_child_execute_test", PATH)
assert spec and spec.loader
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)

PRODUCER_PATH = PATH.with_name("sustained_role_v8_full_input_producer.py")
producer_spec = importlib.util.spec_from_file_location("w19_v8_full_input_composition", PRODUCER_PATH)
assert producer_spec and producer_spec.loader
producer = importlib.util.module_from_spec(producer_spec)
producer_spec.loader.exec_module(producer)


def _canon(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canon(value) if not isinstance(value, bytes) else value)


def _descriptor(root: Path, relative: str, raw: bytes) -> dict[str, object]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
    return {"path": relative, "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _root(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "v8-root"; root.mkdir()
    app = _descriptor(root, "bin/app", b"app")
    manager = _descriptor(root, "bin/manager", b"manager")
    keygen = _descriptor(root, "bin/keygen", b"keygen")
    tls_keygen = _descriptor(root, "bin/tls-keygen", b"tls-keygen")
    e0_helper = _descriptor(root, "bin/e0-helper", b"e0-helper")
    binary_descriptors = {
        "hotstuff_app": app, "adaptation_manager": manager,
        "hotstuff_keygen": keygen, "hotstuff_tls_keygen": tls_keygen,
        "e0_helper": e0_helper,
    }
    build_log = tmp_path / "strict-build.log"; build_log.write_bytes(b"strict synthetic W19 build\n")
    receipt_binaries = {
        receipt_name: {"path": str((root / binary_descriptors[artifact_name]["path"]).resolve()),
                       "sha256": binary_descriptors[artifact_name]["sha256"],
                       "size_bytes": binary_descriptors[artifact_name]["size_bytes"]}
        for receipt_name, artifact_name in {
            "hotstuff_app": "hotstuff_app", "adaptation_manager": "adaptation_manager",
            "hotstuff_keygen": "hotstuff_keygen", "hotstuff_tls_keygen": "hotstuff_tls_keygen",
            "epoch0_treefile_digest": "e0_helper",
        }.items()
    }
    build_receipt = _descriptor(root, "runtime/external-build.json", _canon({
        "schema_version": 1, "kind": "kauri-w19-cluster-build-provenance-v1",
        "repository_revision": "a" * 40, "origin_revision": "a" * 40,
        "repository_branch": "feature/adaptive-epoch-throughput",
        "repository_clean_after_build": True, "host": "proteina02",
        "linux_boot_id": "12345678-1234-1234-1234-123456789abc",
        "build_exit_code": 0,
        "build_command": ["cmake", "--build", "build-adaptive", "--clean-first", "--target",
                          "hotstuff-app", "adaptation-manager", "hotstuff-keygen",
                          "hotstuff-tls-keygen", "n7-epoch0-treefile-digest", "-j2"],
        "build_log_path": str(build_log.resolve()),
        "build_log_sha256": hashlib.sha256(build_log.read_bytes()).hexdigest(),
        "build_type": "RelWithDebInfo", "cmake_version": "3.30.0",
        "cxx_compiler": "clang++", "cxx_compiler_version": "18.0.0",
        "recorded_utc": "2026-10-01T12:00:00Z", "submodule_status": ["", ""],
        "binaries": receipt_binaries,
    }))
    profile = _descriptor(root, "runtime/v8-profile.json", b'{"profile":"v8"}\n')
    main = _descriptor(root, "config/main.conf", b"main-config")
    launch_processes, processes, configs = [], [], []
    for source in ("adaptive-manager", *(f"replica-{replica}" for replica in range(7))):
        kind = "adaptation_manager" if source == "adaptive-manager" else "replica"
        instance = f"run-1-{source}"
        if source == "adaptive-manager":
            argv = [str(root / manager["path"]), "--convergence-deadline-seconds", "12",
                    "--structured-event-run-id", "run-1", "--structured-event-source-instance", instance,
                    "--required-nonresponsive", "1"]
        else:
            config = _descriptor(root, f"config/{source}.conf", f"structured-event-source-instance = {instance}\n".encode())
            configs.append(config)
            argv = [str(root / app["path"]), "--conf", str(root / main["path"]), "--conf", str(root / config["path"])]
            if source == "replica-1":
                argv += list(subject.profile.native_actor_overlay(scheduled_start_monotonic_ns=1, scheduled_end_monotonic_ns=82_000_000_001))
        row = {"source_kind": kind, "source_id": source, "source_instance": instance, "argv": argv,
               "argv_sha256": subject._argv_digest(argv)}
        processes.append(row)
        options = {"binary_sha256": manager["sha256"] if source == "adaptive-manager" else app["sha256"]}
        if source != "adaptive-manager": options["replica_config_sha256"] = config["sha256"]
        launch_processes.append({"source_kind": kind, "source_id": source, "argv": argv, "effective_options": options})
    launch = {"schema_version": 1, "processes": launch_processes}
    launch_descriptor = _descriptor(root, "runtime/launch-arguments.json", _canon(launch))
    plan = {"schema_version": 1, "kind": "kauri-n7-sustained-role-v8-full-input-plan-v1", "state": "PREPARED_INPUTS_NO_LAUNCH",
            "run_id": "run-1", "repository_revision": "a" * 40,
            "profile_id": "n7-sustained-role-proposal-boundary-v8", "profile_sha256": profile["sha256"], "hard_timeout_seconds": 210,
            "scheduled_window": {"clock": "CLOCK_MONOTONIC_RAW", "start_ns": 1, "end_ns": 82_000_000_001, "minimum_duration_ns": 82_000_000_000},
            "processes": processes, "artifacts": {"build_receipt": build_receipt, "launch_arguments": launch_descriptor, **binary_descriptors, "main_config": main, "v8_profile": profile, "replica_configs": configs},
            "no_retry": True, "claim_eligible": False, "figure_eligible": False, "build_provenance": "ARCHIVED_NOT_LIVE_ATTESTED"}
    plan["plan_sha256"] = hashlib.sha256(_canon(plan)).hexdigest()
    _write(root / subject.PLAN, plan)
    request = {"schema_version": 1, "kind": "kauri-n7-sustained-role-v8-full-input-request-v1", "plan_sha256": plan["plan_sha256"],
               "run_id": "run-1", "no_launch": True, "no_retry": True}
    _write(root / subject.REQUEST, request)
    approval = tmp_path / "approval.json"
    _write(approval, {"schema_version": 1, "kind": "kauri-n7-sustained-role-v8-adaptive-child-launch-authorization-v1",
                      "request_sha256": hashlib.sha256(_canon(request)).hexdigest(), "plan_sha256": plan["plan_sha256"],
                      "run_id": "run-1", "no_retry": True})
    return root, approval


def _prepare(root: Path, approval: Path) -> dict[str, object]:
    return subject.prepare_pre_spawn_scope(
        root, approval_path=approval, observed_revision="a" * 40,
        target_host="proteina02",
        linux_boot_id="12345678-1234-1234-1234-123456789abc",
        expected_approval_sha256=hashlib.sha256(approval.read_bytes()).hexdigest(),
    )


def test_pre_spawn_scope_rejects_incomplete_synthetic_closure_before_intent(tmp_path: Path) -> None:
    root, approval = _root(tmp_path)
    with pytest.raises(subject.V8AdaptiveChildExecuteError, match="producer closure verification"):
        _prepare(root, approval)
    assert not (root / subject.INTENT).exists()


def test_real_full_input_producer_composes_into_no_process_child_scope(tmp_path: Path) -> None:
    build = PATH.parents[3] / "build-adaptive"
    binaries = {"app_binary": build / "examples/hotstuff-app", "manager_binary": build / "examples/adaptation-manager",
                "keygen_binary": build / "hotstuff-keygen", "tls_keygen_binary": build / "hotstuff-tls-keygen",
                "e0_helper_binary": build / "examples/n7-epoch0-treefile-digest"}
    if not all(path.is_file() for path in binaries.values()):
        pytest.skip("fixture-only local build binaries are unavailable")
    build_log = tmp_path / "composition-build.log"; build_log.write_bytes(b"strict composition fixture build\n")
    receipt_binaries = {
        receipt_name: {"path": str(binaries[binary_name].resolve()),
                       "sha256": hashlib.sha256(binaries[binary_name].read_bytes()).hexdigest(),
                       "size_bytes": binaries[binary_name].stat().st_size}
        for receipt_name, binary_name in {
            "hotstuff_app": "app_binary", "adaptation_manager": "manager_binary",
            "hotstuff_keygen": "keygen_binary", "hotstuff_tls_keygen": "tls_keygen_binary",
            "epoch0_treefile_digest": "e0_helper_binary",
        }.items()
    }
    build_receipt = tmp_path / "strict-composition-build-receipt.json"
    _write(build_receipt, {"schema_version": 1, "kind": "kauri-w19-cluster-build-provenance-v1",
                           "repository_revision": "a" * 40, "origin_revision": "a" * 40,
                           "repository_branch": "feature/adaptive-epoch-throughput",
                           "repository_clean_after_build": True, "host": "proteina02",
                           "linux_boot_id": "12345678-1234-1234-1234-123456789abc",
                           "build_exit_code": 0,
                           "build_command": ["cmake", "--build", "build-adaptive", "--clean-first", "--target",
                                             "hotstuff-app", "adaptation-manager", "hotstuff-keygen",
                                             "hotstuff-tls-keygen", "n7-epoch0-treefile-digest", "-j2"],
                           "build_log_path": str(build_log.resolve()),
                           "build_log_sha256": hashlib.sha256(build_log.read_bytes()).hexdigest(),
                           "build_type": "RelWithDebInfo", "cmake_version": "3.30.0",
                           "cxx_compiler": "clang++", "cxx_compiler_version": "18.0.0",
                           "recorded_utc": "2026-10-01T12:00:00Z", "submodule_status": ["", ""],
                           "binaries": receipt_binaries})
    root = tmp_path / "real-full-input"
    outcome = producer.prepare(root, run_id="composition", ports=(18471, 19471, 20471), start_ns=1, end_ns=82_000_000_001,
                               binaries=binaries, build_receipt=build_receipt,
                               repository_verifier=lambda _root: type("Snapshot", (), {"revision": "a" * 40, "worktree_clean": True})())
    request_raw = (root / producer.REQUEST).read_bytes()
    approval = tmp_path / "external-approval.json"
    _write(approval, {"schema_version": 1, "kind": "kauri-n7-sustained-role-v8-adaptive-child-launch-authorization-v1",
                      "request_sha256": hashlib.sha256(request_raw).hexdigest(), "plan_sha256": outcome["plan_sha256"],
                      "run_id": "composition", "no_retry": True})
    scope = subject.prepare_pre_spawn_scope(
        root, approval_path=approval, observed_revision="a" * 40,
        target_host="proteina02",
        linux_boot_id="12345678-1234-1234-1234-123456789abc",
        expected_approval_sha256=hashlib.sha256(approval.read_bytes()).hexdigest(),
    )
    assert scope["no_launch"] is True
    assert len(scope["commands"]) == 8
    assert {"run_id", "plan_sha256", "request_sha256", "approval_sha256", "scheduled_window",
            "source_instances", "build_binding", "no_retry", "claim_eligible", "figure_eligible"} <= set(scope)
    assert scope["scheduled_window"]["start_ns"] == 1
    assert not (root / "raw").exists()


def test_pre_spawn_rejects_producer_abort_inventory_and_intermediate_symlink(tmp_path: Path) -> None:
    root, approval = _root(tmp_path)
    _write(root / "runtime/sustained-role-v8-full-input-abort.json", {"state": "ABORTED_NO_RETRY_NO_LAUNCH"})
    with pytest.raises(subject.V8AdaptiveChildExecuteError, match="fresh no-retry"):
        _prepare(root, approval)


def test_argv_executable_must_be_the_archived_in_root_path_not_just_same_bytes(tmp_path: Path) -> None:
    root = tmp_path / "root"; root.mkdir()
    archived = _descriptor(root, "bin/app", b"same-bytes")
    outside = tmp_path / "outside-app"; outside.write_bytes(b"same-bytes")
    with pytest.raises(subject.V8AdaptiveChildExecuteError, match="argv executable differs"):
        subject._argv_executable((str(outside),), archived, root, "app")


def test_rejects_external_approval_replacement_during_producer_verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, approval = _root(tmp_path)
    original_sha = hashlib.sha256(approval.read_bytes()).hexdigest()

    def replace_approval(_root: Path, *, expected_request_sha256: str) -> dict[str, str]:
        replacement = tmp_path / "replacement-approval.json"
        _write(replacement, {"schema_version": 1, "kind": "kauri-n7-sustained-role-v8-adaptive-child-launch-authorization-v1",
                             "request_sha256": "0" * 64, "plan_sha256": "0" * 64,
                             "run_id": "wrong", "no_retry": True})
        replacement.replace(approval)
        return {"plan_sha256": "a" * 64}

    monkeypatch.setattr(subject.producer, "verify", replace_approval)
    with pytest.raises(subject.V8AdaptiveChildExecuteError, match="external approval differs"):
        subject.prepare_pre_spawn_scope(root, approval_path=approval, observed_revision="a" * 40,
                                        target_host="proteina02",
                                        linux_boot_id="12345678-1234-1234-1234-123456789abc",
                                        expected_approval_sha256=original_sha)
    assert not (root / subject.INTENT).exists()


@pytest.mark.parametrize("mutation, error", [
    ("manager_deadline", "producer closure verification"),
    ("missing_replica", "producer closure verification"),
    ("descriptor_hash", "producer closure verification"),
    ("approval_request", "does not bind"),
    ("existing_intent", "fresh no-retry"),
])
def test_pre_spawn_scope_fails_closed_before_any_process_exists(tmp_path: Path, mutation: str, error: str) -> None:
    root, approval = _root(tmp_path)
    launch_path = root / "runtime/launch-arguments.json"
    launch = json.loads(launch_path.read_text())
    if mutation == "manager_deadline":
        argv = launch["processes"][0]["argv"]
        argv[argv.index("--convergence-deadline-seconds") + 1] = "20"
    elif mutation == "missing_replica": launch["processes"].pop()
    elif mutation == "descriptor_hash":
        (root / "bin/app").write_bytes(b"tampered")
    elif mutation == "approval_request":
        document = json.loads(approval.read_text()); document["request_sha256"] = "0" * 64; _write(approval, document)
    elif mutation == "existing_intent": _write(root / subject.INTENT, {"old": True})
    if mutation in {"manager_deadline", "missing_replica"}:
        _write(launch_path, launch)
        plan = json.loads((root / subject.PLAN).read_text())
        if mutation == "manager_deadline":
            argv = plan["processes"][0]["argv"]
            argv[argv.index("--convergence-deadline-seconds") + 1] = "20"
            plan["processes"][0]["argv_sha256"] = subject._argv_digest(argv)
        raw = launch_path.read_bytes(); plan["artifacts"]["launch_arguments"]["size_bytes"] = len(raw); plan["artifacts"]["launch_arguments"]["sha256"] = hashlib.sha256(raw).hexdigest()
        old_hash = plan.pop("plan_sha256"); plan["plan_sha256"] = hashlib.sha256(_canon(plan)).hexdigest(); _write(root / subject.PLAN, plan)
        request = json.loads((root / subject.REQUEST).read_text()); request["plan_sha256"] = plan["plan_sha256"]; _write(root / subject.REQUEST, request)
        document = json.loads(approval.read_text()); document["plan_sha256"] = plan["plan_sha256"]; document["request_sha256"] = hashlib.sha256((root / subject.REQUEST).read_bytes()).hexdigest(); _write(approval, document)
    with pytest.raises(subject.V8AdaptiveChildExecuteError, match=error):
        _prepare(root, approval)
    assert not (root / "raw").exists()
