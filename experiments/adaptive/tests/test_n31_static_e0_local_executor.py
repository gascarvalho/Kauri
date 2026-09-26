"""Adversarial contracts for the bounded W16 local executor seam."""

from __future__ import annotations

import hashlib
import importlib
from pathlib import Path

import pytest


def _modules():
    return (
        importlib.import_module("experiments.adaptive.kauri_experiment.n31_static_e0_feasibility"),
        importlib.import_module("experiments.adaptive.kauri_experiment.n31_static_e0_local_executor"),
    )


def _preflight(feasibility, plan, binaries: dict[str, Path]) -> dict[str, object]:
    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "verdict": "PREFLIGHT_OK_NO_EXECUTION", "kind": feasibility.SCHEMA,
        "arm": plan.arm, "profile_sha256": plan.profile.sha256,
        "treegen_sha256": plan.treegen_sha256, "revision": "a" * 40,
        "binaries": {name: str(path) for name, path in binaries.items()},
        "binary_sha256": {name: digest(path) for name, path in binaries.items()},
    }


def _inputs(tmp_path: Path):
    feasibility, executor = _modules()
    plan = feasibility.frozen_plan(arm="slow-roots")
    tree = tmp_path / "tree.conf"; tree.write_bytes(plan.treegen_bytes)
    binaries = {}
    for name in ("app", "keygen", "tls_keygen", "native_digest"):
        binary = tmp_path / name
        if name == "native_digest":
            binary.write_text("#!/bin/sh\nprintf '%s\\n' 827e7626c74f8d815bca6ae5cbe10e312bc4f00f287e67d41277b8d689b21c0f\n", encoding="utf-8")
        else:
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755); binaries[name] = binary
    return feasibility, executor, plan, tree, binaries, _preflight(feasibility, plan, binaries)


def test_prepare_requires_binary_hash_bound_preflight(tmp_path: Path) -> None:
    _f, executor, plan, tree, binaries, receipt = _inputs(tmp_path)
    del receipt["binary_sha256"]
    with pytest.raises(executor.LocalExecutorError, match="binary hashes"):
        executor.prepare_launch(plan=plan, preflight=receipt, treegen_path=tree,
                                run_id="r", hard_timeout_s=1)


def test_prepare_rejects_modified_binary_or_tree(tmp_path: Path) -> None:
    _f, executor, plan, tree, binaries, receipt = _inputs(tmp_path)
    binaries["app"].write_text("changed", encoding="utf-8")
    with pytest.raises(executor.LocalExecutorError, match="app executable hash"):
        executor.prepare_launch(plan=plan, preflight=receipt, treegen_path=tree,
                                run_id="r", hard_timeout_s=1)
    binaries["app"].write_text("#!/bin/sh\n", encoding="utf-8"); binaries["app"].chmod(0o755)
    receipt = _preflight(_f, plan, binaries)
    tree.write_text("forged", encoding="utf-8")
    with pytest.raises(executor.LocalExecutorError, match="written tree"):
        executor.prepare_launch(plan=plan, preflight=receipt, treegen_path=tree,
                                run_id="r", hard_timeout_s=1)


def test_prepare_binds_native_digest_and_all_31_sources(tmp_path: Path) -> None:
    _f, executor, plan, tree, _binaries, receipt = _inputs(tmp_path)
    inputs = executor.prepare_launch(plan=plan, preflight=receipt, treegen_path=tree,
                                     run_id="bound", hard_timeout_s=1)
    assert inputs.epoch_zero_digest == "827e7626c74f8d815bca6ae5cbe10e312bc4f00f287e67d41277b8d689b21c0f"
    assert set(inputs.source_instances) == {f"replica-{i}" for i in range(31)}


def test_prepare_rejects_malformed_native_digest(tmp_path: Path) -> None:
    _f, executor, plan, tree, binaries, receipt = _inputs(tmp_path)
    binaries["native_digest"].write_text("#!/bin/sh\nprintf not-a-digest\\n\n", encoding="utf-8")
    binaries["native_digest"].chmod(0o755)
    receipt = _preflight(_f, plan, binaries)
    with pytest.raises(executor.LocalExecutorError, match="malformed digest"):
        executor.prepare_launch(plan=plan, preflight=receipt, treegen_path=tree,
                                run_id="r", hard_timeout_s=1)


def test_prepare_rejects_global_timeout_before_helper(tmp_path: Path) -> None:
    _f, executor, plan, tree, _binaries, receipt = _inputs(tmp_path)
    with pytest.raises(executor.LocalExecutorError, match="timeout"):
        executor.prepare_launch(plan=plan, preflight=receipt, treegen_path=tree,
                                run_id="r", hard_timeout_s=0)


def test_seal_rejects_incomplete_cleanup_and_never_relaunches(tmp_path: Path) -> None:
    _f, executor, plan, tree, _binaries, receipt = _inputs(tmp_path)
    inputs = executor.prepare_launch(plan=plan, preflight=receipt, treegen_path=tree,
                                     run_id="seal", hard_timeout_s=1)
    cleanup = executor.CleanupVerification(tuple(range(30)), True, True)
    with pytest.raises(executor.LocalExecutorError, match="all 31"):
        executor.seal_outcome(directory=tmp_path, inputs=inputs, cleanup=cleanup, streams={})
    cleanup = executor.CleanupVerification(tuple(range(31)), True, True)
    aborted = executor.seal_outcome(directory=tmp_path, inputs=inputs, cleanup=cleanup, streams={})
    assert aborted.name == "feasibility-abort.json"
    with pytest.raises(executor.LocalExecutorError, match="already been sealed"):
        executor.seal_outcome(directory=tmp_path, inputs=inputs, cleanup=cleanup, streams={})


def test_execute_once_rejects_unbound_preflight_before_creating_output(tmp_path: Path) -> None:
    _f, executor, plan, _tree, _binaries, receipt = _inputs(tmp_path)
    del receipt["binary_sha256"]
    with pytest.raises(executor.LocalExecutorError, match="binary hashes"):
        executor.execute_once(
            plan=plan, preflight=receipt, directory=tmp_path / "run",
        )
    assert not (tmp_path / "run").exists()


def test_execute_once_seals_keygen_failure_without_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _f, executor, plan, _tree, _binaries, receipt = _inputs(tmp_path)
    closed: list[bool] = []

    class Held:
        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(executor, "reserve_no_listener", lambda **_kwargs: Held())
    monkeypatch.setattr(
        executor, "_generate_identities_bounded",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("keygen failed")),
    )
    output = tmp_path / "run"
    result = executor.execute_once(
        plan=plan, preflight=receipt, directory=output, hard_timeout_s=21,
    )
    assert result["verdict"] == "ABORT"
    assert result["failure"] == "keygen failed"
    assert result["registered_replica_ids"] == []
    assert (output / "feasibility-abort.json").is_file()
    assert not (output / "feasibility-receipt.json").exists()
    assert closed == [True]
    with pytest.raises(executor.LocalExecutorError, match="fresh output"):
        executor.execute_once(
            plan=plan, preflight=receipt, directory=output, hard_timeout_s=21,
        )


def test_held_no_listener_reservation_binds_without_listening() -> None:
    feasibility, executor = _modules()
    calls: list[tuple[object, ...]] = []

    class Socket:
        def setsockopt(self, *args: object) -> None:
            calls.append(("setsockopt", *args))

        def bind(self, endpoint: object) -> None:
            calls.append(("bind", endpoint))

        def close(self) -> None:
            calls.append(("close",))

    reservation = executor.reserve_no_listener(
        host=feasibility.PINNED_MANAGER_HOST,
        port=feasibility.PINNED_MANAGER_PORT,
        socket_factory=lambda *_args: Socket(),
    )
    try:
        assert any(call[0] == "bind" for call in calls)
        assert not any(call[0] == "listen" for call in calls)
    finally:
        reservation.close()
    assert calls[-1] == ("close",)


def test_registry_gate_requires_every_replica_group() -> None:
    _f, executor = _modules()

    class Record:
        def __init__(self, replica_id: int) -> None:
            self.replica_id = replica_id

    class Registry:
        def __init__(self, count: int) -> None:
            self.records = tuple(Record(replica) for replica in range(count))

    executor.assert_exactly_31_registered(Registry(31))
    with pytest.raises(executor.LocalExecutorError, match="exactly the 31"):
        executor.assert_exactly_31_registered(Registry(30))
