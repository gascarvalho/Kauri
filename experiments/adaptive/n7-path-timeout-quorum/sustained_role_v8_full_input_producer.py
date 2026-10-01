"""Fresh-root v8 input closure; no approval or process-launch capability."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
from pathlib import Path
from typing import Any, Callable, Mapping


HERE = Path(__file__).resolve().parent
KAURI = HERE.parents[2]
PLAN = Path("runtime/sustained-role-v8-full-input-plan.json")
REQUEST = Path("runtime/sustained-role-v8-full-input-request.json")
FIXED_PLAN = Path("runtime/sustained-role-v8-fixed-e0-full-input-plan.json")
FIXED_REQUEST = Path("runtime/sustained-role-v8-fixed-e0-full-input-request.json")
_MIN_WINDOW_NS = 82_000_000_000
_PLAN_KIND = "kauri-n7-sustained-role-v8-full-input-plan-v1"
_REQUEST_KIND = "kauri-n7-sustained-role-v8-full-input-request-v1"
_FIXED_PLAN_KIND = "kauri-n7-sustained-role-v8-fixed-e0-full-input-plan-v1"
_FIXED_REQUEST_KIND = "kauri-n7-sustained-role-v8-fixed-e0-full-input-request-v1"
_REVISION = re.compile(r"[0-9a-f]{40}")
_ARTIFACT_NAMES = frozenset({
    "build_receipt", "launch_arguments", "preparation", "e0_identity", "issuer_public_key",
    "epoch0_tree", "main_config", "hotstuff_app", "adaptation_manager", "hotstuff_keygen",
    "hotstuff_tls_keygen", "e0_helper", "v8_profile", "selection_profile", "replica_configs",
})
_MAIN_CONFIG_KEYS = frozenset({
    "block-size", "nworker", "repnworker", "pace-maker", "proposer", "fan-out", "piped_latency",
    "async_blocks", "base-timeout", "prop-delay", "aggregation-timeout", "leader-progress-timeout",
    "leader-activation-grace", "client-ip", "tree-generation", "tree-generation-fpath", "tree-switch-period",
    "epoch-protocol-mode", "epoch-change-issuer-id", "epoch-change-issuer-public-key",
    "epoch-change-minimum-activation-delay", "epoch-change-maximum-activation-delay",
    "epoch-change-maximum-block-extra-bytes", "epoch-change-maximum-ancestry-blocks",
    "epoch-manager-address", "epoch-manager-tls-cert", "max-rep-msg", "replica",
})
_REPLICA_CONFIG_KEYS = frozenset({
    "privkey", "tls-privkey", "tls-cert", "idx", "structured-event-run-id",
    "structured-event-source-instance", "structured-event-output", "structured-event-commit-observer-id",
    "structured-event-commit-observer-instance", "experiment-exact-timeout-attempt-evidence-v3",
})


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prep = _load("w19_v8_full_prep", HERE / "sustained_role_v8_preparation.py")


class FullInputError(ValueError):
    pass


def _canon(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii") + b"\n"


def _json(raw: bytes, label: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise FullInputError(f"{label} has duplicate JSON key")
            result[key] = value
        return result
    try:
        document = json.loads(raw.decode("ascii"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FullInputError(f"{label} is not JSON") from exc
    if not isinstance(document, dict) or raw != _canon(document):
        raise FullInputError(f"{label} is not canonical JSON")
    return document


def _fresh_root(root: Path) -> Path:
    supplied = Path(root)
    if not supplied.is_absolute() or supplied.is_symlink() or supplied != supplied.resolve() or supplied.exists():
        raise FullInputError("fresh canonical non-symlink root required")
    return supplied


def _safe_file(root: Path, relative: str, *, label: str) -> Path:
    candidate = Path(relative)
    if not relative or candidate.is_absolute() or ".." in candidate.parts:
        raise FullInputError(f"{label} path is unsafe")
    path = root
    for part in candidate.parts:
        path = path / part
        if path.is_symlink():
            raise FullInputError(f"{label} traverses a symlink")
    if not path.is_file():
        raise FullInputError(f"{label} is not a regular file")
    return path


def _desc(root: Path, path: Path) -> dict[str, object]:
    relative = str(path.relative_to(root))
    checked = _safe_file(root, relative, label="closure artifact")
    raw = checked.read_bytes()
    return {"path": relative, "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _descriptor_file(root: Path, value: object, *, label: str) -> Path:
    if not isinstance(value, Mapping) or set(value) != {"path", "size_bytes", "sha256"}:
        raise FullInputError(f"{label} descriptor drifted")
    if (not isinstance(value["path"], str) or type(value["size_bytes"]) is not int or
            value["size_bytes"] < 0 or not isinstance(value["sha256"], str) or len(value["sha256"]) != 64):
        raise FullInputError(f"{label} descriptor drifted")
    path = _safe_file(root, value["path"], label=label)
    raw = path.read_bytes()
    if len(raw) != value["size_bytes"] or hashlib.sha256(raw).hexdigest() != value["sha256"]:
        raise FullInputError(f"{label} descriptor drifted")
    return path


def _source_instance_from_config(path: Path, replica: int, *, run_id: str) -> str:
    try:
        if prep.local._config_option(path, "idx", f"replica-{replica} configuration") != str(replica):
            raise FullInputError("replica configuration index drifted")
        if prep.local._config_option(path, "structured-event-run-id", f"replica-{replica} configuration") != run_id:
            raise FullInputError("replica configuration run id drifted")
        return prep.local._config_option(path, "structured-event-source-instance", f"replica-{replica} configuration")
    except Exception as exc:
        if isinstance(exc, FullInputError):
            raise
        raise FullInputError("replica configuration source identity drifted") from exc


def _config_schema(path: Path, *, main: bool) -> None:
    """Reject inert or future config additions outside this v8 materialization schema.

    Dynamic key and certificate values are bound by the archived descriptors
    and semantic profile checks below; this deliberately freezes the emitted
    key grammar so an unknown native option cannot hide in a resealed file.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise FullInputError("materialized config cannot be decoded") from exc
    keys: list[str] = []
    for line in lines:
        if not line or " = " not in line:
            raise FullInputError("materialized config grammar drifted")
        key, value = line.split(" = ", 1)
        if not key or not value:
            raise FullInputError("materialized config grammar drifted")
        keys.append(key)
    allowed = _MAIN_CONFIG_KEYS if main else _REPLICA_CONFIG_KEYS
    if any(key not in allowed for key in keys):
        raise FullInputError("materialized config contains an unknown key")
    if main:
        if set(keys) != allowed or keys.count("replica") != 7 or any(keys.count(key) != 1 for key in allowed - {"replica"}):
            raise FullInputError("main config key cardinality drifted")
    elif set(keys) != allowed or any(keys.count(key) != 1 for key in allowed):
        raise FullInputError("replica config key cardinality drifted")


def _validate_launch_semantics(*, launch_processes: list[object], files: Mapping[str, Path],
                               replicas: list[Path], run_id: str, arm: str,
                               start_ns: int, end_ns: int) -> None:
    """Bind normalized argv/effective metadata back to frozen generated inputs."""
    _config_schema(files["main_config"], main=True)
    for replica_config in replicas:
        _config_schema(replica_config, main=False)
    rows = {(row.get("source_kind"), row.get("source_id")): row
            for row in launch_processes if isinstance(row, Mapping)}
    manager = rows[("adaptation_manager", "adaptive-manager")]
    manager_argv = manager.get("argv")
    if not isinstance(manager_argv, list) or manager_argv[0:1] != [str(files["adaptation_manager"])]:
        raise FullInputError("manager executable binding drifted")
    replica_positions = [index for index, value in enumerate(manager_argv) if value == "--replica"]
    if len(replica_positions) != 7:
        raise FullInputError("manager replica endpoint cardinality drifted")
    endpoint_ids: set[int] = set()
    for position in replica_positions:
        if position + 1 >= len(manager_argv):
            raise FullInputError("manager replica endpoint cardinality drifted")
        fields = manager_argv[position + 1].split(",")
        if len(fields) != 3 or fields[2] != "<fingerprinted>":
            raise FullInputError("manager replica credential binding drifted")
        try:
            replica = int(fields[0])
        except ValueError as exc:
            raise FullInputError("manager replica endpoint identity drifted") from exc
        if replica not in range(7) or not fields[1].startswith("127.0.0.1:"):
            raise FullInputError("manager replica endpoint identity drifted")
        endpoint_ids.add(replica)
    if endpoint_ids != set(range(7)):
        raise FullInputError("manager replica endpoint identity drifted")
    for option, placeholder in (("--tls-privkey", "<redacted>"),
                                ("--issuer-private-key", "<redacted>"),
                                ("--tls-cert", "<fingerprinted>")):
        positions = [index for index, value in enumerate(manager_argv) if value == option]
        if len(positions) != 1 or positions[0] + 1 >= len(manager_argv) or manager_argv[positions[0] + 1] != placeholder:
            raise FullInputError("manager credential cardinality drifted")
    try:
        prep.local._require_materialized_profile_bindings(
            arm=arm, epoch0_tree=files["epoch0_tree"], main_config=files["main_config"],
            replica_configs=replicas, manager=manager_argv, hard_timeout_seconds=210)
    except Exception as exc:
        raise FullInputError("frozen materialized consensus profile drifted") from exc
    if arm == "fixed_e0":
        identity = _json(files["e0_identity"].read_bytes(), "E0 identity")
        expected = {
            "--scheduled-fixed-e0-run-id": run_id,
            "--scheduled-fixed-e0-profile-sha256": hashlib.sha256(files["v8_profile"].read_bytes()).hexdigest(),
            "--scheduled-fixed-e0-epoch-zero-digest": identity.get("epoch_digest"),
            "--scheduled-fixed-e0-window-start-monotonic-ns": str(start_ns),
            "--scheduled-fixed-e0-window-end-monotonic-ns": str(end_ns),
        }
        if (manager_argv.count("--scheduled-fixed-e0-control") != 1 or
                any(option in manager_argv for option in ("--transition-request", "--bundle-output", "--convergence-deadline-seconds")) or
                any(option.startswith("--fault-window-arm-") for option in manager_argv) or
                any(manager_argv.count(option) != 1 or
                    manager_argv.index(option) + 1 >= len(manager_argv) or
                    manager_argv[manager_argv.index(option) + 1] != value
                    for option, value in expected.items())):
            raise FullInputError("fixed-E0 native scheduled-control binding drifted")
    elif (manager_argv.count("--convergence-deadline-seconds") != 1 or
          manager_argv[manager_argv.index("--convergence-deadline-seconds") + 1] != "12"):
        raise FullInputError("adaptive manager convergence deadline drifted")
    try:
        prep._roles(files["epoch0_tree"])
        overlay = prep.v8.native_actor_overlay(
            scheduled_start_monotonic_ns=start_ns, scheduled_end_monotonic_ns=end_ns)
    except Exception as exc:
        raise FullInputError("frozen E0 role or physical omission schedule drifted") from exc
    runtime = prep.local.base.runtime_parameters(
        prep.v8.materialization_profile(), app_binary=files["hotstuff_app"],
        manager_binary=files["adaptation_manager"])
    for replica in range(7):
        row = rows[("replica", f"replica-{replica}")]
        argv = row.get("argv")
        options = row.get("effective_options")
        if (not isinstance(argv, list) or argv[0:1] != [str(files["hotstuff_app"])] or
                not isinstance(options, Mapping) or
                options.get("binary_sha256") != hashlib.sha256(files["hotstuff_app"].read_bytes()).hexdigest() or
                options.get("main_config_sha256") != hashlib.sha256(files["main_config"].read_bytes()).hexdigest() or
                options.get("replica_config_sha256") != hashlib.sha256(replicas[replica].read_bytes()).hexdigest()):
            raise FullInputError("replica executable or effective metadata drifted")
        if replica == 1:
            if argv[-len(overlay):] != list(overlay):
                raise FullInputError("actor-1 physical omission argv drifted")
        elif "--experiment-byzantine-mode" in argv:
            raise FullInputError("physical omission argv appears outside actor 1")
        for key in ("block_size", "pipeline_depth", "aggregation_timeout_ms", "leader_progress_timeout_ms",
                    "leader_activation_grace_ms", "fanout", "tree_switch_period_blocks"):
            if options.get(key) != runtime[key]:
                raise FullInputError("replica frozen effective profile drifted")
    manager_options = manager.get("effective_options")
    if (not isinstance(manager_options, Mapping) or
            manager_options.get("binary_sha256") != hashlib.sha256(files["adaptation_manager"].read_bytes()).hexdigest() or
            not isinstance(manager_options.get("replica_tls_certificate_sha256"), list) or
            len(manager_options["replica_tls_certificate_sha256"]) != 7):
        raise FullInputError("manager effective metadata drifted")


def prepare(root: Path, *, run_id: str, ports: tuple[int, int, int], start_ns: int, end_ns: int,
            binaries: Mapping[str, Path], build_receipt: Path,
            arm: str = "adaptive_e1",
            repository_verifier: Callable[[Path], object] = prep.local.base.verify_repository_state) -> dict[str, str]:
    root = _fresh_root(root)
    if arm not in {"adaptive_e1", "fixed_e0"}:
        raise FullInputError("v8 full-input arm is invalid")
    if type(start_ns) is not int or type(end_ns) is not int or start_ns <= 0 or end_ns - start_ns < _MIN_WINDOW_NS:
        raise FullInputError("native scheduled window is invalid")
    try:
        snapshot = repository_verifier(KAURI)
        revision = prep.local._require_clean_snapshot(snapshot)
    except Exception as exc:
        raise FullInputError("clean pushed repository verification failed") from exc
    if build_receipt.is_symlink() or not build_receipt.is_file():
        raise FullInputError("external build receipt is not regular")
    try:
        made = prep.prepare_inputs(
            root, run_id=run_id, ports=ports, binaries=binaries,
            scheduled_start_monotonic_ns=start_ns, scheduled_end_monotonic_ns=end_ns,
            arm=arm)
        runtime = root / "runtime"
        archived = runtime / "external-clean-build-receipt.json"
        with build_receipt.open("rb") as incoming, archived.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
        artifacts: dict[str, object] = {
            "build_receipt": _desc(root, archived),
            "launch_arguments": _desc(root, runtime / "launch-arguments.json"),
            "preparation": _desc(root, runtime / "sustained-role-v8-preparation.json"),
            "e0_identity": _desc(root, runtime / "e0-identity-receipt.json"),
            "issuer_public_key": _desc(root, runtime / "issuer-public-key.txt"),
            "epoch0_tree": _desc(root, Path(made["epoch0_tree"])),
            "main_config": _desc(root, Path(made["main_config"])),
            "hotstuff_app": _desc(root, root / "materialization-binaries/hotstuff-app"),
            "adaptation_manager": _desc(root, root / "materialization-binaries/adaptation-manager"),
            "hotstuff_keygen": _desc(root, root / "materialization-binaries/hotstuff-keygen"),
            "hotstuff_tls_keygen": _desc(root, root / "materialization-binaries/hotstuff-tls-keygen"),
            "e0_helper": _desc(root, root / "materialization-binaries/e0-identity-helper"),
            "v8_profile": _desc(root, runtime / "sustained-role-v8-materialization-profile.json"),
            "selection_profile": _desc(root, runtime / "inherited-selection-profile-v4.json"),
            "replica_configs": [_desc(root, path) for path in made["replica_configs"]],
        }
        launch = _json((runtime / "launch-arguments.json").read_bytes(), "launch metadata")
        processes: list[dict[str, object]] = []
        for item in launch.get("processes", []):
            source = item.get("source_id")
            if item.get("source_kind") == "adaptation_manager" and source == "adaptive-manager":
                instance = prep.local._one_option(made["manager_command"], "--structured-event-source-instance")
            elif item.get("source_kind") == "replica" and isinstance(source, str) and source.startswith("replica-"):
                replica = int(source.removeprefix("replica-"))
                if replica not in range(7):
                    raise FullInputError("launch metadata replica identity drifted")
                instance = _source_instance_from_config(Path(made["replica_configs"][replica]), replica, run_id=run_id)
            else:
                raise FullInputError("launch metadata source identity drifted")
            argv = item.get("argv")
            if not isinstance(argv, list) or not all(isinstance(argument, str) for argument in argv):
                raise FullInputError("launch metadata argv drifted")
            processes.append({"source_kind": item["source_kind"], "source_id": source,
                              "source_instance": instance, "argv": argv,
                              "argv_sha256": hashlib.sha256(_canon(argv)).hexdigest()})
        profile_raw = (runtime / "sustained-role-v8-materialization-profile.json").read_bytes()
        plan: dict[str, object] = {
            "schema_version": 1, "kind": _FIXED_PLAN_KIND if arm == "fixed_e0" else _PLAN_KIND,
            "state": "PREPARED_INPUTS_NO_LAUNCH",
            "run_id": run_id, "repository_revision": revision,
            "profile_id": "n7-sustained-role-proposal-boundary-v8",
            "profile_sha256": hashlib.sha256(profile_raw).hexdigest(), "hard_timeout_seconds": 210,
            "scheduled_window": {"clock": "CLOCK_MONOTONIC_RAW", "start_ns": start_ns,
                                 "end_ns": end_ns, "minimum_duration_ns": _MIN_WINDOW_NS},
            "processes": processes, "artifacts": artifacts, "no_retry": True,
            "claim_eligible": False, "figure_eligible": False,
            "build_provenance": "ARCHIVED_NOT_LIVE_ATTESTED",
        }
        if arm == "fixed_e0":
            plan["arm"] = arm
        plan["plan_sha256"] = hashlib.sha256(_canon(plan)).hexdigest()
        request = {"schema_version": 1, "kind": _FIXED_REQUEST_KIND if arm == "fixed_e0" else _REQUEST_KIND,
                   "plan_sha256": plan["plan_sha256"],
                   "run_id": run_id, "no_launch": True, "no_retry": True}
        if arm == "fixed_e0":
            request["arm"] = arm
        plan_path, request_path = (FIXED_PLAN, FIXED_REQUEST) if arm == "fixed_e0" else (PLAN, REQUEST)
        for path, value in ((root / plan_path, plan), (root / request_path, request)):
            with path.open("xb") as output:
                output.write(_canon(value))
        return {"plan_sha256": plan["plan_sha256"], "request_sha256": hashlib.sha256(_canon(request)).hexdigest()}
    except Exception as exc:
        if root.exists():
            runtime = root / "runtime"
            runtime.mkdir(mode=0o700, exist_ok=True)
            abort = runtime / "sustained-role-v8-full-input-abort.json"
            try:
                with abort.open("xb") as output:
                    output.write(_canon({"schema_version": 1, "state": "ABORTED_NO_RETRY_NO_LAUNCH",
                                         "reason": type(exc).__name__, "no_retry": True}))
            except FileExistsError:
                pass
        raise


def verify(root: Path, *, expected_request_sha256: str,
           arm: str = "adaptive_e1") -> dict[str, str]:
    """Reopen one sealed no-launch closure using an externally pinned request digest."""
    supplied_root = Path(root)
    if (not supplied_root.is_absolute() or supplied_root.is_symlink() or
            supplied_root != supplied_root.resolve() or not supplied_root.is_dir() or
            not isinstance(expected_request_sha256, str) or len(expected_request_sha256) != 64):
        raise FullInputError("canonical root and expected request hash required")
    root = supplied_root
    if arm not in {"adaptive_e1", "fixed_e0"}:
        raise FullInputError("v8 full-input arm is invalid")
    plan_path, request_path = (FIXED_PLAN, FIXED_REQUEST) if arm == "fixed_e0" else (PLAN, REQUEST)
    plan_kind, request_kind = (_FIXED_PLAN_KIND, _FIXED_REQUEST_KIND) if arm == "fixed_e0" else (_PLAN_KIND, _REQUEST_KIND)
    plan_raw = _safe_file(root, str(plan_path), label="plan").read_bytes()
    plan = _json(plan_raw, "plan")
    required_plan = {"schema_version", "kind", "state", "run_id", "repository_revision", "profile_id",
                     "profile_sha256", "hard_timeout_seconds", "scheduled_window", "processes", "artifacts",
                     "no_retry", "claim_eligible", "figure_eligible", "build_provenance", "plan_sha256"}
    if arm == "fixed_e0":
        required_plan.add("arm")
    without_hash = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if (set(plan) != required_plan or plan.get("schema_version") != 1 or plan.get("kind") != plan_kind or
            (arm == "fixed_e0" and plan.get("arm") != "fixed_e0") or
            plan.get("state") != "PREPARED_INPUTS_NO_LAUNCH" or not isinstance(plan.get("run_id"), str) or
            not plan["run_id"] or plan.get("no_retry") is not True or plan.get("claim_eligible") is not False or
            plan.get("figure_eligible") is not False or plan.get("build_provenance") != "ARCHIVED_NOT_LIVE_ATTESTED" or
            _REVISION.fullmatch(str(plan.get("repository_revision"))) is None or
            plan.get("plan_sha256") != hashlib.sha256(_canon(without_hash)).hexdigest()):
        raise FullInputError("plan schema or self-hash drifted")
    request_raw = _safe_file(root, str(request_path), label="request").read_bytes()
    request = _json(request_raw, "request")
    required_request = {"schema_version", "kind", "plan_sha256", "run_id", "no_launch", "no_retry"}
    if arm == "fixed_e0":
        required_request.add("arm")
    if (set(request) != required_request or request.get("schema_version") != 1 or request.get("kind") != request_kind or
            (arm == "fixed_e0" and request.get("arm") != "fixed_e0") or
            request.get("plan_sha256") != plan["plan_sha256"] or request.get("run_id") != plan["run_id"] or
            request.get("no_launch") is not True or request.get("no_retry") is not True or
            hashlib.sha256(request_raw).hexdigest() != expected_request_sha256):
        raise FullInputError("request binding drifted")
    window = plan.get("scheduled_window")
    if (not isinstance(window, Mapping) or set(window) != {"clock", "start_ns", "end_ns", "minimum_duration_ns"} or
            window.get("clock") != "CLOCK_MONOTONIC_RAW" or type(window.get("start_ns")) is not int or
            type(window.get("end_ns")) is not int or type(window.get("minimum_duration_ns")) is not int or
            window["start_ns"] <= 0 or window["minimum_duration_ns"] != _MIN_WINDOW_NS or
            window["end_ns"] - window["start_ns"] < _MIN_WINDOW_NS):
        raise FullInputError("native scheduled window drifted")
    artifacts = plan.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != _ARTIFACT_NAMES:
        raise FullInputError("artifact membership drifted")
    replica_descriptors = artifacts["replica_configs"]
    if not isinstance(replica_descriptors, list) or len(replica_descriptors) != 7:
        raise FullInputError("replica configuration membership drifted")
    files = {name: _descriptor_file(root, descriptor, label=name)
             for name, descriptor in artifacts.items() if name != "replica_configs"}
    replicas = [_descriptor_file(root, descriptor, label=f"replica-{index} configuration")
                for index, descriptor in enumerate(replica_descriptors)]
    exact_profile = _canon(prep.v8.materialization_profile())
    if (plan.get("profile_id") != "n7-sustained-role-proposal-boundary-v8" or
            files["v8_profile"].read_bytes() != exact_profile or
            plan.get("profile_sha256") != hashlib.sha256(exact_profile).hexdigest() or
            plan.get("hard_timeout_seconds") != 210):
        raise FullInputError("profile binding drifted")
    launch = _json(files["launch_arguments"].read_bytes(), "launch metadata")
    expected_sources = {("adaptation_manager", "adaptive-manager"),
                        *{("replica", f"replica-{index}") for index in range(7)}}
    launch_processes = launch.get("processes")
    if (not isinstance(launch_processes, list) or len(launch_processes) != 8 or
            {(row.get("source_kind"), row.get("source_id")) for row in launch_processes if isinstance(row, Mapping)} != expected_sources):
        raise FullInputError("launch metadata source membership drifted")
    _validate_launch_semantics(launch_processes=launch_processes, files=files,
                               replicas=replicas, run_id=plan["run_id"], arm=arm,
                               start_ns=window["start_ns"], end_ns=window["end_ns"])
    launch_rows = {(row["source_kind"], row["source_id"]): row.get("argv") for row in launch_processes}
    processes = plan.get("processes")
    if (not isinstance(processes, list) or len(processes) != 8 or
            {(item.get("source_kind"), item.get("source_id")) for item in processes if isinstance(item, Mapping)} != expected_sources):
        raise FullInputError("process membership drifted")
    instances: set[str] = set()
    for item in processes:
        if not isinstance(item, Mapping) or set(item) != {"source_kind", "source_id", "source_instance", "argv", "argv_sha256"}:
            raise FullInputError("process schema drifted")
        source_kind, source_id, instance, argv = (item["source_kind"], item["source_id"],
                                                    item["source_instance"], item["argv"])
        if (not isinstance(instance, str) or not instance or not isinstance(argv, list) or
                not all(isinstance(argument, str) for argument in argv) or
                item["argv_sha256"] != hashlib.sha256(_canon(argv)).hexdigest() or
                launch_rows.get((source_kind, source_id)) != argv):
            raise FullInputError("process argv drifted")
        if source_kind == "adaptation_manager":
            try:
                expected_instance = prep.local._one_option(argv, "--structured-event-source-instance")
                if prep.local._one_option(argv, "--structured-event-run-id") != plan["run_id"]:
                    raise FullInputError("manager run identity drifted")
                if arm == "adaptive_e1" and prep.local._one_option(argv, "--convergence-deadline-seconds") != "12":
                    raise FullInputError("manager convergence deadline drifted")
            except Exception as exc:
                if isinstance(exc, FullInputError):
                    raise
                raise FullInputError("manager argv semantics drifted") from exc
        else:
            replica = int(str(source_id).removeprefix("replica-"))
            expected_instance = _source_instance_from_config(replicas[replica], replica, run_id=plan["run_id"])
            try:
                prep.local._replica_config_binding(argv, main_config=files["main_config"], replica_config=replicas[replica])
            except Exception as exc:
                raise FullInputError("replica argv semantics drifted") from exc
        if instance != expected_instance or instance in instances:
            raise FullInputError("source instance identity drifted")
        instances.add(instance)
    return {"plan_sha256": plan["plan_sha256"]}
