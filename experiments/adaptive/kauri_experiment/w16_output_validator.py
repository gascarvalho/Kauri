"""Read-only, producer-bound validation for one W16 executor output root.

This validator deliberately does not compare arms and never upgrades a single
run into a throughput-improvement claim.  It reparses the producer's native
artifacts and derives any reported throughput solely from authoritative commit
events inside a complete, post-terminal Epoch-0 measurement window.
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Mapping, Sequence

from . import n31_static_e0_feasibility as feasibility
from . import static_e0_cpu_contract


_RECEIPT_SCHEMA = "kauri-n31-static-e0-local-executor-v1"
_TERMINAL = "adaptive_v2_reporting_terminal"
_ENVELOPE_KEYS = frozenset(
    {
        "event_schema_version", "run_id", "source_kind", "source_id",
        "source_instance", "source_sequence", "source_monotonic_ns",
        "event_type", "payload",
    }
)
_ACTIVE_KEYS = frozenset(
    {
        "epoch_number", "tree_id", "epoch_digest", "block_hash",
        "context_generation", "observer_replica", "wait_exempt_signers",
        "accepted_signers", "absent_direct_children",
        "missing_optional_signers", "required_branch_gaps",
        "root_signer_count", "global_quorum", "rejection_reason",
    }
)
_COMMIT_KEYS = frozenset(
    {
        "block_height", "block_hash", "parent_hash", "transaction_count",
        "designated_observer", "decision_proof", "view_generation",
        "commit_batch_index",
    }
)
_PROOF_KEYS = frozenset({"epoch_number", "tree_id", "epoch_digest", "block_hash"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_EPOCH_ZERO_DIGESTS = {
    "slow-roots": "827e7626c74f8d815bca6ae5cbe10e312bc4f00f287e67d41277b8d689b21c0f",
    "fast-roots": "e640d31a0f4c394ca1ed50005c9f387fdc25de0b67c9ae8eae158275189e462e",
}
_FATAL_EVENTS = frozenset(
    {
        "process.forced_crash_requested", "adaptive_v2_convergence_failure",
        "adaptive_v2_session_terminal", "adaptive_v3_terminal",
        "adaptive_v3_command_terminal", "epoch.command_committed",
    }
)
_ALLOWED_EVENT_TYPES = frozenset(
    {
        "process.started", "process.ready", "process.stopping", "process.stopped",
        "adaptive.configuration_active", _TERMINAL,
        "block.commit_observed", "block.committed",
        "aggregation.required_set_ready",
        "aggregation.initial_reserved", "aggregation.initial_enqueued",
        "aggregation.initial_committed",
        "aggregation.root_quorum_progress", "aggregation.root_qc_published",
    }
)


class _InvalidEvidence(RuntimeError):
    def __init__(self, code: str, detail: str, *, observed: bool = False) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.observed = observed


def _fail(code: str, detail: str, *, observed: bool = False) -> None:
    raise _InvalidEvidence(code, detail, observed=observed)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, ensure_ascii=True,
                   sort_keys=True, separators=(",", ":")).encode("ascii")
        + b"\n"
    )


def _integer(value: object, *, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _read_json(path: Path, label: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        _fail("missing_artifact", f"{label} is not a regular file")
    try:
        document = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail("malformed_json", f"{label} is not valid JSON: {exc}")
    if not isinstance(document, dict):
        _fail("malformed_json", f"{label} must contain one object")
    return document


def _read_jsonl(path: Path, label: str) -> list[dict[str, object]]:
    if path.is_symlink() or not path.is_file():
        _fail("missing_artifact", f"{label} is not a regular file")
    rows: list[dict[str, object]] = []
    try:
        with path.open("r", encoding="utf-8") as source:
            for line_number, line in enumerate(source, 1):
                if not line.endswith("\n"):
                    _fail("truncated_jsonl", f"{label}:{line_number} lacks a newline")
                value = json.loads(line)
                if not isinstance(value, dict):
                    _fail("malformed_jsonl", f"{label}:{line_number} is not an object")
                rows.append(value)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail("malformed_jsonl", f"{label} is invalid JSONL: {exc}")
    if not rows:
        _fail("empty_jsonl", f"{label} is empty")
    return rows


def _safe_relative(value: object) -> str:
    if not isinstance(value, str) or not value:
        _fail("artifact_inventory", "artifact inventory contains a non-path key")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or ".." in candidate.parts or "." in candidate.parts:
        _fail("artifact_inventory", f"unsafe artifact path: {value!r}")
    return value


def _validate_inventory(root: Path, receipt: Mapping[str, object]) -> None:
    declared = receipt.get("artifact_sha256")
    if not isinstance(declared, dict):
        _fail(
            "receipt_missing_artifact_inventory",
            "receipt predates the producer-bound artifact_sha256 inventory",
        )
    expected: dict[str, str] = {}
    for raw_path, raw_digest in declared.items():
        relative = _safe_relative(raw_path)
        if not _digest(raw_digest):
            _fail("artifact_inventory", f"malformed digest for {relative}")
        expected[relative] = raw_digest

    actual: dict[str, str] = {}
    try:
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                _fail("artifact_symlink", f"artifact tree contains symlink {path}")
            if path.is_file():
                relative = path.relative_to(root).as_posix()
                if relative not in {"feasibility-receipt.json", "feasibility-abort.json"}:
                    actual[relative] = _sha256_file(path)
    except OSError as exc:
        _fail("artifact_inventory", f"cannot inventory artifact tree: {exc}")
    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        _fail(
            "artifact_inventory_mismatch",
            f"artifact set differs from receipt (missing={missing}, extra={extra})",
            observed=True,
        )
    changed = sorted(path for path in actual if actual[path] != expected[path])
    if changed:
        _fail(
            "artifact_hash_mismatch",
            f"artifact bytes differ from receipt: {changed}",
            observed=True,
        )


def _validate_preflight(receipt: Mapping[str, object]) -> tuple[str, object]:
    preflight = receipt.get("preflight")
    if not isinstance(preflight, dict):
        _fail("preflight", "receipt does not embed the exact preflight")
    exact_keys = {
        "schema_version", "kind", "verdict", "revision", "profile_id",
        "profile_sha256", "arm", "replica_count", "quorum", "tree_count",
        "treegen_sha256", "manager_endpoint", "binaries", "binary_sha256",
        "limitations",
    }
    if set(preflight) != exact_keys:
        _fail("preflight", "preflight schema drifted")
    arm = preflight.get("arm")
    if arm not in {"slow-roots", "fast-roots"}:
        _fail("preflight", "preflight arm is invalid")
    plan = feasibility.frozen_plan(arm=str(arm))
    binaries = preflight.get("binaries")
    binary_sha256 = preflight.get("binary_sha256")
    binary_names = {"app", "keygen", "tls_keygen", "native_digest"}
    if (
        preflight.get("schema_version") != 1
        or preflight.get("kind") != feasibility.SCHEMA
        or preflight.get("verdict") != "PREFLIGHT_OK_NO_EXECUTION"
        or not isinstance(preflight.get("revision"), str)
        or _REVISION.fullmatch(str(preflight["revision"])) is None
        or preflight.get("profile_id") != plan.profile.profile_id
        or preflight.get("profile_sha256") != plan.profile.sha256
        or preflight.get("replica_count") != 31
        or preflight.get("quorum") != 21
        or preflight.get("tree_count") != 21
        or preflight.get("treegen_sha256") != plan.treegen_sha256
        or preflight.get("manager_endpoint") != plan.manager_endpoint
        or not isinstance(binaries, dict) or set(binaries) != binary_names
        or any(not isinstance(binaries[name], str) or not binaries[name]
               for name in binary_names)
        or not isinstance(binary_sha256, dict) or set(binary_sha256) != binary_names
        or any(not _digest(binary_sha256[name]) for name in binary_names)
        or not isinstance(preflight.get("limitations"), list)
    ):
        _fail("preflight", "preflight identity differs from the frozen W16 plan")
    return str(arm), plan


def _config_value(lines: Sequence[str], key: str) -> str:
    prefix = key + " = "
    values = [line[len(prefix):] for line in lines if line.startswith(prefix)]
    if len(values) != 1:
        _fail("configuration", f"main.conf must contain exactly one {key}")
    return values[0]


def _validate_configuration(root: Path, receipt: Mapping[str, object], plan: object) -> None:
    treegen = root / "config/epoch0-treegen.conf"
    if treegen.is_symlink() or not treegen.is_file() or treegen.read_bytes() != plan.treegen_bytes:
        _fail("configuration", "Epoch-0 tree file differs from the frozen arm")
    if receipt.get("treegen_sha256") != plan.treegen_sha256:
        _fail("configuration", "receipt tree hash differs from frozen arm")
    main = root / "config/main.conf"
    if main.is_symlink() or not main.is_file():
        _fail("configuration", "main.conf is missing")
    try:
        lines = main.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        _fail("configuration", f"main.conf is unreadable: {exc}")
    fixed = {
        "block-size": "1000", "proposer": "0", "fan-out": "5",
        "async_blocks": "2", "tree-generation": "file",
        "tree-switch-period": "1", "epoch-protocol-mode": "adaptive_v2",
        "epoch-manager-address": "127.0.0.1:27991",
    }
    for key, expected in fixed.items():
        if _config_value(lines, key) != expected:
            _fail("configuration", f"main.conf {key} differs from W16")
    tree_path = Path(_config_value(lines, "tree-generation-fpath"))
    if tree_path != root / "config/epoch0-treegen.conf":
        _fail("configuration", "main.conf tree path is not the generated W16 tree file")
    replica_lines = [line for line in lines if line.startswith("replica = ")]
    if len(replica_lines) != 31:
        _fail("configuration", "main.conf does not declare exactly 31 replicas")

    run_id = receipt.get("run_id")
    for replica in range(31):
        path = root / "config" / f"replica-{replica}.conf"
        if path.is_symlink() or not path.is_file():
            _fail("configuration", f"replica-{replica}.conf is missing")
        rows = path.read_text(encoding="utf-8").splitlines()
        values: dict[str, list[str]] = defaultdict(list)
        for row in rows:
            if " = " in row:
                key, value = row.split(" = ", 1)
                values[key].append(value)
        expected = {
            "idx": str(replica),
            "structured-event-run-id": run_id,
            "structured-event-source-instance": f"{run_id}-replica-{replica}",
            "structured-event-commit-observer-id": "replica-2",
            "structured-event-commit-observer-instance": f"{run_id}-replica-2",
        }
        for key, value in expected.items():
            if values.get(key) != [value]:
                _fail("configuration", f"replica {replica} has a drifted {key}")
        outputs = values.get("structured-event-output", [])
        if len(outputs) != 1 or Path(outputs[0]) != root / "raw" / f"replica-{replica}.jsonl":
            _fail("configuration", f"replica {replica} has a drifted event output")


def _validate_active(payload: object, *, replica: int, digest: str) -> int:
    if not isinstance(payload, dict) or set(payload) != _ACTIVE_KEYS:
        _fail("native_event_schema", "configuration-active payload schema drifted")
    tree = payload.get("tree_id")
    list_keys = (
        "wait_exempt_signers", "accepted_signers", "absent_direct_children",
        "missing_optional_signers", "required_branch_gaps",
    )
    if (
        payload.get("epoch_number") != 0
        or payload.get("epoch_digest") != digest
        or payload.get("block_hash") is not None
        or payload.get("context_generation") is not None
        or payload.get("observer_replica") != replica
        or payload.get("root_signer_count") != 0
        or payload.get("global_quorum") != 21
        or payload.get("rejection_reason") is not None
        or not _integer(tree) or tree >= 21
        or any(payload.get(key) != [] for key in list_keys)
    ):
        _fail("epoch_zero_identity", f"replica {replica} active configuration drifted", observed=True)
    return int(tree)


def _validate_commit(payload: object, *, digest: str, observer: bool) -> dict[str, object]:
    if not isinstance(payload, dict):
        _fail("native_event_schema", "commit payload is not an object")
    allowed = set(_COMMIT_KEYS) | {"reporter_local_commit_monotonic_ns"}
    payload_keys = set(payload)
    if payload_keys != set(_COMMIT_KEYS) and payload_keys != allowed:
        _fail("native_event_schema", "commit payload schema drifted")
    proof = payload.get("decision_proof")
    if not isinstance(proof, dict) or set(proof) != _PROOF_KEYS:
        _fail("native_event_schema", "decision proof schema drifted")
    if (
        payload.get("designated_observer") is not observer
        or not _integer(payload.get("block_height"), minimum=1)
        or not _digest(payload.get("block_hash"))
        or not _digest(payload.get("parent_hash"))
        or not _integer(payload.get("transaction_count"))
        or int(payload.get("transaction_count", 0)) > 1000
        or not _integer(payload.get("view_generation"))
        or not _integer(payload.get("commit_batch_index"))
        or proof.get("epoch_number") != 0
        or proof.get("epoch_digest") != digest
        or not _integer(proof.get("tree_id")) or int(proof["tree_id"]) >= 21
        or proof.get("block_hash") != payload.get("block_hash")
    ):
        _fail("authoritative_commit", "commit evidence differs from native Epoch-0 identity", observed=True)
    local = payload.get("reporter_local_commit_monotonic_ns")
    if "reporter_local_commit_monotonic_ns" in payload and not _integer(local, minimum=1):
        _fail("native_event_schema", "reporter-local commit timestamp is invalid")
    return payload


def _validate_streams(
    root: Path, receipt: Mapping[str, object], *, cpu_run: bool
) -> tuple[list[dict[str, object]], int, int, int]:
    run_id = receipt.get("run_id")
    digest = receipt.get("epoch_zero_digest")
    raw_sha256 = receipt.get("raw_sha256")
    if not isinstance(run_id, str) or not run_id or not _digest(digest):
        _fail("receipt_identity", "receipt lacks run ID or native Epoch-0 digest")
    if not isinstance(raw_sha256, dict) or set(raw_sha256) != {
        f"replica-{replica}" for replica in range(31)
    }:
        _fail("raw_inventory", "raw_sha256 does not cover exactly 31 replicas")

    observer_events: list[dict[str, object]] | None = None
    observer_terminal_index = -1
    terminal_times: list[tuple[str, int]] = []
    cycle_completion_times: list[tuple[str, int]] = []
    for replica in range(31):
        source = f"replica-{replica}"
        path = root / "raw" / f"{source}.jsonl"
        if raw_sha256[source] != _sha256_file(path):
            _fail("raw_hash_mismatch", f"{source} differs from raw_sha256", observed=True)
        events = _read_jsonl(path, source)
        active: list[tuple[int, int]] = []
        terminals: list[int] = []
        lifecycle: dict[str, list[int]] = defaultdict(list)
        previous_time = 0
        for index, event in enumerate(events):
            if set(event) != _ENVELOPE_KEYS:
                _fail("native_event_schema", f"{source} envelope schema drifted")
            if (
                event.get("event_schema_version") != 1
                or event.get("run_id") != run_id
                or event.get("source_kind") != "replica"
                or event.get("source_id") != source
                or event.get("source_instance") != f"{run_id}-{source}"
                or event.get("source_sequence") != index + 1
                or not _integer(event.get("source_monotonic_ns"), minimum=1)
                or int(event["source_monotonic_ns"]) < previous_time
                or not isinstance(event.get("event_type"), str)
                or not isinstance(event.get("payload"), dict)
            ):
                _fail("native_envelope", f"{source} has a gapped or unbound envelope", observed=True)
            previous_time = int(event["source_monotonic_ns"])
            event_type = str(event["event_type"])
            if event_type in _FATAL_EVENTS:
                _fail("fatal_native_event", f"{source} emitted {event_type}", observed=True)
            if event_type not in _ALLOWED_EVENT_TYPES:
                _fail(
                    "unexpected_native_event",
                    f"{source} emitted non-W16 event {event_type}",
                    observed=True,
                )
            if event_type == "process.ready":
                if event["payload"] != {"exit_status": None}:
                    _fail("native_event_schema", f"{source} ready payload drifted")
                lifecycle[event_type].append(index)
            elif event_type in {"process.started", "process.stopping", "process.stopped"}:
                if event["payload"] != {"exit_status": None}:
                    _fail("native_event_schema", f"{source} lifecycle payload drifted")
                lifecycle[event_type].append(index)
            elif event_type == _TERMINAL:
                payload = event["payload"]
                if (
                    set(payload) != {"reason", "terminal_monotonic_ns"}
                    or payload.get("reason") != "shared_outbox_delivery_failed"
                    or not _integer(payload.get("terminal_monotonic_ns"), minimum=1)
                ):
                    _fail("terminal", f"{source} terminal differs from reviewed outcome", observed=True)
                terminals.append(index)
            elif event_type == "adaptive.configuration_active":
                active.append(
                    (index, _validate_active(event["payload"], replica=replica, digest=str(digest)))
                )
            elif event_type == "block.committed":
                _validate_commit(event["payload"], digest=str(digest), observer=(replica == 2))
        if (
            len(terminals) != 1
            or any(len(lifecycle[kind]) != 1 for kind in (
                "process.started", "process.ready", "process.stopping", "process.stopped"
            ))
            or not (
                lifecycle["process.started"][0] < lifecycle["process.ready"][0]
                < terminals[0] < lifecycle["process.stopping"][0]
                < lifecycle["process.stopped"][0]
            )
        ):
            _fail("lifecycle", f"{source} lifecycle is incomplete or reordered")
        initial_cycle_completion: int | None = None
        for offset in range(max(0, len(active) - 20)):
            candidate = active[offset:offset + 21]
            if (
                [tree for _index, tree in candidate] == list(range(21))
                and lifecycle["process.started"][0] < candidate[0][0]
                and lifecycle["process.ready"][0] < candidate[-1][0]
                and candidate[-1][0] < lifecycle["process.stopping"][0]
            ):
                initial_cycle_completion = int(
                    events[candidate[-1][0]]["source_monotonic_ns"]
                )
                break
        if initial_cycle_completion is None:
            _fail(
                "epoch_zero_cycle",
                f"{source} lacks one complete native cycle between ready and stopping",
            )
        terminal_times.append((source, int(events[terminals[0]]["source_monotonic_ns"])))
        cycle_completion_times.append((source, initial_cycle_completion))
        if replica == 2:
            observer_events = events
            observer_terminal_index = terminals[0]

    assert observer_events is not None
    terminal_event = observer_events[observer_terminal_index]
    terminal_sequence = int(terminal_event["source_sequence"])
    terminal_time = int(terminal_event["source_monotonic_ns"])
    if not cpu_run:
        return observer_events, terminal_sequence, terminal_time, terminal_time

    cycles = receipt.get("required_complete_cycles")
    if not _integer(cycles, minimum=1) or int(cycles) != 5:
        _fail("measurement_window", "CPU run is not bound to exactly five cycles")
    post_active = [
        event for event in observer_events
        if event["event_type"] == "adaptive.configuration_active"
        and int(event["source_sequence"]) > terminal_sequence
    ]
    try:
        start_offset = next(
            offset for offset, event in enumerate(post_active)
            if event["payload"]["tree_id"] == 0
        )
    except StopIteration:
        _fail("measurement_window", "observer has no post-terminal tree-0 boundary")
    required = int(cycles) * 21 + 1
    window = post_active[start_offset:start_offset + required]
    expected = list(range(21)) * int(cycles) + [0]
    if len(window) != required or [event["payload"]["tree_id"] for event in window] != expected:
        _fail("measurement_window", "observer lacks the exact five-cycle tree-0 window")
    start_ns = int(window[0]["source_monotonic_ns"])
    end_ns = int(window[-1]["source_monotonic_ns"])
    if end_ns <= start_ns:
        _fail("measurement_window", "measurement window has non-positive duration", observed=True)
    late_terminal = [source for source, timestamp in terminal_times if timestamp >= start_ns]
    if late_terminal:
        _fail(
            "settlement_boundary",
            "reporting terminal does not precede the global measurement start for "
            + ", ".join(late_terminal),
        )
    late_cycle = [source for source, timestamp in cycle_completion_times if timestamp >= start_ns]
    if late_cycle:
        _fail(
            "settlement_boundary",
            "complete Epoch-0 cycle does not precede the global measurement start for "
            + ", ".join(late_cycle),
        )
    return observer_events, terminal_sequence, start_ns, end_ns


def _derive_throughput(
    events: Sequence[Mapping[str, object]], *, start_ns: int, end_ns: int,
    digest: str,
) -> dict[str, object]:
    commits: list[Mapping[str, object]] = []
    activated_trees: set[int] = set()
    for event in events:
        timestamp = int(event["source_monotonic_ns"])
        if event["event_type"] == "adaptive.configuration_active":
            activated_trees.add(int(event["payload"]["tree_id"]))
        elif event["event_type"] == "block.committed" and start_ns <= timestamp < end_ns:
            proof = event["payload"]["decision_proof"]
            if proof["tree_id"] not in activated_trees:
                _fail(
                    "commit_tree_binding",
                    "authoritative commit proof names a tree not yet activated",
                    observed=True,
                )
            commits.append(event)
    if len(commits) < 2:
        _fail("commit_chain_incomplete", "measurement window has fewer than two commits")
    payloads = [_validate_commit(event["payload"], digest=digest, observer=True)
                for event in commits]
    hashes: set[str] = set()
    for index, payload in enumerate(payloads):
        block_hash = str(payload["block_hash"])
        if block_hash in hashes:
            _fail("commit_chain", "authoritative commit chain repeats a block hash", observed=True)
        hashes.add(block_hash)
        if index:
            previous = payloads[index - 1]
            if (
                payload["block_height"] != int(previous["block_height"]) + 1
                or payload["parent_hash"] != previous["block_hash"]
            ):
                _fail("commit_chain", "authoritative commit chain is not continuous", observed=True)
    transactions = sum(int(payload["transaction_count"]) for payload in payloads)
    if transactions <= 0:
        _fail("zero_throughput", "complete measurement window committed no transactions", observed=True)
    duration = end_ns - start_ns
    return {
        "window_start_monotonic_ns": start_ns,
        "window_end_monotonic_ns": end_ns,
        "duration_ns": duration,
        "commit_count": len(payloads),
        "transaction_count": transactions,
        "first_block_height": payloads[0]["block_height"],
        "last_block_height": payloads[-1]["block_height"],
        "first_block_hash": payloads[0]["block_hash"],
        "last_block_hash": payloads[-1]["block_hash"],
        "throughput_milli_tps": transactions * 1_000_000_000_000 // duration,
    }


def _post_terminal_chain(
    events: Sequence[Mapping[str, object]], *, terminal_sequence: int, digest: str,
) -> dict[str, object]:
    commits = [
        event for event in events
        if event["event_type"] == "block.committed"
        and int(event["source_sequence"]) > terminal_sequence
    ]
    if len(commits) < 2:
        _fail("commit_chain_incomplete", "CPU-free run has fewer than two post-terminal commits")
    payloads = [
        _validate_commit(event["payload"], digest=digest, observer=True)
        for event in commits
    ]
    for previous, current in zip(payloads, payloads[1:]):
        if (
            current["block_height"] != int(previous["block_height"]) + 1
            or current["parent_hash"] != previous["block_hash"]
            or current["block_hash"] == previous["block_hash"]
        ):
            _fail("commit_chain", "CPU-free post-terminal commit chain is not continuous", observed=True)
    return {
        "commit_count": len(payloads),
        "first_block_height": payloads[0]["block_height"],
        "last_block_height": payloads[-1]["block_height"],
        "first_block_hash": payloads[0]["block_hash"],
        "last_block_hash": payloads[-1]["block_hash"],
    }


def _contract_document(contract: object) -> dict[str, object]:
    return {
        "schema_version": contract.schema_version,
        "contract_id": contract.contract_id,
        "enabled": contract.enabled,
        "figure_eligible": contract.figure_eligible,
        "launcher": contract.launcher,
        "manager_visibility": contract.manager_visibility,
        "sampling_interval_ms": contract.sampling_interval_ms,
        "base_profile_id": contract.base_profile_id,
        "base_profile_sha256": contract.base_profile_sha256,
        "base_profile_canonical_sha256": contract.base_profile_canonical_sha256,
        "assignments": [
            {
                "replica_id": assignment.replica_id,
                "capacity_class": assignment.capacity_class,
                "cpu_quota_percent": assignment.cpu_quota_percent,
            }
            for assignment in contract.assignments
        ],
    }


def _validate_quota(
    root: Path, receipt: Mapping[str, object], *, plan: object,
    start_ns: int, end_ns: int,
) -> dict[str, object]:
    contract_doc = _read_json(root / "runtime/cpu-quota-contract.json", "CPU contract")
    contract_id = contract_doc.get("contract_id")
    if contract_id == "w16-e0-heterogeneous-v1":
        mode = "heterogeneous"
    elif contract_id == "w16-e0-homogeneous-v1":
        mode = "homogeneous"
    else:
        _fail("cpu_contract", "CPU contract ID is not a frozen W16 mode")
    expected_contract = static_e0_cpu_contract.frozen_contract(plan, mode)
    if contract_doc != _contract_document(expected_contract):
        _fail("cpu_contract", "CPU contract differs from frozen assignments", observed=True)
    if receipt.get("quota_contract_sha256") != expected_contract.contract_sha256:
        _fail("cpu_contract", "receipt CPU contract hash differs from semantic contract")

    launch = _read_json(root / "runtime/cpu-quota-launch.json", "CPU launch receipt")
    if set(launch) != {
        "schema_version", "launcher", "contract_id", "contract_sha256",
        "manager_visibility", "replicas",
    }:
        _fail("cpu_launch", "CPU launch receipt schema drifted")
    rows = launch.get("replicas")
    if (
        launch.get("schema_version") != 1
        or launch.get("launcher") != expected_contract.launcher
        or launch.get("contract_id") != expected_contract.contract_id
        or launch.get("contract_sha256") != expected_contract.contract_sha256
        or launch.get("manager_visibility") != "none"
        or not isinstance(rows, list) or len(rows) != 31
    ):
        _fail("cpu_launch", "CPU launch does not bind all frozen scopes")
    launched: dict[int, dict[str, object]] = {}
    launch_keys = {
        "replica_id", "cpu_quota_percent", "unit", "control_group",
        "cpu_stat_path", "owned_pid", "owned_pgid", "cgroup_pids",
        "active_state", "sub_state", "cpu_quota_per_second_usec",
    }
    for row in rows:
        if not isinstance(row, dict) or set(row) != launch_keys:
            _fail("cpu_launch", "CPU launch row schema drifted")
        replica = row.get("replica_id")
        if not _integer(replica) or int(replica) >= 31 or int(replica) in launched:
            _fail("cpu_launch", "CPU launch membership is duplicated or invalid")
        assignment = expected_contract.assignment(int(replica))
        pids = row.get("cgroup_pids")
        if (
            row.get("cpu_quota_percent") != assignment.cpu_quota_percent
            or row.get("cpu_quota_per_second_usec") != assignment.cpu_quota_percent * 10_000
            or row.get("active_state") != "active"
            or row.get("sub_state") not in {"running", "start"}
            or not isinstance(row.get("unit"), str) or not row["unit"]
            or not isinstance(row.get("control_group"), str) or not str(row["control_group"]).startswith("/")
            or not isinstance(row.get("cpu_stat_path"), str)
            or not str(row["cpu_stat_path"]).endswith("/cpu.stat")
            or not _integer(row.get("owned_pid"), minimum=1)
            or row.get("owned_pgid") != row.get("owned_pid")
            or not isinstance(pids, list) or not pids
            or any(not _integer(pid, minimum=1) for pid in pids)
        ):
            _fail("cpu_launch", f"CPU launch row {replica} is not the reviewed scope", observed=True)
        launched[int(replica)] = row
    if set(launched) != set(range(31)):
        _fail("cpu_launch", "CPU launch does not cover exactly 31 replicas")

    samples = _read_jsonl(root / "raw/cpu-quota-samples.jsonl", "CPU samples")
    sample_keys = {
        "schema_version", "source_monotonic_ns", "replica_id",
        "cpu_quota_percent", "unit", "control_group", "cpu_stat_path",
        "cpu_quota_per_second_usec", "active_state", "sub_state", "cpu_stat",
    }
    required_stat_keys = {"usage_usec", "user_usec", "system_usec"}
    throttle_stat_keys = {"nr_periods", "nr_throttled", "throttled_usec"}
    observed_stat_keys: set[str] | None = None
    by_time: dict[int, dict[int, dict[str, int]]] = defaultdict(dict)
    for row in samples:
        if set(row) != sample_keys:
            _fail("cpu_samples", "active CPU sample schema drifted")
        replica = row.get("replica_id")
        timestamp = row.get("source_monotonic_ns")
        if not _integer(replica) or int(replica) not in launched or not _integer(timestamp, minimum=1):
            _fail("cpu_samples", "CPU sample identity is invalid")
        launch_row = launched[int(replica)]
        assignment = expected_contract.assignment(int(replica))
        stat = row.get("cpu_stat")
        if (
            row.get("schema_version") != 1
            or row.get("cpu_quota_percent") != assignment.cpu_quota_percent
            or row.get("cpu_quota_per_second_usec") != assignment.cpu_quota_percent * 10_000
            or row.get("unit") != launch_row["unit"]
            or row.get("control_group") != launch_row["control_group"]
            or row.get("cpu_stat_path") != launch_row["cpu_stat_path"]
            or row.get("active_state") != "active"
            or row.get("sub_state") not in {"running", "start"}
            or not isinstance(stat, dict)
            or set(stat) not in (
                required_stat_keys, required_stat_keys | throttle_stat_keys
            )
            or any(not _integer(value) for value in stat.values())
            or int(replica) in by_time[int(timestamp)]
        ):
            _fail("cpu_samples", "CPU sample differs from launched frozen scope", observed=True)
        current_stat_keys = set(stat)
        if observed_stat_keys is None:
            observed_stat_keys = current_stat_keys
        elif current_stat_keys != observed_stat_keys:
            _fail("cpu_samples", "CPU accounting key set changed between samples", observed=True)
        by_time[int(timestamp)][int(replica)] = {
            key: int(stat[key]) for key in current_stat_keys
        }
    timestamps = sorted(by_time)
    if any(set(by_time[timestamp]) != set(range(31)) for timestamp in timestamps):
        _fail("cpu_samples", "CPU sampling rounds do not cover exactly 31 replicas")
    if len(timestamps) < 2:
        _fail("cpu_samples", "CPU run has fewer than two full sampling rounds")
    interval_ns = expected_contract.sampling_interval_ms * 1_000_000
    before = [timestamp for timestamp in timestamps if timestamp <= start_ns]
    after = [timestamp for timestamp in timestamps if timestamp >= end_ns]
    in_window = [timestamp for timestamp in timestamps if start_ns <= timestamp <= end_ns]
    if (
        not before or not after or len(in_window) < 2
        or start_ns - before[-1] > 3 * interval_ns
        or after[0] - end_ns > 3 * interval_ns
    ):
        _fail(
            "cpu_sample_coverage",
            "CPU samples do not span the full window with two in-window rounds",
        )
    relevant = [timestamp for timestamp in timestamps if before[-1] <= timestamp <= after[0]]
    if any(right - left > 3 * interval_ns for left, right in zip(relevant, relevant[1:])):
        _fail("cpu_sample_coverage", "CPU sampling has a gap exceeding three intervals")
    for replica in range(31):
        previous: dict[str, int] | None = None
        for timestamp in relevant:
            current = by_time[timestamp][replica]
            if previous is not None and any(
                current[key] < previous[key] for key in observed_stat_keys or ()
            ):
                _fail("cpu_counters", f"replica {replica} CPU counter regressed", observed=True)
            previous = current
        if by_time[after[0]][replica]["usage_usec"] <= by_time[before[-1]][replica]["usage_usec"]:
            _fail(
                "cpu_accounting_not_observed",
                f"replica {replica} has no positive CPU usage across measurement",
                observed=True,
            )

    rounds = _read_jsonl(root / "raw/cpu-quota-monitor-rounds.jsonl", "CPU monitor rounds")
    round_keys = {
        "schema_version", "round_ordinal", "scheduled_monotonic_ns",
        "started_monotonic_ns", "sample_monotonic_ns", "finished_monotonic_ns",
        "duration_ns", "start_lateness_ns", "completion_overrun_ns",
    }
    if len(rounds) != len(timestamps):
        _fail("cpu_monitor", "monitor-round count differs from CPU sample groups")
    for ordinal, row in enumerate(rounds):
        if (
            set(row) != round_keys or row.get("schema_version") != 1
            or row.get("round_ordinal") != ordinal
            or row.get("sample_monotonic_ns") != timestamps[ordinal]
            or not all(_integer(row.get(key)) for key in round_keys - {"schema_version"})
            or int(row["finished_monotonic_ns"]) < int(row["started_monotonic_ns"])
            or row["duration_ns"] != int(row["finished_monotonic_ns"]) - int(row["started_monotonic_ns"])
        ):
            _fail("cpu_monitor", "CPU monitor cadence receipt drifted", observed=True)

    cleanup = _read_json(root / "runtime/cpu-quota-cleanup.json", "CPU cleanup")
    if receipt.get("quota_cleanup") != cleanup:
        _fail("cpu_cleanup", "receipt cleanup does not equal cleanup artifact")
    if set(cleanup) != {"schema_version", "complete", "monitor", "units"}:
        _fail("cpu_cleanup", "CPU cleanup schema drifted")
    monitor = cleanup.get("monitor")
    units = cleanup.get("units")
    if (
        cleanup.get("schema_version") != 1 or cleanup.get("complete") is not True
        or monitor != {"status": "PASSED", "stopped": True}
        or not isinstance(units, list) or len(units) != 31
    ):
        _fail("cpu_cleanup", "CPU cleanup did not prove complete quiescence", observed=True)
    cleanup_by_id: dict[int, dict[str, object]] = {}
    cleanup_keys = {
        "replica_id", "unit", "launch_verified", "load_state",
        "active_state", "sub_state", "control_group",
    }
    for row in units:
        if not isinstance(row, dict) or set(row) != cleanup_keys:
            _fail("cpu_cleanup", "CPU cleanup unit schema drifted")
        replica = row.get("replica_id")
        if not _integer(replica) or int(replica) not in launched or int(replica) in cleanup_by_id:
            _fail("cpu_cleanup", "CPU cleanup membership is invalid")
        if (
            row.get("unit") != launched[int(replica)]["unit"]
            or row.get("launch_verified") is not True
            or row.get("active_state") != "inactive"
            or row.get("sub_state") != "dead"
            or row.get("control_group") != ""
        ):
            _fail("cpu_cleanup", f"replica {replica} scope is not quiescent", observed=True)
        cleanup_by_id[int(replica)] = row
    if set(cleanup_by_id) != set(range(31)):
        _fail("cpu_cleanup", "CPU cleanup does not cover exactly 31 scopes")

    scope_cleanup = _read_json(
        root / "runtime/cpu-quota-scope-termination.json",
        "CPU owned-scope termination",
    )
    if receipt.get("quota_scope_cleanup") != scope_cleanup:
        _fail("cpu_scope_cleanup", "receipt scope cleanup differs from its artifact")
    scope_keys = {
        "replica_id", "unit", "ownership_verified", "invocation_id",
        "control_group", "worker_pid", "worker_pgid", "cgroup_dev",
        "cgroup_ino", "identity_revalidated", "kill_attempted",
        "populated_after_kill", "cgroup_removed_after_kill", "status", "error",
    }
    killed_scope_keys = scope_keys | {
        "worker_live_at_cleanup", "member_pids_before_kill",
    }
    scope_units = scope_cleanup.get("units")
    if (
        set(scope_cleanup) != {
            "schema_version", "complete", "deadline_ns", "deadline_exhausted", "units"
        }
        or scope_cleanup.get("schema_version") != 1
        or scope_cleanup.get("complete") is not True
        or scope_cleanup.get("deadline_exhausted") is not False
        or not _integer(scope_cleanup.get("deadline_ns"), minimum=1)
        or not isinstance(scope_units, list) or len(scope_units) != 31
    ):
        _fail("cpu_scope_cleanup", "owned-scope termination is incomplete", observed=True)
    scope_by_id: dict[int, dict[str, object]] = {}
    invocation_ids: set[str] = set()
    for row in scope_units:
        if not isinstance(row, dict):
            _fail("cpu_scope_cleanup", "owned-scope termination row schema drifted")
        expected_scope_keys = (
            killed_scope_keys if row.get("status") == "killed_and_empty" else scope_keys
        )
        if set(row) != expected_scope_keys:
            _fail("cpu_scope_cleanup", "owned-scope termination row schema drifted")
        replica = row.get("replica_id")
        invocation = row.get("invocation_id")
        if (
            not _integer(replica) or int(replica) not in launched or int(replica) in scope_by_id
            or row.get("unit") != launched[int(replica)]["unit"]
            or row.get("ownership_verified") is not True
            or not isinstance(invocation, str) or not invocation or invocation in invocation_ids
            or row.get("control_group") != launched[int(replica)]["control_group"]
            or row.get("worker_pid") != launched[int(replica)]["owned_pid"]
            or row.get("worker_pgid") != launched[int(replica)]["owned_pgid"]
            or not _integer(row.get("cgroup_dev"), minimum=1)
            or not _integer(row.get("cgroup_ino"), minimum=1)
            or not isinstance(row.get("identity_revalidated"), bool)
            or not isinstance(row.get("kill_attempted"), bool)
            or not isinstance(row.get("cgroup_removed_after_kill"), bool)
            or row.get("status") not in {"killed_and_empty", "already_inactive"}
            or row.get("error") is not None
        ):
            _fail("cpu_scope_cleanup", "owned-scope termination lost launch identity", observed=True)
        if row["status"] == "killed_and_empty" and (
            row.get("identity_revalidated") is not True
            or row.get("kill_attempted") is not True
            or row.get("populated_after_kill") != 0
            or not isinstance(row.get("worker_live_at_cleanup"), bool)
            or not isinstance(row.get("member_pids_before_kill"), list)
            or any(not _integer(pid, minimum=1)
                   for pid in row.get("member_pids_before_kill", []))
            or len(set(row.get("member_pids_before_kill", [])))
            != len(row.get("member_pids_before_kill", []))
            or (
                row.get("worker_live_at_cleanup") is True
                and row.get("worker_pid") not in row.get("member_pids_before_kill", [])
            )
        ):
            _fail("cpu_scope_cleanup", "killed scope lacks revalidation and empty proof", observed=True)
        if row["status"] == "already_inactive" and (
            row.get("identity_revalidated") is not False
            or row.get("kill_attempted") is not False
            or row.get("populated_after_kill") is not None
            or row.get("cgroup_removed_after_kill") is not False
        ):
            _fail(
                "cpu_scope_cleanup",
                "inactive scope carries inconsistent termination evidence",
                observed=True,
            )
        invocation_ids.add(str(invocation))
        scope_by_id[int(replica)] = row
    if set(scope_by_id) != set(range(31)):
        _fail("cpu_scope_cleanup", "scope termination does not cover exactly 31 owned scopes")

    first, last = relevant[0], relevant[-1]
    deltas: dict[str, dict[str, int]] = {}
    assert observed_stat_keys is not None
    for replica in range(31):
        deltas[str(replica)] = {
            key: by_time[last][replica][key] - by_time[first][replica][key]
            for key in sorted(observed_stat_keys)
        }
    slow_throttled_delta = sum(
        deltas[str(replica)].get("throttled_usec", 0) for replica in range(6)
    )
    if (
        mode == "heterogeneous" and plan.arm == "slow-roots"
        and slow_throttled_delta <= 0
    ):
        _fail(
            "cpu_manipulation_not_observed",
            "heterogeneous slow-root scopes show no throttling during measurement",
            observed=True,
        )
    return {
        "mode": mode,
        "contract_id": expected_contract.contract_id,
        "contract_sha256": expected_contract.contract_sha256,
        "sample_round_count": len(timestamps),
        "measurement_sample_first_monotonic_ns": first,
        "measurement_sample_last_monotonic_ns": last,
        "counter_deltas": deltas,
        "slow_root_throttled_usec_delta": slow_throttled_delta,
    }


def _validate_authorization(
    root: Path, receipt: Mapping[str, object], *, arm: str, mode: str,
) -> dict[str, object]:
    path = root / "authorization.json"
    document = _read_json(path, "CPU authorization")
    expected_keys = {
        "schema_version", "kind", "block_id", "block_order", "cell_ordinal",
        "revision", "profile_sha256", "arm", "quota_mode", "preflight_sha256",
        "binary_sha256", "output_root", "required_complete_cycles",
        "hard_timeout_s", "external_timeout_s", "automatic_retries",
        "claim_eligible", "figure_eligible", "approval_ref", "approved_at_utc",
    }
    order = [
        "slow-roots:homogeneous", "fast-roots:homogeneous",
        "slow-roots:heterogeneous", "fast-roots:heterogeneous",
    ]
    cell = f"{arm}:{mode}"
    preflight = receipt["preflight"]
    actual_sha = _sha256_file(path)
    canonical_preflight_sha = hashlib.sha256(
        feasibility.canonical_json(preflight)
    ).hexdigest()
    if (
        set(document) != expected_keys
        or document.get("schema_version") != 1
        or document.get("kind") != "kauri-w16-static-e0-exploratory-authorization-v1"
        or not isinstance(document.get("block_id"), str)
        or not str(document["block_id"]).startswith("w16-static-e0-")
        or document.get("block_order") != order
        or document.get("cell_ordinal") != order.index(cell) + 1
        or document.get("revision") != preflight["revision"]
        or document.get("profile_sha256") != preflight["profile_sha256"]
        or document.get("arm") != arm
        or document.get("quota_mode") != mode
        or document.get("preflight_sha256") != canonical_preflight_sha
        or document.get("binary_sha256") != preflight["binary_sha256"]
        or document.get("output_root") != str(root)
        or document.get("required_complete_cycles") != 5
        or not isinstance(document.get("hard_timeout_s"), (int, float))
        or isinstance(document.get("hard_timeout_s"), bool)
        or not 20 < float(document["hard_timeout_s"]) <= 300
        or document.get("external_timeout_s") != 540
        or document.get("automatic_retries") != 0
        or document.get("claim_eligible") is not False
        or document.get("figure_eligible") is not False
        or document.get("approval_ref")
        != "user-confirmation:2026-09-26:inesc-cpu-throughput"
        or not isinstance(document.get("approved_at_utc"), str)
        or not str(document["approved_at_utc"]).endswith("Z")
        or receipt.get("authorization_sha256") != actual_sha
    ):
        _fail("cpu_authorization", "CPU authorization does not bind this exact exploratory cell")
    return {
        "sha256": actual_sha,
        "block_id": document["block_id"],
        "cell_ordinal": document["cell_ordinal"],
        "approval_ref": document["approval_ref"],
        "approved_at_utc": document["approved_at_utc"],
        "preflight_byte_hash_recheckable": True,
    }


def validate_w16_output(root: Path) -> dict[str, object]:
    """Validate one immutable W16 output root without modifying it.

    ``PASS`` means the bounded evidence class named in the result is complete.
    It never means a throughput-improvement claim has passed: a single output
    root cannot establish the paired causal comparison, host/scheduler identity,
    or independent external attestation required for such a claim.
    """

    result: dict[str, object] = {
        "schema_version": 1,
        "kind": "kauri-w16-output-validation-v2",
        "verdict": "INCOMPLETE",
        "evidence_class": None,
        "claim_eligible": False,
        "figure_eligible": False,
        "limitations": [
            "A single arm cannot establish throughput improvement.",
            "The executor receipt is self-authored and does not attest scheduler or host identity.",
            "Executable bytes are hash-bound by preflight but are not archived in the output root.",
        ],
    }
    try:
        candidate = Path(root)
        if candidate.is_symlink() or not candidate.is_dir():
            _fail("output_root", "output root must be a regular directory, not a symlink")
        candidate = candidate.resolve()
        receipt_path = candidate / "feasibility-receipt.json"
        abort_path = candidate / "feasibility-abort.json"
        if receipt_path.exists() == abort_path.exists():
            _fail("outcome_cardinality", "output root must contain exactly one outcome")
        outcome = _read_json(receipt_path if receipt_path.exists() else abort_path, "executor outcome")
        if outcome.get("schema") != _RECEIPT_SCHEMA:
            _fail("receipt_schema", "executor outcome schema is not W16")
        if abort_path.exists() or outcome.get("verdict") != "PASS":
            _fail("producer_abort", "executor sealed an abort, not evidence")
        required_keys = {
            "schema", "run_id", "attempts", "retries", "verdict", "failure",
            "cleanup_error", "registered_replica_ids", "all_registered_exited",
            "treegen_sha256", "raw_sha256", "artifact_sha256",
            "quota_contract_sha256", "quota_cleanup", "required_complete_cycles",
            "quota_scope_cleanup",
            "preflight", "epoch_zero_digest", "started_monotonic_ns",
            "ended_monotonic_ns",
        }
        required_keys.add("authorization_sha256")
        if set(outcome) != required_keys:
            missing = sorted(required_keys - set(outcome))
            _fail("receipt_schema", f"executor receipt schema drifted; missing={missing}")
        if (
            outcome.get("attempts") != 1 or outcome.get("retries") != 0
            or outcome.get("failure") is not None or outcome.get("cleanup_error") is not None
            or outcome.get("registered_replica_ids") != list(range(31))
            or outcome.get("all_registered_exited") is not True
            or not _integer(outcome.get("started_monotonic_ns"), minimum=1)
            or not _integer(outcome.get("ended_monotonic_ns"), minimum=1)
            or int(outcome["ended_monotonic_ns"]) <= int(outcome["started_monotonic_ns"])
        ):
            _fail("receipt_outcome", "executor receipt does not prove one clean attempt")

        _validate_inventory(candidate, outcome)
        arm, plan = _validate_preflight(outcome)
        _validate_configuration(candidate, outcome, plan)
        if outcome.get("epoch_zero_digest") != _EPOCH_ZERO_DIGESTS[arm]:
            _fail("epoch_zero_identity", "native Epoch-0 digest differs from the frozen arm")
        cpu_run = outcome.get("quota_contract_sha256") is not None
        if (
            cpu_run != (outcome.get("quota_cleanup") is not None)
            or cpu_run != (outcome.get("quota_scope_cleanup") is not None)
        ):
            _fail("cpu_identity", "quota contract and cleanup presence disagree")
        events, terminal_sequence, start_ns, end_ns = _validate_streams(
            candidate, outcome, cpu_run=cpu_run
        )
        result["run_id"] = outcome["run_id"]
        result["revision"] = outcome["preflight"]["revision"]
        result["arm"] = arm
        result["epoch_zero_digest"] = outcome["epoch_zero_digest"]
        if cpu_run:
            throughput = _derive_throughput(
                events, start_ns=start_ns, end_ns=end_ns,
                digest=str(outcome["epoch_zero_digest"]),
            )
            result["quota"] = _validate_quota(
                candidate, outcome, plan=plan, start_ns=start_ns, end_ns=end_ns
            )
            result["authorization"] = _validate_authorization(
                candidate, outcome, arm=arm, mode=str(result["quota"]["mode"])
            )
            result["throughput"] = throughput
            result["evidence_class"] = "CPU_QUOTA_SINGLE_ARM"
        else:
            if outcome.get("required_complete_cycles") != 1:
                _fail("measurement_window", "CPU-free feasibility is not bound to one cycle")
            if (
                outcome.get("quota_contract_sha256") is not None
                or outcome.get("quota_cleanup") is not None
                or outcome.get("quota_scope_cleanup") is not None
                or any((candidate / relative).exists() for relative in (
                    "runtime/cpu-quota-contract.json", "runtime/cpu-quota-launch.json",
                    "runtime/cpu-quota-cleanup.json", "raw/cpu-quota-samples.jsonl",
                    "raw/cpu-quota-monitor-rounds.jsonl",
                ))
                or outcome.get("authorization_sha256") is not None
                or (candidate / "authorization.json").exists()
            ):
                _fail("cpu_identity", "CPU-free receipt contains quota evidence")
            result["throughput"] = None
            result["post_terminal_commit_chain"] = _post_terminal_chain(
                events, terminal_sequence=terminal_sequence,
                digest=str(outcome["epoch_zero_digest"]),
            )
            result["evidence_class"] = "CPU_FREE_FEASIBILITY"
        result["verdict"] = "PASS"
        result["reason_code"] = "bounded_evidence_complete"
        result["detail"] = (
            "Complete producer-bound single-run evidence; no paired improvement claim."
        )
    except _InvalidEvidence as exc:
        result["verdict"] = "FAIL" if exc.observed else "INCOMPLETE"
        result["reason_code"] = exc.code
        result["detail"] = exc.detail
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        result["verdict"] = "INCOMPLETE"
        result["reason_code"] = "validator_input_error"
        result["detail"] = str(exc) or type(exc).__name__
    return result
