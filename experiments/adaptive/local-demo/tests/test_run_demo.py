from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "run_demo.py"
SPEC = importlib.util.spec_from_file_location("adaptive_local_demo", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
demo = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = demo
SPEC.loader.exec_module(demo)


def marker(event: str, replica: int, epoch: int, root: int) -> str:
    fields = (
        f"{event} replica={replica} epoch={epoch} tree=0 root={root} "
        f"height={8 if epoch == 0 else 9}"
    )
    return f"2026-07-14 10:00:00 [hotstuff info] KAURI_DEMO {fields}"


def passing_logs() -> dict[int, str]:
    logs: dict[int, str] = {}
    for replica in demo.REPLICA_IDS:
        logs[replica] = "\n".join(
            [
                "KAURI_DEMO bootstrap_staged "
                f"replica={replica} active_epoch=0 active_root=0 "
                "successor_epoch=1 successor_root=1 activation_height=8 "
                "epoch_digest=epoch1-digest",
                marker("commit", replica, 0, 0),
                "KAURI_DEMO epoch_activated "
                f"replica={replica} epoch=1 tree=0 root=1 height=8 "
                "epoch_digest=epoch1-digest",
                marker("commit", replica, 1, 1),
            ]
        )
    return logs


def reputation_marker(
    *,
    reporter: int = 0,
    target: int = 1,
    outcome: str = "response",
    delta: int = 1,
    score: int = 1,
) -> str:
    return (
        "2026-07-17 10:00:00 [hotstuff info] "
        "KAURI_REPUTATION update "
        f"reporter={reporter} target={target} outcome={outcome} "
        f"delta={delta} score={score}"
    )


class FakeProcess:
    def __init__(self) -> None:
        self.return_code: int | None = None

    def poll(self) -> int | None:
        return self.return_code

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.return_code is None:
            raise subprocess.TimeoutExpired("fake", 0)
        return self.return_code


class FakeLog:
    def close(self) -> None:
        pass


def record(name: str, pid: int, pgid: int) -> object:
    return demo.ProcessRecord(
        name,
        pid,
        pgid,
        ("/safe/binary",),
        Path(f"{name}.log"),
        FakeProcess(),
        FakeLog(),
    )


class MarkerTests(unittest.TestCase):
    def test_parser_finds_marker_after_logger_prefix(self) -> None:
        parsed = demo.parse_marker(
            "timestamp [hotstuff info] KAURI_DEMO "
            "commit replica=2 epoch=1 tree=0 root=1 height=9",
            7,
        )

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.event, "commit")
        self.assertEqual(parsed.line_number, 7)
        self.assertEqual(parsed.fields["replica"], "2")
        self.assertEqual(parsed.fields["root"], "1")

    def test_parser_rejects_malformed_markers(self) -> None:
        malformed = (
            "KAURI_DEMO replica=0",
            "KAURI_DEMO start broken",
            "KAURI_DEMO start replica=0 replica=1",
        )
        for line in malformed:
            with self.subTest(line=line):
                with self.assertRaises(demo.DemoError):
                    demo.parse_marker(line)


class ReputationMarkerTests(unittest.TestCase):
    def test_parser_retains_complete_manager_update(self) -> None:
        parsed = demo.parse_reputation_marker(reputation_marker(), 11)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.event, "update")
        self.assertEqual(parsed.line_number, 11)
        self.assertEqual(
            parsed.fields,
            {
                "reporter": "0",
                "target": "1",
                "outcome": "response",
                "delta": "1",
                "score": "1",
            },
        )

    def test_parser_rejects_incomplete_manager_update(self) -> None:
        incomplete = reputation_marker().replace(" score=1", "")

        with self.assertRaises(demo.DemoError):
            demo.parse_reputation_marker(incomplete)


class ReputationVerdictTests(unittest.TestCase):
    def test_manager_verdict_requires_complete_reputation_marker(self) -> None:
        verdict = demo.evaluate_reputation_log(reputation_marker())

        self.assertTrue(verdict.passed)
        self.assertEqual(verdict.reasons, ())
        self.assertEqual(
            verdict.evidence["updates"],
            [
                {
                    "reporter": 0,
                    "target": 1,
                    "outcome": "response",
                    "delta": 1,
                    "score": 1,
                }
            ],
        )

    def test_manager_verdict_rejects_missing_reputation_marker(self) -> None:
        verdict = demo.evaluate_reputation_log(
            "2026-07-17 10:00:00 manager ready"
        )

        self.assertFalse(verdict.passed)
        self.assertIn(
            "manager: missing KAURI_REPUTATION update",
            verdict.reasons,
        )

    def test_manager_verdict_rejects_inconsistent_score_progression(self) -> None:
        manager_log = "\n".join(
            [
                reputation_marker(score=1),
                reputation_marker(score=3),
            ]
        )

        verdict = demo.evaluate_reputation_log(manager_log)

        self.assertFalse(verdict.passed)
        self.assertIn(
            "manager: target 1 score jumped from 1 to 3 on line 2",
            verdict.reasons,
        )

    def test_manager_verdict_rejects_unknown_members(self) -> None:
        verdict = demo.evaluate_reputation_log(
            reputation_marker(reporter=0, target=99)
        )

        self.assertFalse(verdict.passed)
        self.assertIn(
            "manager: reporter-target pair is outside demo membership on line 1",
            verdict.reasons,
        )

    def test_manager_verdict_preserves_late_correction_sequence(self) -> None:
        manager_log = "\n".join(
            [
                reputation_marker(
                    outcome="timeout", delta=-1, score=-1
                ),
                reputation_marker(
                    outcome="response", delta=1, score=0
                ),
            ]
        )

        verdict = demo.evaluate_reputation_log(manager_log)

        self.assertTrue(verdict.passed)
        self.assertEqual(
            [update["score"] for update in verdict.evidence["updates"]],
            [-1, 0],
        )
        self.assertEqual(
            {
                (update["reporter"], update["target"])
                for update in verdict.evidence["updates"]
            },
            {(0, 1)},
        )

    def test_combined_verdict_retains_narrow_claim_boundary(self) -> None:
        verdict = demo.combine_live_verdicts(
            demo.evaluate_logs(passing_logs()),
            demo.evaluate_reputation_log(reputation_marker()),
        )

        self.assertTrue(verdict.passed)
        self.assertEqual(
            verdict.evidence["claim_boundary"],
            {
                "environment": "trusted-local",
                "protocol_mode": "adaptive_v1",
                "evidence_scope": "transition-only",
                "does_not_establish": [
                    "crash-recovery",
                    "adaptive-v2-activation",
                ],
            },
        )


class VerdictTests(unittest.TestCase):
    def test_requires_shared_ordered_epoch_transition(self) -> None:
        verdict = demo.evaluate_logs(passing_logs())

        self.assertTrue(verdict.passed)
        self.assertEqual(verdict.reasons, ())
        self.assertEqual(
            verdict.evidence["shared_digests"],
            {
                "activation": ["epoch1-digest"],
                "bootstrap": ["epoch1-digest"],
            },
        )

    def test_fails_when_one_replica_has_no_post_activation_commit(self) -> None:
        logs = passing_logs()
        logs[3] = "\n".join(logs[3].splitlines()[:-1])

        verdict = demo.evaluate_logs(logs)

        self.assertFalse(verdict.passed)
        self.assertIn(
            "replica 3: missing ordered epoch1_commit", verdict.reasons
        )

    def test_fails_on_digest_divergence(self) -> None:
        logs = passing_logs()
        lines = logs[2].splitlines()
        lines[2] = lines[2].replace("epoch1-digest", "forked-digest")
        logs[2] = "\n".join(lines)

        verdict = demo.evaluate_logs(logs)

        self.assertFalse(verdict.passed)
        self.assertIn(
            "activation: replicas do not share one digest", verdict.reasons
        )
        self.assertIn(
            "bootstrap and activation epoch digests differ", verdict.reasons
        )

    def test_fails_on_fatal_markers_or_text(self) -> None:
        fatal_lines = (
            "KAURI_DEMO fatal replica=1 reason=bootstrap_failed",
            "terminate called after throwing an exception",
            "Segmentation fault: 11",
        )
        for fatal_line in fatal_lines:
            with self.subTest(fatal_line=fatal_line):
                logs = passing_logs()
                logs[1] += f"\n{fatal_line}\n"
                verdict = demo.evaluate_logs(logs)
                self.assertFalse(verdict.passed)
                self.assertTrue(
                    any("fatal" in reason for reason in verdict.reasons)
                )

    def test_fails_on_protocol_rejection_diagnostics(self) -> None:
        diagnostics = (
            "invalid adaptive epoch consensus message",
            "malformed adaptive epoch consensus message",
            "Rejecting malformed proposal payload",
            "Rejecting invalid vote envelope",
            "dropping invalid block from peer",
        )
        for diagnostic in diagnostics:
            with self.subTest(diagnostic=diagnostic):
                logs = passing_logs()
                logs[0] += f"\n{diagnostic}\n"

                verdict = demo.evaluate_logs(logs)

                self.assertFalse(verdict.passed)
                self.assertIn(
                    "replica 0: protocol rejection text", verdict.reasons
                )

    def test_fails_when_boundary_heights_disagree(self) -> None:
        logs = passing_logs()
        lines = logs[0].splitlines()
        lines[2] = lines[2].replace("height=8", "height=7")
        logs[0] = "\n".join(lines)

        verdict = demo.evaluate_logs(logs)

        self.assertFalse(verdict.passed)
        self.assertIn(
            "replica 0: activated height 7 does not equal activation height 8",
            verdict.reasons,
        )

    def test_fails_when_replicas_choose_different_activation_heights(self) -> None:
        logs = passing_logs()
        lines = logs[3].splitlines()
        lines[0] = lines[0].replace(
            "activation_height=8", "activation_height=12"
        )
        lines[1] = lines[1].replace("height=8", "height=12")
        lines[2] = lines[2].replace("height=8", "height=12")
        lines[3] = lines[3].replace("height=9", "height=13")
        logs[3] = "\n".join(lines)

        verdict = demo.evaluate_logs(logs)

        self.assertFalse(verdict.passed)
        self.assertIn(
            "replicas do not share one activation height", verdict.reasons
        )

    def test_rejects_epoch1_commit_at_activation_boundary(self) -> None:
        logs = passing_logs()
        lines = logs[1].splitlines()
        lines[3] = lines[3].replace("height=9", "height=8")
        logs[1] = "\n".join(lines)

        verdict = demo.evaluate_logs(logs)

        self.assertFalse(verdict.passed)
        self.assertIn(
            "replica 1: epoch1 commit height 8 must be greater than "
            "activation height 8",
            verdict.reasons,
        )

    def test_rejects_epoch1_label_on_epoch0_boundary_commit(self) -> None:
        logs = passing_logs()
        lines = logs[2].splitlines()
        lines[1] = lines[1].replace("epoch=0", "epoch=1")
        logs[2] = "\n".join(lines)

        verdict = demo.evaluate_logs(logs)

        self.assertFalse(verdict.passed)
        self.assertIn(
            "replica 2: missing ordered epoch0_commit", verdict.reasons
        )

    def test_uses_last_epoch0_commit_before_activation(self) -> None:
        logs = passing_logs()
        lines = logs[0].splitlines()
        earlier = marker("commit", 0, 0, 0).replace(
            "height=8", "height=7"
        )
        lines.insert(1, earlier)
        logs[0] = "\n".join(lines)

        self.assertTrue(demo.evaluate_logs(logs).passed)


class CleanupTests(unittest.TestCase):
    def test_signals_only_recorded_groups_in_escalation_order(self) -> None:
        records = [
            record("manager", 1001, 1001),
            record("replica", 1002, 1002),
        ]
        calls: list[tuple[int, signal.Signals]] = []

        def killpg(pgid: int, sig: signal.Signals) -> None:
            calls.append((pgid, sig))
            if sig == signal.SIGKILL:
                for item in records:
                    if item.pgid == pgid:
                        item.process.return_code = -int(sig)

        with (
            mock.patch.object(demo.os, "getpgrp", return_value=999),
            mock.patch.object(demo.os, "killpg", side_effect=killpg),
            mock.patch.object(demo.time, "sleep"),
            mock.patch.object(demo.time, "monotonic", return_value=0.0),
        ):
            demo.terminate_recorded_groups(
                records, int_grace=0, term_grace=0
            )

        self.assertEqual(
            calls,
            [
                (1001, signal.SIGINT),
                (1002, signal.SIGINT),
                (1001, signal.SIGTERM),
                (1002, signal.SIGTERM),
                (1001, signal.SIGKILL),
                (1002, signal.SIGKILL),
            ],
        )

    def test_refuses_the_launchers_group(self) -> None:
        with (
            mock.patch.object(demo.os, "getpgrp", return_value=1001),
            mock.patch.object(demo.os, "killpg") as killpg,
        ):
            with self.assertRaisesRegex(demo.DemoError, "launcher"):
                demo.terminate_recorded_groups(
                    [record("replica", 1001, 1001)]
                )
            killpg.assert_not_called()


class ExitDiagnosticTests(unittest.TestCase):
    def test_signal_exit_records_direct_and_shell_return_codes(self) -> None:
        self.assertEqual(
            demo.process_exit_status(-signal.SIGPIPE),
            {
                "popen_return_code": -signal.SIGPIPE,
                "shell_return_code": 128 + signal.SIGPIPE,
                "signaled": True,
                "signal_number": signal.SIGPIPE,
                "signal_name": "SIGPIPE",
            },
        )

    def test_normal_exit_is_not_reported_as_a_signal(self) -> None:
        self.assertEqual(
            demo.process_exit_status(7),
            {
                "popen_return_code": 7,
                "shell_return_code": 7,
                "signaled": False,
                "signal_number": None,
                "signal_name": None,
            },
        )

    def test_first_observed_exit_includes_command_and_identity(self) -> None:
        first = record("replica-0", 1200, 1200)
        second = record("replica-1", 1201, 1201)
        first.process.return_code = -signal.SIGPIPE
        second.process.return_code = 3
        observations: list[dict[str, object]] = []

        newly_observed = demo.observe_process_exits(
            [first, second], observations
        )

        self.assertEqual(len(newly_observed), 2)
        self.assertIs(observations[0]["first_observed"], True)
        self.assertIs(observations[1]["first_observed"], False)
        self.assertEqual(observations[0]["name"], "replica-0")
        self.assertEqual(observations[0]["command"], ["/safe/binary"])
        self.assertEqual(observations[0]["shell_return_code"], 141)
        self.assertEqual(
            demo.observe_process_exits([first, second], observations), []
        )

    def test_cleanup_exit_is_not_labeled_as_first_runtime_exit(self) -> None:
        cleanup_record = record("manager", 1200, 1200)
        cleanup_record.process.return_code = 0
        observations: list[dict[str, object]] = []

        demo.observe_process_exits(
            [cleanup_record], observations, phase="cleanup"
        )

        self.assertEqual(observations[0]["phase"], "cleanup")
        self.assertIs(observations[0]["first_observed"], False)


class CommandTests(unittest.TestCase):
    def test_commands_use_no_shell_or_global_process_killers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            replica = demo.build_replica_command(
                root / "hotstuff-app",
                root / "main.conf",
                root / "replica.conf",
            )
            manager = demo.build_manager_command(
                root / "hotstuff-client",
                root / "main.conf",
                root / "epoch0.tree",
                root / "timeouts.empty",
            )

        lowered = {Path(token).name.lower() for token in replica + manager}
        self.assertFalse(lowered & demo.FORBIDDEN_COMMAND_TOKENS)
        self.assertTrue(all("workload" not in token for token in replica))
        self.assertNotIn("--notls", replica)
        self.assertIn("--default_epoch", manager)
        self.assertNotIn("--mock", manager)

    def test_spawn_uses_new_session_and_argument_vector(self) -> None:
        captured: dict[str, object] = {}

        class Spawned(FakeProcess):
            pid = 8080

        def popen(command: list[str], **kwargs: object) -> Spawned:
            captured["command"] = command
            captured.update(kwargs)
            return Spawned()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch.object(demo.subprocess, "Popen", side_effect=popen),
                mock.patch.object(demo.os, "getpgid", side_effect=lambda pid: pid),
            ):
                created = demo.spawn_process(
                    "replica-0",
                    ("/safe/hotstuff-app", "--conf", "/safe/main.conf"),
                    root / "replica.log",
                    root,
                )
            created.log_handle.close()

        self.assertEqual(
            captured["command"],
            ["/safe/hotstuff-app", "--conf", "/safe/main.conf"],
        )
        self.assertIs(captured["start_new_session"], True)
        self.assertNotIn("shell", captured)
        self.assertEqual(created.pid, 8080)
        self.assertEqual(created.pgid, 8080)

    def test_generated_replica_configs_preserve_tls_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            run_directory = root / "run"
            repository.mkdir()
            run_directory.mkdir()
            repository.joinpath("hotstuff.conf").write_text(
                "".join(
                    f"replica = 127.0.0.1:{22000 + replica}, "
                    f"public-{replica}, peer-{replica}\n"
                    for replica in demo.REPLICA_IDS
                )
            )
            for replica in demo.REPLICA_IDS:
                repository.joinpath(
                    f"hotstuff-sec{replica}.conf"
                ).write_text(
                    f"privkey = private-{replica}\n"
                    f"tls-privkey = tls-private-{replica}\n"
                    f"tls-cert = tls-cert-{replica}\n"
                    f"idx = {replica}\n"
                )

            _, replica_configs, _, _ = demo.write_configs(
                repository, run_directory, 23100, 24100, 8
            )

            for replica, config in enumerate(replica_configs):
                self.assertEqual(
                    config.read_text().splitlines(),
                    [
                        f"privkey = private-{replica}",
                        f"tls-privkey = tls-private-{replica}",
                        f"tls-cert = tls-cert-{replica}",
                        f"idx = {replica}",
                    ],
                )


class LauncherTests(unittest.TestCase):
    def test_launcher_builds_both_demo_targets_before_running(self) -> None:
        launcher = (MODULE_PATH.parent / "run.sh").read_text()

        self.assertIn('-S "$repository" -B "$build_dir"', launcher)
        self.assertIn("--target hotstuff-app hotstuff-client", launcher)
        self.assertLess(
            launcher.index("--target hotstuff-app hotstuff-client"),
            launcher.index('exec python3 "$script_dir/run_demo.py"'),
        )

    def test_launcher_defaults_can_be_overridden_safely(self) -> None:
        launcher = (MODULE_PATH.parent / "run.sh").read_text()

        self.assertIn("KAURI_CMAKE", launcher)
        self.assertIn("KAURI_BUILD_DIR", launcher)
        self.assertIn("KAURI_BUILD_JOBS", launcher)
        self.assertIn('"$@"', launcher)
        lowered_tokens = {
            token.strip('"').lower() for token in launcher.split()
        }
        self.assertFalse(lowered_tokens & demo.FORBIDDEN_COMMAND_TOKENS)


class ArtifactTests(unittest.TestCase):
    def test_process_cwd_has_no_implicit_default_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            config = root / "run" / "config"
            run_directory = config.parent
            repository.mkdir()
            config.mkdir(parents=True)
            main = config / "hotstuff.gen.conf"
            main.write_text("explicit config\n")

            selected = demo.isolated_process_cwd(
                run_directory, repository, main
            )

            self.assertEqual(selected, run_directory.resolve())
            self.assertNotEqual(selected, repository.resolve())
            self.assertNotEqual(selected, config.resolve())
            self.assertFalse((selected / "hotstuff.gen.conf").exists())

    def test_results_directories_are_unique(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = demo.create_run_directory(root)
            second = demo.create_run_directory(root)
            self.assertNotEqual(first, second)
            self.assertTrue(first.is_dir())
            self.assertTrue(second.is_dir())

    def test_epoch_files_pin_expected_roots(self) -> None:
        local_directory = MODULE_PATH.parent
        self.assertEqual(
            (local_directory / "epoch0.tree").read_text().split()[2], "0"
        )
        self.assertEqual(
            (local_directory / "epoch1.tree").read_text().split()[2], "1"
        )

    def test_manifest_entry_records_pid_pgid_and_argument_vector(self) -> None:
        entry = record("replica-0", 1200, 1200).manifest_entry()
        self.assertTrue(json.dumps(entry))
        self.assertEqual(entry["pid"], 1200)
        self.assertEqual(entry["pgid"], 1200)
        self.assertEqual(entry["command"], ["/safe/binary"])


class PortCheckTests(unittest.TestCase):
    def test_preflight_binds_but_postflight_checks_listeners(self) -> None:
        actions: list[tuple[str, int]] = []

        class FakeSocket:
            def bind(self, address: tuple[str, int]) -> None:
                actions.append(("bind", address[1]))
                if address[1] == 111:
                    raise OSError("occupied")

            def settimeout(self, timeout: float) -> None:
                del timeout

            def connect_ex(self, address: tuple[str, int]) -> int:
                actions.append(("connect", address[1]))
                return 0 if address[1] == 211 else 1

            def close(self) -> None:
                pass

            def __enter__(self) -> "FakeSocket":
                return self

            def __exit__(self, *args: object) -> None:
                del args

        with mock.patch.object(
            demo.socket, "socket", side_effect=lambda *args: FakeSocket()
        ):
            self.assertEqual(demo.check_ports_free([111, 112]), [111])
            self.assertEqual(demo.check_ports_listening([211, 212]), [211])

        self.assertEqual(
            actions,
            [
                ("bind", 111),
                ("bind", 112),
                ("connect", 211),
                ("connect", 212),
            ],
        )


if __name__ == "__main__":
    unittest.main()
