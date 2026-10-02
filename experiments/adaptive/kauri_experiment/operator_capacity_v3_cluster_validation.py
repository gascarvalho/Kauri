"""Full cluster authority plus independent W18 protocol, quota and metric replay.

Local rehearsal receipts cannot enter this API. A complete result remains
non-claim-bearing until the separately frozen matched campaign is evaluated.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json

from . import operator_capacity_v3_cluster as cluster
from . import operator_capacity_v3_validation_bridge as bridge
from . import operator_capacity_v3_authority as authority
from . import operator_capacity_v3_backend as backend
from . import operator_capacity_v3_raw_validator as raw
from . import operator_capacity_v3_readiness_replay as readiness
from . import operator_capacity_v3_materializer as materializer
from . import factorial_validation
from . import cpu_quota
from . import operator_capacity_v3_cluster_profiles as profiles
from .operator_capacity_v3_cluster_build import TOOLS, DEPS_SHA


TERMINAL = "runtime/cluster-terminal.json"
INVENTORY = "runtime/cluster-inventory.json"
RAW_AUTHORITY = "runtime/cluster-raw-authority.json"


def inventory(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise cluster.ClusterError("cluster raw archive contains a symlink")
        if path.is_file() and str(path.relative_to(root)) not in {TERMINAL, INVENTORY}:
            result[str(path.relative_to(root))] = cluster.sha(path.read_bytes())
    return result


def _cluster_chain(root):
    request, request_raw = cluster.read(root / "runtime/cluster-request.json")
    approval, approval_raw = cluster.read(root / "runtime/cluster-approval.json")
    build, build_raw = cluster.read(root / "runtime/cluster-build.json")
    observed, _ = cluster.read(root / "runtime/cluster-pre-spawn.json")
    post, _ = cluster.read(root / "runtime/cluster-post-scope.json")
    receipt, receipt_raw = cluster.read(root / "runtime/cluster-receipt.json")
    argv, _ = cluster.read(root / "private-argv.json")
    quota = root / "runtime/frozen-cpu-quota-contract.json"
    plan = backend.inspect_retained_cluster_backend(materialization_root=root,
        manager_argv=argv["manager"], replica_argv=argv["replicas"], quota_profile=quota,
        cluster_physical_regime=request["physical_regime"])
    expected = cluster.build_request(plan, root=root, run_id=request["run_id"],
        build_receipt=root / "runtime/cluster-build.json", booking_id=request["booking_id"])
    if request_raw != cluster.canonical(expected):
        raise cluster.ClusterError("sealed cluster request differs from materialization/build")
    profiles.load_cluster_contract(quota, base_profile_path=root / "runtime/cluster-base-profile.json",
                                   regime=request["physical_regime"])
    cluster.verify_approval(request, root / "runtime/cluster-approval.json", observed["external_approval_sha256"])
    revision = request["revision"]
    if (receipt["kind"] != cluster.RECEIPT_KIND or
            receipt["verdict"] != "PROCESS_COMPLETED_PENDING_RAW_VALIDATION" or
            receipt["failure"] is not None or receipt["automatic_retries"] != 0 or
            receipt["execution_request_sha256"] != cluster.sha(request_raw) or
            receipt["manager_exit_code"] not in (None, 0) or
            type(receipt["manager_exit_code_after_cleanup"]) is not int or
            receipt["manager_success_terminal_verified"] is not True or
            receipt["claim_eligible"] is not False or receipt["figure_eligible"] is not False):
        raise cluster.ClusterError("cluster child lacks a successful sealed no-retry receipt")
    if (build["kind"] != "kauri-w18-cluster-build-provenance-v1" or
            build["repository_revision"] != revision or build["origin_revision"] != revision or
            build["repository_clean_after_build"] is not True or build["build_exit_code"] != 0 or
            build["build_type"] != "Release" or build["host"] != "proteina02" or
            observed["repository_revision"] != revision or observed["host"] != "proteina02" or
            observed["user"] != "gascarvalho" or observed["linux_boot_id"] != build["linux_boot_id"] or
            observed["owned_native_processes"] != [] or
            observed["build_receipt_sha256"] != cluster.sha(build_raw)):
        raise cluster.ClusterError("cluster build/host/source provenance differs")
    if (set(build["binaries"]) != set(TOOLS) or build["dependency_tree_sha256"] != DEPS_SHA or
            cluster.sha(authority._read(Path(build["build_log_path"]), "actual native build log",
                                       512 * 1024 * 1024)) != build["build_log_sha256"]):
        raise cluster.ClusterError("native build log, dependency closure or complete tool map differs")
    cluster.booking_row(observed["booking_stdout"], request["booking_id"],
        datetime.fromisoformat(observed["observed_utc"]))
    if observed["booking_row"] != cluster.booking_row(observed["booking_stdout"],
            request["booking_id"], datetime.fromisoformat(observed["observed_utc"])):
        raise cluster.ClusterError("retained exact booking row differs")
    unit = "kauri-w18-" + request["run_id"] + ".scope"
    if (post["unit"] != unit or observed["unit"] != unit or post["run_id"] != request["run_id"] or
            post["scope_returncode"] != 0 or post["failure"] is not None or post["automatic_retries"] != 0 or
            not post["scope_started_ns"] <= observed["spawn_observed_ns"] < post["scope_returned_ns"] or
            post["scope_returned_ns"] - post["scope_started_ns"] > 330_000_000_000 or
            not any(line.startswith("0::/") and line.endswith("/" + unit)
                    for line in observed["child_cgroup"].splitlines())):
        raise cluster.ClusterError("bounded scope chronology or exact child cgroup differs")
    expected_units = {cpu_quota._unit_name(request["run_id"], i) for i in range(31)} | {unit}
    if (len(post["cleanup"]) != 32 or {item["unit"] for item in post["cleanup"]} != expected_units or
            any(item.get("populated") != 0 or "error" in item for item in post["cleanup"])):
        raise cluster.ClusterError("cluster wrapper did not prove all 32 owned scopes empty")
    for i in range(31):
        limit, _ = cluster.read(root / f"runtime/cluster-replica-{i}-scope-limit.json")
        if (limit["unit"] != cpu_quota._unit_name(request["run_id"], i) or
                limit["hard_timeout_s"] != 300 or limit["raw_properties"].strip()
                not in {"RuntimeMaxUSec=5min", "RuntimeMaxUSec=300s"}):
            raise cluster.ClusterError("replica scope lacked unchanged 300-second ceiling")
    tool, tool_raw = cluster.read(root / "runtime/tool-identity-approval.json")
    manifest, _ = cluster.read(root / "materialization-manifest.json")
    if cluster.sha(tool_raw) != manifest["tool_identity_approval_receipt_sha256"]:
        raise cluster.ClusterError("tool approval differs from materialization")
    for name, row in build["binaries"].items():
        if cluster.sha(authority._read(Path(row["path"]), "exact built verifier", 512 * 1024 * 1024)) != row["sha256"]:
            raise cluster.ClusterError("actual native build tool hash differs")
        if name != "readiness_verifier" and tool["binary_sha256"].get(name) != row["sha256"]:
            raise cluster.ClusterError("tool approval differs from build map")
    return request, build, receipt, receipt_raw


def produce_raw_authority(root, build, receipt_raw):
    a = Path(build["binaries"]["stage_a_envelope_verifier"]["path"])
    b = Path(build["binaries"]["stage_b_authorization_verifier"]["path"])
    inputs = bridge.derive_validation_inputs(root, stage_a_verifier_binary=a, stage_b_verifier_binary=b)
    manifest, manifest_raw = cluster.read(root / "materialization-manifest.json")
    target = root / "runtime/stage-b-verifier-receipt.json"
    if not target.exists():
        authority._materialize_stage_b_receipt(inputs.stage_b_command, root=root,
            manifest=manifest, pins=inputs.pins, target=target, runner=__import__("subprocess").run)
    result = {"kind": "kauri-w18-cluster-raw-authority-v1", "runner_receipt_sha256": cluster.sha(receipt_raw),
        "materialization_manifest_sha256": cluster.sha(manifest_raw), "pins": inputs.pins,
        "stage_a_verifier_receipt": "runtime/stage-a-verifier-receipt.json",
        "stage_b_verifier_receipt": "runtime/stage-b-verifier-receipt.json",
        "event_stream_sha256": {name: cluster.sha(authority._read(root / relative, name)) for name, relative in
            [("manager", "raw/manager-events.jsonl")] + [(f"replica-{i}", f"raw/replica-{i}.jsonl") for i in range(31)]}}
    for key, relative in (("frozen_contract", "runtime/frozen-cpu-quota-contract.json"),
            ("contract", "runtime/cpu-quota-contract.json"), ("launch", "runtime/cpu-quota-launch.json"),
            ("samples", "raw/cpu-quota-samples.jsonl"), ("rounds", "raw/cpu-quota-monitor-rounds.jsonl")):
        result["cpu_quota_" + key + "_sha256"] = cluster.sha(authority._read(root / relative, key))
    return result


def validate(root, raw_authority):
    request, build, receipt, receipt_raw = _cluster_chain(root)
    regenerated = produce_raw_authority(root, build, receipt_raw)
    if cluster.canonical(raw_authority) != cluster.canonical(regenerated):
        raise cluster.ClusterError("cluster raw pins differ from independently reopened files")
    result = raw._validate_evidence(root, receipt=receipt, authority=raw_authority,
        independently_recompute_verifiers=lambda r, d: bridge.independently_recompute_verifiers(r, d,
            stage_a_verifier_binary=Path(build["binaries"]["stage_a_envelope_verifier"]["path"]),
            stage_b_verifier_binary=Path(build["binaries"]["stage_b_authorization_verifier"]["path"])),
        cluster_physical_regime=request["physical_regime"])
    ready = readiness.verify_native_readiness(root,
        verifier_binary=Path(build["binaries"]["readiness_verifier"]["path"]),
        expected_verifier_sha256=build["binaries"]["readiness_verifier"]["sha256"])
    activations = []
    for i in range(31):
        events = readiness._events(root, f"raw/replica-{i}.jsonl", request["run_id"], f"replica-{i}")
        event = next(e for e in events if e["event_type"] == "epoch.activated")
        activations.append({"replica_id": i, "source_id": event["source_id"],
            "source_sequence": event["source_sequence"], "source_monotonic_ns": event["source_monotonic_ns"],
            "epoch_digest": event["payload"]["epoch_digest"]})
    window = receipt["e1_measurement_window"]
    if (window["post_e1_window_start_monotonic_ns"] != max(e["source_monotonic_ns"] for e in activations) or
            cluster.canonical(window["all_replica_e1_activation_events"]) != cluster.canonical(activations)):
        raise cluster.ClusterError("measurement is not exactly anchored to all31 native activations")
    projection, _ = cluster.read(root / "config/identity-public-projection.json")
    bundle = factorial_validation.decode_adaptive_v3_epoch_change_bundle(authority._read(
        root / "transitions/e0-to-e1-operator-capacity/successor.bundle", "signed E1"),
        issuer_public_key=projection["issuer_public_key"])
    expected = [tuple(int(x) for x in row.split()[2:])
                for row in materializer.canonical_e0_tree().decode().splitlines()]
    if request["arm"] == "treatment":
        fast, slow = list(range(6, 31)), list(range(6))
        expected = [tuple(fast[(i + j) % 25] for j in range(25)) +
                    tuple(slow[(i + j) % 6] for j in range(6)) for i in range(21)]
    if (len(bundle.trees) != 21 or any(tree.tree_id != i or tree.fanout != 5 or
            tree.pipeline_stretch != 2 or tree.wait_exempt or tree.members != expected[i]
            for i, tree in enumerate(bundle.trees))):
        raise cluster.ClusterError("signed successor placement differs from frozen real/sham policy")
    return {**result, "kind": "kauri-w18-cluster-raw-result-v1", "physical_regime": request["physical_regime"],
        "arm": request["arm"], "run_id": request["run_id"], "repository_revision": request["revision"],
        "full_cluster_authority_verified": True, "readiness_replay": ready,
        "signed_successor_placement_verified": True}


def seal(root):
    _, build, _, receipt_raw = _cluster_chain(root)
    pins = produce_raw_authority(root, build, receipt_raw)
    result = validate(root, pins)
    cluster.write(root / RAW_AUTHORITY, pins)
    cluster.write(root / INVENTORY, inventory(root))
    terminal = {"kind": "kauri-w18-cluster-terminal-v1", "result": result,
        "raw_authority_sha256": cluster.sha(authority._read(root / RAW_AUTHORITY, "raw authority")),
        "inventory_sha256": cluster.sha(authority._read(root / INVENTORY, "inventory"))}
    cluster.write(root / TERMINAL, terminal)
    return terminal


def replay(root, *, expected_terminal_sha256):
    terminal, terminal_raw = cluster.read(root / TERMINAL)
    files, files_raw = cluster.read(root / INVENTORY)
    pins, pins_raw = cluster.read(root / RAW_AUTHORITY)
    if (cluster.sha(terminal_raw) != expected_terminal_sha256 or
            cluster.sha(files_raw) != terminal["inventory_sha256"] or
            cluster.sha(pins_raw) != terminal["raw_authority_sha256"] or
            cluster.canonical(files) != cluster.canonical(inventory(root))):
        raise cluster.ClusterError("sealed terminal/raw inventory changed")
    result = validate(root, pins)
    if cluster.canonical(result) != cluster.canonical(terminal["result"]):
        raise cluster.ClusterError("independent cluster replay differs from terminal")
    return result
