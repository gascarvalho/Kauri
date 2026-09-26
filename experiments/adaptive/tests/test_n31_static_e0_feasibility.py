"""Tests for the no-launch W16 native feasibility preflight."""

from __future__ import annotations

import hashlib
import importlib
from pathlib import Path

import pytest


def _module():
    return importlib.import_module("experiments.adaptive.kauri_experiment.n31_static_e0_feasibility")


def test_frozen_plan_has_exact_twenty_one_static_trees() -> None:
    module = _module()
    plan = module.frozen_plan(arm="fast-roots")
    assert plan.treegen_bytes.count(b"\n") == 21
    assert plan.treegen_sha256 == hashlib.sha256(plan.treegen_bytes).hexdigest()
    assert plan.manager_endpoint == "127.0.0.1:27991"


def test_preflight_fails_closed_when_checkout_is_dirty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _module()
    binary = tmp_path / "binary"
    binary.write_text("x", encoding="ascii")
    binary.chmod(0o755)

    def fake_git(_repository: Path, *arguments: str) -> str:
        if arguments == ("branch", "--show-current"):
            return "feature/adaptive-epoch-throughput\n"
        if arguments == ("status", "--porcelain"):
            return " M test/example.cpp\n"
        raise AssertionError(arguments)

    monkeypatch.setattr(module, "_git", fake_git)
    with pytest.raises(module.StaticE0FeasibilityError, match="dirty"):
        module.preflight(
            repository=tmp_path,
            app_binary=binary,
            keygen_binary=binary,
            tls_keygen_binary=binary,
            native_digest_binary=binary,
            arm="slow-roots",
        )


def test_preflight_binds_clean_revision_artifact_and_no_listener(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _module()
    binary = tmp_path / "binary"
    binary.write_text("x", encoding="ascii")
    binary.chmod(0o755)

    def fake_git(_repository: Path, *arguments: str) -> str:
        values = {
            ("branch", "--show-current"): "feature/adaptive-epoch-throughput\n",
            ("status", "--porcelain"): "",
            ("rev-parse", "HEAD"): "a" * 40 + "\n",
            ("rev-parse", "origin/feature/adaptive-epoch-throughput"): "a" * 40 + "\n",
        }
        return values[arguments]

    seen: list[tuple[str, int]] = []
    monkeypatch.setattr(module, "_git", fake_git)
    receipt = module.preflight(
        repository=tmp_path,
        app_binary=binary,
        keygen_binary=binary,
        tls_keygen_binary=binary,
        native_digest_binary=binary,
        arm="fast-roots",
        endpoint_probe=seen.append,
    )
    assert receipt["verdict"] == "PREFLIGHT_OK_NO_EXECUTION"
    assert receipt["tree_count"] == 21
    assert receipt["binary_sha256"] == {
        name: hashlib.sha256(b"x").hexdigest()
        for name in ("app", "keygen", "tls_keygen", "native_digest")
    }
    assert seen == [("127.0.0.1", 27991)]


def test_prepared_config_uses_file_treegen_and_pinned_endpoint(tmp_path: Path) -> None:
    module = _module()
    plan = module.frozen_plan(arm="slow-roots")
    keys = [{"pub": "p", "sec": "s"} for _ in range(31)]
    tls = [{"crt": "c", "sec": "s", "cid": "i"} for _ in range(32)]
    source_instances = {f"replica-{replica}": f"instance-{replica}" for replica in range(31)}
    paths = module.write_prepared_inputs(
        plan=plan,
        directory=tmp_path,
        bls=keys,
        tls=tls,
        issuer={"pub": "issuer", "sec": "secret"},
        run_id="test-run",
        source_instances=source_instances,
    )
    config = Path(paths["main"]).read_text(encoding="utf-8")
    assert "tree-generation = file\n" in config
    assert f"tree-generation-fpath = {paths['treegen']}\n" in config
    assert "epoch-manager-address = 127.0.0.1:27991\n" in config
    assert Path(paths["treegen"]).read_bytes() == plan.treegen_bytes


def _source_instances() -> dict[str, str]:
    return {f"replica-{replica}": f"instance-{replica}" for replica in range(31)}


def _native_event(
    *,
    replica: int,
    sequence: int,
    event_type: str,
    payload: dict[str, object],
    run_id: str = "w16-source-bound-test",
    source_instance: str | None = None,
) -> dict[str, object]:
    return {
        "event_schema_version": 1,
        "run_id": run_id,
        "source_kind": "replica",
        "source_id": f"replica-{replica}",
        "source_instance": source_instance or f"instance-{replica}",
        "source_sequence": sequence,
        "source_monotonic_ns": sequence * 100,
        "event_type": event_type,
        "payload": payload,
    }


def _complete_native_streams() -> tuple[dict[str, list[dict[str, object]]], str, dict[str, str]]:
    digest = "d" * 64
    instances = _source_instances()
    streams: dict[str, list[dict[str, object]]] = {}
    for replica in range(31):
        events = [
            _native_event(
                replica=replica,
                sequence=1,
                event_type="process.ready",
                payload={"exit_status": None},
            )
        ]
        events.extend(
            _native_event(
                replica=replica,
                sequence=tree + 2,
                event_type="adaptive.configuration_active",
                payload={
                    "epoch_number": 0,
                    "tree_id": tree,
                    "epoch_digest": digest,
                    "observer_replica": replica,
                    "global_quorum": 21,
                },
            )
            for tree in range(21)
        )
        events.append(
            _native_event(
                replica=replica,
                sequence=23,
                event_type="adaptive_v2_reporting_terminal",
                payload={
                    "reason": "shared_outbox_delivery_failed",
                    "terminal_monotonic_ns": 2300,
                },
            )
        )
        streams[f"replica-{replica}"] = events
    streams["replica-2"].append(
        _native_event(
            replica=2,
            sequence=24,
            event_type="block.committed",
            payload={
                "block_height": 42,
                "block_hash": "c" * 64,
                "parent_hash": "b" * 64,
                "transaction_count": 1000,
                "designated_observer": True,
                "decision_proof": {
                    "epoch_number": 0,
                    "tree_id": 20,
                    "epoch_digest": digest,
                    "block_hash": "c" * 64,
                },
                "view_generation": 1,
                "commit_batch_index": 0,
            },
        )
    )
    streams["replica-2"].append(
        _native_event(
            replica=2,
            sequence=25,
            event_type="block.committed",
            payload={
                "block_height": 43,
                "block_hash": "e" * 64,
                "parent_hash": "c" * 64,
                "transaction_count": 1000,
                "designated_observer": True,
                "decision_proof": {
                    "epoch_number": 0,
                    "tree_id": 0,
                    "epoch_digest": digest,
                    "block_hash": "e" * 64,
                },
                "view_generation": 1,
                "commit_batch_index": 0,
            },
        )
    )
    return streams, digest, instances


def _gate(module, streams: dict[str, list[dict[str, object]]], digest: str,
          instances: dict[str, str]) -> tuple[bool, str]:
    return module.event_gate(
        streams,
        observer=2,
        run_id="w16-source-bound-test",
        source_instances=instances,
        epoch_digest=digest,
        expected_terminal_reason="shared_outbox_delivery_failed",
    )


def test_source_bound_event_gate_accepts_only_complete_native_envelopes() -> None:
    module = _module()
    streams, digest, instances = _complete_native_streams()
    passed, detail = _gate(module, streams, digest, instances)
    assert passed is True
    assert "source-bound" in detail


def test_terminal_may_precede_full_tree_cycle_but_commit_chain_must_follow_both() -> None:
    module = _module()
    streams, digest, instances = _complete_native_streams()
    for events in streams.values():
        terminal = next(event for event in events
                        if event["event_type"] == "adaptive_v2_reporting_terminal")
        events.remove(terminal)
        events.insert(2, terminal)
        for sequence, event in enumerate(events, 1):
            event["source_sequence"] = sequence
            event["source_monotonic_ns"] = sequence * 100
    assert _gate(module, streams, digest, instances)[0] is True

    # An isolated post-terminal commit before the cycle is insufficient.
    streams["replica-2"].pop()
    assert _gate(module, streams, digest, instances)[0] is False


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (
            lambda streams: streams["replica-0"][0].__setitem__("run_id", "foreign-run"),
            "unbound",
        ),
        (
            lambda streams: streams["replica-5"][7].__setitem__("source_sequence", 99),
            "gapped",
        ),
        (
            lambda streams: streams["replica-8"][0].__setitem__("source_instance", "foreign-instance"),
            "unbound",
        ),
        (
            lambda streams: streams["replica-10"][8]["payload"].__setitem__("tree_id", 5),
            "ordered Epoch-0",
        ),
        (
            lambda streams: streams["replica-12"][4]["payload"].__setitem__("epoch_digest", "e" * 64),
            "ordered Epoch-0",
        ),
        (
            lambda streams: streams["replica-14"][22].__setitem__(
                "event_type", "adaptive_v2_convergence_failure"
            ),
            "terminal or convergence failure",
        ),
        (
            lambda streams: streams["replica-16"][22]["payload"].__setitem__(
                "reason", "unreviewed-terminal"
            ),
            "reviewed outcome",
        ),
        (
            lambda streams: streams["replica-2"][-2]["payload"].__setitem__(
                "designated_observer", False
            ),
            "no valid designated-observer commit chain",
        ),
    ],
)
def test_source_bound_event_gate_rejects_forged_gapped_and_malformed_evidence(
    mutate, expected: str
) -> None:
    module = _module()
    streams, digest, instances = _complete_native_streams()
    mutate(streams)
    passed, detail = _gate(module, streams, digest, instances)
    assert passed is False
    assert expected in detail


def test_event_gate_fails_closed_without_all_source_bindings() -> None:
    module = _module()
    streams = {}
    for replica in range(31):
        events = [{"event_type": "process.ready", "source_monotonic_ns": 1}]
        events.extend(
            {
                "event_type": "adaptive.configuration_active",
                "source_monotonic_ns": 10 + tree,
                "payload": {"tree_id": tree, "epoch_digest": "a" * 64},
            }
            for tree in range(21)
        )
        streams[f"replica-{replica}"] = events
        events.append(
            {"event_type": "adaptive_v2_reporting_terminal", "source_monotonic_ns": 100}
        )
    streams["replica-2"].append(
        {"event_type": "block.committed", "source_monotonic_ns": 101}
    )
    assert module.event_gate(streams, observer=2)[0] is False
    streams["replica-7"] = streams["replica-7"][:-1]
    assert module.event_gate(streams, observer=2)[0] is False


def test_unreviewed_execute_path_stays_disabled_without_creating_output(
    tmp_path: Path,
) -> None:
    module = _module()
    output = tmp_path / "never-created"
    with pytest.raises(module.StaticE0FeasibilityError, match="disabled"):
        module.execute_once(
            plan=module.frozen_plan(arm="slow-roots"),
            directory=output,
            app_binary=tmp_path / "app",
            keygen_binary=tmp_path / "keygen",
            tls_keygen_binary=tmp_path / "tls-keygen",
        )
    assert not output.exists()
