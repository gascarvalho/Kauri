#!/usr/bin/env python3
"""One-shot v8 process execution and offline replay of a pinned Linux archive.

The operator pins external approval bytes.  Live authority is collected by
actual native commands, never by an injected observation mapping.  Both arms
retain the frozen profile and metric.  Pilots are excluded from comparisons.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import socket
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
KAURI = HERE.parents[2]
SOURCES = ("adaptive-manager", *(f"replica-{i}" for i in range(7)))
RAW_RECEIPT = "runtime/sustained-role-v8-live-raw-receipt.json"
POST_SCOPE = "runtime/sustained-role-v8-live-post-scope.json"
TERMINAL = "runtime/sustained-role-v8-live-terminal.json"
ABORT = "runtime/sustained-role-v8-live-abort.json"
LIVE = "runtime/sustained-role-v8-live-authority.json"
STARTED = "runtime/sustained-role-v8-live-child-started.json"
FIXED_APPROVAL = "runtime/sustained-role-v8-fixed-launch-authorization.json"
FIXED_INTENT = "runtime/sustained-role-v8-fixed-launch-intent.json"
BOOKING = "32usk1i80tieqq3e8jterd8as4"


class LiveError(ValueError):
    pass


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


inputs = load("w19_live_inputs", "sustained_role_v8_full_input_producer.py")
child = load("w19_live_child", "sustained_role_v8_adaptive_child_execute.py")
old = load("w19_live_adaptive_helpers", "sustained_role_adaptive_e1_launcher.py")
scope = load("w19_live_scope_helpers", "sustained_role_campaign_launch.py")
operator = load("w19_live_signed_replay", "sustained_role_v8_campaign_operator.py")
fixed_replay = load("w19_live_fixed_replay", "sustained_role_v8_fixed_e0_raw_replay.py")
local = load("w19_live_cleanup", "run_local.py")
causality = load("w19_live_inherited_selection_causality", "sustained_role_validator.py")
base = inputs.prep.local.base


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii") + b"\n"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path):
    if path.is_symlink() or not path.is_file():
        raise LiveError("required regular artifact is absent")
    raw = path.read_bytes()
    value = inputs._json(raw, path.name)
    return value, raw


def write(path, value):
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as out:
        out.write(canonical(value)); out.flush(); os.fsync(out.fileno())


def clock():
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)


def paths(arm):
    return (inputs.PLAN, inputs.REQUEST, child.APPROVAL, child.INTENT) if arm == "adaptive_e1" else (
        inputs.FIXED_PLAN, inputs.FIXED_REQUEST, Path(FIXED_APPROVAL), Path(FIXED_INTENT))


def checked(root, descriptor):
    return inputs._descriptor_file(root, descriptor, label="live archive artifact")


def booking_row(text, *, now):
    rows = [[field.strip() for field in line.split("|")[1:-1]]
            for line in text.splitlines() if line.startswith("|")]
    matches = [row for row in rows if row[:4] == [BOOKING, "proteina02", "gascarvalho", "EXCLUSIVE"]]
    if len(matches) != 1 or len(matches[0]) != 7:
        raise LiveError("exact active exclusive booking is absent")
    row = matches[0]
    start, end = [datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("Europe/Lisbon"))
                  for value in row[5:7]]
    if (row[5:7] != ["2026-10-02 09:30", "2026-10-02 17:00"] or
            not start <= now < end or (end - now).total_seconds() < 260):
        raise LiveError("booking is inactive or lacks cell/cleanup reserve")
    return row


def collect_authority(root, plan):
    """Only production subprocess and /proc observations establish live state."""
    if socket.gethostname() != "proteina02" or pwd.getpwuid(os.getuid()).pw_name != "gascarvalho":
        raise LiveError("wrong live host or Unix identity")
    snapshot = base.verify_repository_state(KAURI)
    revision = inputs.prep.local._require_clean_snapshot(snapshot)
    if revision != plan["repository_revision"]:
        raise LiveError("live source differs from sealed plan")
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    build = child.build_binding.bind(root=root, plan=plan, observed_revision=revision,
                                    target_host="proteina02", linux_boot_id=boot)
    receipt, _ = read(checked(root, plan["artifacts"]["build_receipt"]))
    log = Path(receipt["build_log_path"])
    if log.is_symlink() or not log.is_file() or sha(log.read_bytes()) != receipt["build_log_sha256"]:
        raise LiveError("actual clean-build log differs from its receipt")
    archived_log = root / "runtime/sustained-role-v8-clean-build.log"
    with archived_log.open("xb") as out:
        out.write(log.read_bytes())
    cache = (KAURI / "build-adaptive/CMakeCache.txt").read_bytes()
    if (b"CMAKE_BUILD_TYPE:STRING=" + receipt["build_type"].encode() + b"\n" not in cache or
            b"HOTSTUFF_TWO_STEP:BOOL=OFF\n" not in cache):
        raise LiveError("effective native build type/three-step settings differ from receipt")
    archived_cache = root / "runtime/sustained-role-v8-CMakeCache.txt"
    with archived_cache.open("xb") as out:
        out.write(cache)
    command = ["gsd_manager", "-N", "proteina02", "booking", "ls", "-c", "-u", "gascarvalho", "-m", "exclusive"]
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=25)
    now = datetime.now(timezone.utc)
    row = booking_row(result.stdout, now=now)
    return {"schema_version": 1, "kind": "kauri-w19-v8-native-live-authority-v1",
            "host": "proteina02", "user": "gascarvalho", "linux_boot_id": boot,
            "repository_revision": revision, "build_binding": build,
            "build_log": inputs._desc(root, archived_log),
            "cmake_cache": inputs._desc(root, archived_cache),
            "booking_command": command, "booking_stdout": result.stdout,
            "booking_row": row, "observed_utc": now.isoformat(),
            "observed_monotonic_ns": clock(), "no_retry": True}


def unseal_manager(root, plan):
    """Restore private argv without storing or printing its credentials."""
    argv = list(next(row["argv"] for row in plan["processes"] if row["source_id"] == "adaptive-manager"))
    tls_path = inputs._safe_file(root, "config/tls-identities.txt", label="private TLS identities")
    issuer_path = inputs._safe_file(root, "config/issuer-identity.txt", label="private issuer identity")
    tls = base._parse_generator_output(tls_path.read_text(), expected_count=8,
                                      expected_fields=frozenset({"crt", "sec", "cid"}), label="private TLS identities")
    issuer = base._parse_generator_output(issuer_path.read_text(), expected_count=1,
                                         expected_fields=frozenset({"pub", "sec"}), label="private issuer identity")[0]
    launch, _ = read(checked(root, plan["artifacts"]["launch_arguments"]))
    effective = next(row["effective_options"] for row in launch["processes"] if row["source_id"] == "adaptive-manager")
    if (base.sha256_hex_value(tls[7]["crt"], "manager certificate") != effective["tls_certificate_sha256"] or
            base.sha256_hex_value(issuer["pub"], "issuer public key") != effective["issuer_public_key_sha256"] or
            checked(root, plan["artifacts"]["issuer_public_key"]).read_text() != issuer["pub"] + "\n"):
        raise LiveError("generated manager trust identity differs from sealed metadata")
    for option, value in (("--tls-privkey", tls[7]["sec"]), ("--tls-cert", tls[7]["crt"]),
                          ("--issuer-private-key", issuer["sec"])):
        argv[argv.index(option) + 1] = value
    for index in [i for i, value in enumerate(argv) if value == "--replica"]:
        rid, endpoint, _ = argv[index + 1].split(",")
        replica = int(rid)
        if base.sha256_hex_value(tls[replica]["crt"], "replica certificate") != effective["replica_tls_certificate_sha256"][replica]:
            raise LiveError("generated replica certificate differs from sealed metadata")
        config = checked(root, plan["artifacts"]["replica_configs"][replica])
        if inputs.prep.local._config_option(config, "tls-cert", "replica config") != tls[replica]["crt"]:
            raise LiveError("generated certificate differs from replica trust config")
        argv[index + 1] = ",".join((rid, endpoint, tls[replica]["crt"]))
    if base.normalized_manager_argv(argv) != next(row["argv"] for row in plan["processes"] if row["source_id"] == "adaptive-manager"):
        raise LiveError("unsealed manager changes non-private launch arguments")
    return tuple(argv)


def validate_approval(root, arm, external, expected_sha):
    plan_path, request_path, approval_path, intent_path = paths(arm)
    plan, _ = read(root / plan_path); request, request_raw = read(root / request_path)
    inputs.verify(root, expected_request_sha256=sha(request_raw), arm=arm)
    if external.resolve().is_relative_to(root) or external.is_symlink():
        raise LiveError("launch approval must be external and regular")
    approval, approval_raw = read(external)
    kind = "kauri-n7-sustained-role-v8-adaptive-child-launch-authorization-v1" if arm == "adaptive_e1" else "kauri-n7-sustained-role-v8-fixed-launch-authorization-v1"
    expected = {"schema_version": 1, "kind": kind, "request_sha256": sha(request_raw),
                "plan_sha256": plan["plan_sha256"], "run_id": plan["run_id"], "no_retry": True}
    if approval != expected or sha(approval_raw) != expected_sha:
        raise LiveError("external approval differs from exact pinned request")
    return plan, approval, approval_raw


def first_anchor(streams):
    for event in streams.get("replica-1", []):
        if event.get("event_type") == "fault.contribution_opportunity":
            identity = operator.raw_replay._opportunity(event)
            if identity[1:3] != ("0", "4") or identity[17] != "internal":
                raise LiveError("first physical fault is not frozen E0 tree4 internal omission")
            return int(identity[10])
    return None


class Monitor:
    """Read each live byte once; offline replay later parses all sealed bytes."""
    def __init__(self, root):
        self.root = root
        self.offsets = {s: 0 for s in SOURCES}
        self.pending = {s: b"" for s in SOURCES}
        self.events = {s: [] for s in SOURCES}

    def poll(self):
        needed = {"block.committed", "block.commit_observed", "epoch.activated",
                  "fault.contribution_opportunity", "fault_window_armed"}
        for source in SOURCES:
            path = self.root / f"raw/{source}.jsonl"
            if not path.exists():
                continue
            if path.is_symlink() or path.stat().st_size < self.offsets[source]:
                raise LiveError("native live stream was replaced or truncated")
            with path.open("rb") as stream:
                stream.seek(self.offsets[source]); appended = stream.read()
                self.offsets[source] = stream.tell()
            chunks = (self.pending[source] + appended).split(b"\n")
            self.pending[source] = chunks.pop()
            for line in chunks:
                event = operator.raw_replay._object(line, source)
                if event.get("event_type") in needed:
                    self.events[source].append(event)
        return self.events


def run_child(root, arm, live_sha, intent_sha, approval_sha):
    plan_path, _, _, _ = paths(arm)
    plan, _ = read(root / plan_path)
    inputs.verify(root, expected_request_sha256=sha((root / paths(arm)[1]).read_bytes()), arm=arm)
    live, live_raw = read(root / LIVE)
    intent, intent_raw = read(root / paths(arm)[3])
    approval, approval_raw = read(root / paths(arm)[2])
    if (sha(live_raw) != live_sha or sha(intent_raw) != intent_sha or sha(approval_raw) != approval_sha or
            intent["approval_sha256"] != approval_sha or approval["plan_sha256"] != plan["plan_sha256"] or
            approval["request_sha256"] != sha((root / paths(arm)[1]).read_bytes())):
        raise LiveError("child authority differs from externally pinned scope inputs")
    unit = "kauri-w19-v8-" + plan["run_id"] + ".scope"
    cgroup = Path("/proc/self/cgroup").read_text()
    if not any(line.startswith("0::/") and line.endswith("/" + unit) for line in cgroup.splitlines()):
        raise LiveError("child is not inside its exact bounded cgroup-v2 scope")
    if (live["linux_boot_id"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip() or
            clock() - live["observed_monotonic_ns"] > 30_000_000_000):
        raise LiveError("live authority changed or expired before child spawn")
    write(root / STARTED, {"run_id": plan["run_id"], "monotonic_ns": clock(), "pid": os.getpid(), "cgroup": cgroup})
    records = []
    start, end = plan["scheduled_window"]["start_ns"], plan["scheduled_window"]["end_ns"]
    manager = unseal_manager(root, plan)
    try:
        if clock() >= start:
            raise LiveError("prearm schedule expired")
        (root / "raw").mkdir(mode=0o700); (root / "logs").mkdir(mode=0o700)
        if arm == "adaptive_e1":
            old._fault_window_arm_startup_path(root, manager)
        commands = {row["source_id"]: tuple(row["argv"]) for row in plan["processes"]}
        commands["adaptive-manager"] = manager
        for source in SOURCES:
            rid = None if source == "adaptive-manager" else int(source.split("-")[1])
            records.append(base.spawn_process(source, commands[source], root / f"logs/{source}.log", root, replica_id=rid))
        monitor = Monitor(root)
        while not old.fixed._all_seven_e0_common(monitor.poll(), start):
            if clock() >= start or any(record.process.poll() is not None for record in records):
                raise LiveError("eight live processes did not establish all-seven common E0 before start")
            time.sleep(.05)
        write(root / "runtime/sustained-role-v8-live-prearm.json", {"run_id": plan["run_id"], "prearm_ns": clock(), "scheduled_start_ns": start})
        if arm == "adaptive_e1":
            approval, _ = read(root / child.APPROVAL)
            e0, _ = read(checked(root, plan["artifacts"]["e0_identity"]))
            _, document = old._fault_window_arm_document(root, manager, approval,
                            e0_digest=e0["epoch_digest"], evidence_start_monotonic_ns=clock())
            while not old._fault_window_arm_ack(monitor.poll(), document):
                if clock() >= start:
                    raise LiveError("manager failed to acknowledge exact fault arm before scheduled start")
                time.sleep(.05)
        anchor = None
        while clock() < end:
            streams = monitor.poll()
            anchor = anchor or first_anchor(streams)
            if anchor is not None:
                if not start <= anchor <= start + 10_000_000_000 or end < anchor + 72_000_000_000:
                    raise LiveError("anchor misses frozen schedule reserve")
                if arm == "adaptive_e1" and clock() > anchor + 32_000_000_000 and not old._all_seven_e1_activated(streams, deadline_ns=anchor + 32_000_000_000):
                    raise LiveError("all-seven E1 activation missed frozen A+32 deadline")
            for record in records:
                rc = record.process.poll()
                if (record.name != "adaptive-manager" and rc is not None) or (rc is not None and rc != 0):
                    raise LiveError("native process exited before horizon or failed")
            time.sleep(.05)
        if anchor is None:
            raise LiveError("no physical omission anchor")
        manager_record = records[0]
        manager_deadline = time.monotonic() + 3
        while manager_record.process.poll() is None and time.monotonic() < manager_deadline:
            time.sleep(.05)
        if manager_record.process.poll() != 0:
            raise LiveError("manager did not naturally finish its native success terminal")
    finally:
        if records:
            local._cleanup_receipt(root, plan["run_id"], records, ())
    streams = base._event_streams(root)
    cleanup, _ = read(root / "runtime/cleanup-receipt.json")
    validate_lifecycle(streams, cleanup)
    make_raw_receipt(root, arm, plan, anchor)


def validate_lifecycle(streams, cleanup):
    if cleanup.get("complete") is not True or len(cleanup.get("processes", [])) != 8:
        raise LiveError("cleanup is incomplete")
    rows = {row["source_id"]: row for row in cleanup["processes"]}
    if set(rows) != set(SOURCES) or set(streams) != set(SOURCES):
        raise LiveError("lifecycle lacks eight unique sources")
    for source in SOURCES:
        row = rows[source]
        events = streams[source]
        if row["returncode"] != 0 or row["termination"] != "clean-exit":
            raise LiveError("native process did not exit cleanly")
        starts = [e for e in events if e["event_type"] == "process.started"]
        stops = [e for e in events if e["event_type"] == "process.stopped"]
        if (len(starts) != 1 or len(stops) != 1 or events[0] != starts[0] or events[-1] != stops[0] or
                stops[0]["payload"] != {"exit_status": None}):
            raise LiveError("native source lifecycle lacks exact clean started/stopped bounds")


def adaptive_receipt(root, plan, anchor):
    artifacts = plan["artifacts"]
    manager = next(row["argv"] for row in plan["processes"] if row["source_id"] == "adaptive-manager")
    bundle = old._safe_child(root, old._option(manager, "--bundle-output"), "signed bundle")
    fault = operator.raw_replay.replay_fault_evidence(run_id=plan["run_id"],
        manager_raw=(root / "raw/adaptive-manager.jsonl").read_bytes(),
        replica_raw=[(root / f"raw/replica-{i}.jsonl").read_bytes() for i in range(7)],
        replica_logs=[(root / f"logs/replica-{i}.log").read_bytes() for i in range(7)])
    if fault["anchor_decision_monotonic_ns"] != anchor:
        raise LiveError("sealed physical anchor differs from runtime anchor")
    scalar = {name: artifacts[name] for name in ("build_receipt", "launch_arguments", "preparation", "e0_identity", "v8_profile", "selection_profile", "epoch0_tree", "main_config", "replica_configs")}
    scalar["executables"] = {name: artifacts[name] for name in ("hotstuff_app", "adaptation_manager", "hotstuff_keygen", "hotstuff_tls_keygen", "e0_helper")}
    receipt = {"schema_version": 1, "kind": operator.ARM_RECEIPT_KIND, "run_id": plan["run_id"],
        "profile": {"id": plan["profile_id"], "sha256": plan["profile_sha256"]},
        "repository_revision": plan["repository_revision"], "arm": "adaptive_e1", "no_retry": True,
        "claim_eligible": False, "figure_eligible": False,
        "plan": inputs._desc(root, root / inputs.PLAN), "request": inputs._desc(root, root / inputs.REQUEST),
        "external_authorization": inputs._desc(root, root / child.APPROVAL), "launch_intent": inputs._desc(root, root / child.INTENT),
        "artifacts": {"bundle": inputs._desc(root, bundle)},
        "anchor": {"source_id": fault["anchor_source_id"], "source_sequence": fault["anchor_source_sequence"], "line_sha256": fault["anchor_line_sha256"], "monotonic_ns": anchor},
        "fault_window": {"start_monotonic_ns": plan["scheduled_window"]["start_ns"], "end_monotonic_ns": plan["scheduled_window"]["end_ns"], "coverage_through_horizon": True},
        "provenance": scalar,
        "raw": {s: inputs._desc(root, root / f"raw/{s}.jsonl") for s in SOURCES},
        "logs": {s: inputs._desc(root, root / f"logs/{s}.log") for s in SOURCES},
        "cleanup": inputs._desc(root, root / "runtime/cleanup-receipt.json")}
    streams = base._event_streams(root)
    identity = dict(next(e["payload"] for e in streams["adaptive-manager"] if e["event_type"] == "adaptive_v2.convergence_started"))
    identity.pop("cycle_ordinal")
    replay = operator.validator_v8.validate_v8_raw_contract(anchor_monotonic_ns=anchor,
        expected_identity=identity, manager_events=streams["adaptive-manager"],
        replica_events={i: streams[f"replica-{i}"] for i in range(7)}, bundle_bytes=bundle.read_bytes(),
        issuer_public_key=checked(root, artifacts["issuer_public_key"]).read_text().strip(), predecessor_tree_ids=frozenset(range(7)))
    path = root / "runtime/sustained-role-v8-signed-validator.json"
    write(path, replay)
    receipt["validator"] = {**inputs._desc(root, path), "profile_id": plan["profile_id"]}
    transition, snapshot = old._transition_artifacts(root, manager, bundle)
    arm_path = old._safe_child(root, old._option(manager, "--fault-window-arm-path"), "native evidence arm")
    receipt["causal_artifacts"] = {"transition_request": inputs._desc(root, transition),
        "manager_evidence_snapshot": inputs._desc(root, snapshot),
        "manager_fault_window_arm": inputs._desc(root, arm_path)}
    validate_causality(root, receipt, plan, streams)
    return receipt


def validate_causality(root, receipt, plan, streams):
    """Reuse the unchanged exact timeout-quorum guard, not the v7 time metric."""
    descriptors = receipt["causal_artifacts"]
    if set(descriptors) != {"transition_request", "manager_evidence_snapshot", "manager_fault_window_arm"}:
        raise LiveError("selection causal artifacts are incomplete")
    for descriptor in descriptors.values():
        checked(root, descriptor)
    artifacts = {name: {key: descriptor[key] for key in ("path", "sha256")}
                 for name, descriptor in descriptors.items()}
    artifacts["epoch0_tree"] = {key: plan["artifacts"]["epoch0_tree"][key] for key in ("path", "sha256")}
    e0, _ = read(checked(root, plan["artifacts"]["e0_identity"]))
    approval_raw = (root / child.APPROVAL).read_bytes()
    binding = {"e0_digest": e0["epoch_digest"], "approval_sha256": sha(approval_raw),
               "selection_profile_sha256": plan["artifacts"]["selection_profile"]["sha256"]}
    bundle = operator.validator_v8._decode_bundle(checked(root, receipt["artifacts"]["bundle"]).read_bytes(),
        checked(root, plan["artifacts"]["issuer_public_key"]).read_text().strip())
    causality._validate_adaptive_causality(root, artifacts,
        receipt={"run_id": plan["run_id"], "anchor": receipt["anchor"], "launch_binding": binding},
        manager_events=streams["adaptive-manager"], actor_events=streams["replica-1"], bundle=bundle)


def fixed_observation(root, plan):
    e0, _ = read(checked(root, plan["artifacts"]["e0_identity"]))
    return fixed_replay.replay_fixed_e0_raw(run_id=plan["run_id"], e0_digest=e0["epoch_digest"],
        profile_sha256=plan["profile_sha256"], scheduled_start_monotonic_ns=plan["scheduled_window"]["start_ns"],
        scheduled_end_monotonic_ns=plan["scheduled_window"]["end_ns"],
        manager_raw=(root / "raw/adaptive-manager.jsonl").read_bytes(),
        replica_raw=[(root / f"raw/replica-{i}.jsonl").read_bytes() for i in range(7)],
        replica_logs=[(root / f"logs/replica-{i}.log").read_bytes() for i in range(7)],
        cleanup_raw=(root / "runtime/cleanup-receipt.json").read_bytes())


def make_raw_receipt(root, arm, plan, anchor):
    if arm == "adaptive_e1":
        receipt = adaptive_receipt(root, plan, anchor)
        operator._component_replay(root, {k: v for k, v in receipt.items() if k != "causal_artifacts"})
    else:
        observation = fixed_observation(root, plan)
        receipt = {"arm": arm, "run_id": plan["run_id"], "repository_revision": plan["repository_revision"],
                   "observation": observation, "raw": {s: inputs._desc(root, root / f"raw/{s}.jsonl") for s in SOURCES},
                   "logs": {s: inputs._desc(root, root / f"logs/{s}.log") for s in SOURCES}}
    receipt["live_descriptors"] = {"plan": inputs._desc(root, root / paths(arm)[0]),
        "request": inputs._desc(root, root / paths(arm)[1]), "approval": inputs._desc(root, root / paths(arm)[2]),
        "intent": inputs._desc(root, root / paths(arm)[3]), "live_authority": inputs._desc(root, root / LIVE),
        "child_started": inputs._desc(root, root / STARTED), "prearm": inputs._desc(root, root / "runtime/sustained-role-v8-live-prearm.json"),
        "cleanup": inputs._desc(root, root / "runtime/cleanup-receipt.json")}
    write(root / RAW_RECEIPT, receipt)


def execute(root, arm, approval, approval_sha):
    if any((root / name).exists() for name in (ABORT, STARTED, RAW_RECEIPT, POST_SCOPE, TERMINAL)):
        raise LiveError("run root already attempted; no retry")
    write(root / "runtime/sustained-role-v8-live-attempt.json",
          {"arm": arm, "monotonic_ns": clock(), "no_retry": True})
    try:
        return _execute(root, arm, approval, approval_sha)
    except BaseException as exc:
        if not (root / ABORT).exists():
            write(root / ABORT, {"state": "ABORTED_NO_RETRY", "error_type": type(exc).__name__,
                                "reason": str(exc)[:512], "no_retry": True})
        raise


def _execute(root, arm, approval, approval_sha):
    plan, _, approval_raw = validate_approval(root, arm, approval, approval_sha)
    if any((root / name).exists() for name in (ABORT, STARTED, RAW_RECEIPT, POST_SCOPE, TERMINAL)):
        raise LiveError("run root already attempted; no retry")
    live = collect_authority(root, plan)
    if not 30_000_000_000 <= plan["scheduled_window"]["start_ns"] - clock() <= 120_000_000_000:
        raise LiveError("prearm reserve is outside frozen scope budget")
    if plan["scheduled_window"]["end_ns"] - clock() > 185_000_000_000:
        raise LiveError("fault window leaves insufficient 210-second scope cleanup reserve")
    unseal_manager(root, plan)
    if arm == "adaptive_e1":
        child.prepare_pre_spawn_scope(root, approval_path=approval, observed_revision=plan["repository_revision"],
            target_host="proteina02", linux_boot_id=live["linux_boot_id"], expected_approval_sha256=approval_sha)
    else:
        write(root / FIXED_APPROVAL, json.loads(approval_raw))
        write(root / FIXED_INTENT, {"run_id": plan["run_id"], "plan_sha256": plan["plan_sha256"],
            "approval_sha256": approval_sha, "build_binding": live["build_binding"], "no_retry": True})
    write(root / LIVE, live)
    unit = "kauri-w19-v8-" + plan["run_id"]
    command = ["systemd-run", "--user", "--scope", "--unit", unit, "-p", "RuntimeMaxSec=210s",
        "-p", "KillMode=control-group", "-p", "SendSIGKILL=yes", sys.executable, str(Path(__file__).resolve()),
        "child", "--root", str(root), "--arm", arm, "--live-sha256", sha(canonical(live)),
        "--intent-sha256", sha((root / paths(arm)[3]).read_bytes()), "--approval-sha256", approval_sha]
    started = clock()
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=225)
        proof = scope._scope_empty(unit, subprocess.run)
        write(root / POST_SCOPE, {"run_id": plan["run_id"], "unit": unit, "command": command,
            "scope_returncode": result.returncode, "scope_started_ns": started, "scope_returned_ns": clock(),
            "scope_stdout": result.stdout.decode(errors="replace"), "scope_stderr": result.stderr.decode(errors="replace"),
            "quiescence": proof})
        if result.returncode != 0:
            raise LiveError("bounded child failed; inspect preserved scope stderr and native raw")
        result = replay(root, expected_receipt_sha=sha((root / RAW_RECEIPT).read_bytes()), terminal_required=False)
        write(root / TERMINAL, {"schema_version": 1, "run_id": plan["run_id"], "state": "SEALED_VALIDATED_NO_CLAIM",
            "raw_receipt_sha256": sha((root / RAW_RECEIPT).read_bytes()), "post_scope_sha256": sha((root / POST_SCOPE).read_bytes()),
            "no_retry": True, "result": result})
        return result
    except BaseException as exc:
        try:
            subprocess.run(["systemctl", "--user", "stop", unit + ".scope"], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
        if not (root / ABORT).exists():
            write(root / ABORT, {"state": "ABORTED_NO_RETRY", "run_id": plan["run_id"],
                "error_type": type(exc).__name__, "reason": str(exc)[:512], "no_retry": True})
        raise


def replay(root, *, expected_receipt_sha, expected_terminal_sha=None, terminal_required=True):
    if (root / ABORT).exists():
        raise LiveError("aborted root cannot be accepted")
    receipt, receipt_raw = read(root / RAW_RECEIPT)
    if sha(receipt_raw) != expected_receipt_sha:
        raise LiveError("raw receipt differs from external pin")
    arm = receipt["arm"]
    if arm not in {"adaptive_e1", "fixed_e0"}:
        raise LiveError("unknown arm")
    for descriptor in receipt["live_descriptors"].values():
        checked(root, descriptor)
    plan, _ = read(root / paths(arm)[0]); request, request_raw = read(root / paths(arm)[1])
    inputs.verify(root, expected_request_sha256=sha(request_raw), arm=arm)
    approval, approval_raw = read(root / paths(arm)[2]); intent, _ = read(root / paths(arm)[3])
    if (approval["plan_sha256"] != plan["plan_sha256"] or approval["request_sha256"] != sha(request_raw) or
            intent["approval_sha256"] != sha(approval_raw) or intent["run_id"] != plan["run_id"]):
        raise LiveError("offline approval/intent identity drift")
    live, _ = read(root / LIVE)
    observed = datetime.fromisoformat(live["observed_utc"])
    booking_row(live["booking_stdout"], now=observed)
    build = child.build_binding.bind(root=root, plan=plan, observed_revision=plan["repository_revision"],
                                    target_host="proteina02", linux_boot_id=live["linux_boot_id"])
    build_receipt, _ = read(checked(root, plan["artifacts"]["build_receipt"]))
    if sha(checked(root, live["build_log"]).read_bytes()) != build_receipt["build_log_sha256"]:
        raise LiveError("archived actual build log differs from pinned build receipt")
    cache = checked(root, live["cmake_cache"]).read_bytes()
    if (b"CMAKE_BUILD_TYPE:STRING=" + build_receipt["build_type"].encode() + b"\n" not in cache or
            b"HOTSTUFF_TWO_STEP:BOOL=OFF\n" not in cache):
        raise LiveError("archived effective build settings differ from pinned receipt")
    if (live["host"] != "proteina02" or live["user"] != "gascarvalho" or live["build_binding"] != build or
            live["repository_revision"] != plan["repository_revision"]):
        raise LiveError("offline live identity/build drift")
    post, post_raw = read(root / POST_SCOPE)
    if (post["scope_returncode"] != 0 or post["run_id"] != plan["run_id"] or
            post["quiescence"].get("cgroup_population") not in {"collected", "zero"}):
        raise LiveError("successful bounded scope/empty cgroup is absent")
    streams = {}
    instances = {row["source_id"]: row["source_instance"] for row in plan["processes"]}
    for source in SOURCES:
        stream = checked(root, receipt["raw"][source]).read_bytes()
        checked(root, receipt["logs"][source])
        streams[source] = operator.raw_replay._stream(stream, run_id=plan["run_id"], source_id=source)
        if streams[source][0]["source_instance"] != instances[source]:
            raise LiveError("native source instance differs from frozen launch plan")
    cleanup, _ = read(root / "runtime/cleanup-receipt.json")
    validate_lifecycle(streams, cleanup)
    started, _ = read(root / STARTED); prearm, _ = read(root / "runtime/sustained-role-v8-live-prearm.json")
    if (not any(line.startswith("0::/") and line.endswith("/" + post["unit"] + ".scope") for line in started["cgroup"].splitlines()) or
            not post["scope_started_ns"] <= started["monotonic_ns"] < prearm["prearm_ns"] < plan["scheduled_window"]["start_ns"] or
            not 0 <= started["monotonic_ns"] - live["observed_monotonic_ns"] <= 30_000_000_000 or
            post["scope_returned_ns"] - post["scope_started_ns"] > 225_000_000_000):
        raise LiveError("scope/prearm/native schedule chronology drift")
    if not old.fixed._all_seven_e0_common(streams, plan["scheduled_window"]["start_ns"]):
        raise LiveError("raw does not prove all-seven E0 prearm")
    if arm == "adaptive_e1":
        component = {key: value for key, value in receipt.items() if key not in {"live_descriptors", "causal_artifacts"}}
        observation = operator._component_replay(root, component)
        validate_causality(root, receipt, plan, streams)
        count = observation["common_e1_committed_blocks"]
    else:
        observation = fixed_observation(root, plan)
        if receipt["observation"] != observation:
            raise LiveError("fixed raw replay differs from sealed observation")
        count = observation["common_authoritative_e0_commit_count"]
    result = {"verdict": "RAW_BUNDLE_VALIDATED_NO_CLAIM", "arm": arm, "run_id": plan["run_id"],
        "repository_revision": plan["repository_revision"], "common_committed_blocks": count,
        "metric_window": "[A+32s,A+72s)", "metric_duration_seconds": 40,
        "metric_kind": "all_seven_common_committed_block_count", "external_client_throughput": False,
        "claim_eligible": False, "figure_eligible": False, "observation": observation}
    if terminal_required:
        terminal, terminal_raw = read(root / TERMINAL)
        if (sha(terminal_raw) != expected_terminal_sha or terminal["state"] != "SEALED_VALIDATED_NO_CLAIM" or terminal["result"] != result or
                terminal["raw_receipt_sha256"] != expected_receipt_sha or terminal["post_scope_sha256"] != sha(post_raw)):
            raise LiveError("positive final terminal differs from immediate replay")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("child", "execute", "replay"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--arm", choices=("adaptive_e1", "fixed_e0"))
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--terminal-sha256")
    parser.add_argument("--live-sha256")
    parser.add_argument("--intent-sha256")
    parser.add_argument("--approval-sha256")
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    if args.action == "child":
        run_child(root, args.arm, args.live_sha256, args.intent_sha256, args.approval_sha256); return
    if args.action == "execute":
        result = execute(root, args.arm, args.approval, args.expected_sha256)
    else:
        result = replay(root, expected_receipt_sha=args.expected_sha256, expected_terminal_sha=args.terminal_sha256)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
