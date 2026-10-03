"""No-launch N31 all-live adaptive-v3 materialization for prospective W18.

It writes only deterministic configuration and a hash manifest.  It neither
starts a manager/replica nor writes any event, authorization, or result.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from typing import Any, Mapping, Sequence

from .operator_capacity_stage_a_preflight import REQUIRED_BINARIES
from . import operator_capacity_v3_cluster_timing as timing


class OperatorCapacityV3MaterializerError(ValueError):
    pass


N = 31
Q = 21
TREE_COUNT = 21
SLOW_ROOTS = tuple(range(6))
_HEX = frozenset("0123456789abcdef")
_ARMS = {"sham": "exact_copy_sham", "treatment": "fast_priority_treatment"}
_STAGE_A_RECEIPT_KIND = "kauri-operator-capacity-native-envelope-verification-receipt-v1"
_STAGE_A_RECEIPT_VERDICT = "NATIVE_ENVELOPE_VERIFIED_NO_EXECUTION"
_STAGE_A_RECEIPT_KEYS = frozenset({
    "schema_version", "kind", "verdict", "envelope_wire_sha256",
    "envelope_canonical_digest", "approved_capacity_digest", "issuer_id",
    "issuer_reference", "issuer_public_key_fingerprint", "arm",
    "source_revision", "verification_monotonic_raw_ns",
    "epoch0_tree_file_sha256", "epoch0_consensus_digest",
    "epoch0_topology_digest",
})
_IDENTITY_PARITY_RECEIPT_KIND = "kauri-operator-capacity-native-identity-parity-receipt-v1"
_IDENTITY_PARITY_RECEIPT_VERDICT = "NATIVE_IDENTITY_PARITY_VERIFIED_NO_EXECUTION"
_IDENTITY_PARITY_RECEIPT_KEYS = frozenset({
    "schema_version", "kind", "verdict", "source_revision",
    "identity_bundle_sha256", "public_identity_fingerprint",
    "bls_replicas", "tls_identities",
})
_IDENTITY_APPROVAL_KIND = "kauri-n31-operator-capacity-tool-identity-approval-v1"
_SYNTHETIC_WORKLOAD = "replica-local-synthetic-v1"
_POST_E1_WINDOW_NS = 30 * 1_000_000_000


def _fail(message: str) -> None:
    raise OperatorCapacityV3MaterializerError(message)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _hex(value: object, label: str, *, length: int | None = None) -> str:
    if not isinstance(value, str) or not value or (length is not None and len(value) != length) or any(c not in _HEX for c in value):
        _fail(f"{label} is not lower-case hexadecimal")
    return value


def _write_new(path: Path, raw: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    except OSError as exc:
        _fail(f"cannot create fresh {path.name}")
        raise AssertionError from exc
    try:
        position = 0
        while position < len(raw):
            written = os.write(fd, raw[position:])
            if written <= 0:
                _fail(f"cannot write complete {path.name}")
            position += written
        os.fsync(fd)
    finally:
        os.close(fd)


def _regular(path: Path, label: str, *, maximum_bytes: int | None = None) -> bytes:
    try:
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(path, flags)
    except OSError as exc:
        _fail(f"cannot read {label}")
        raise AssertionError from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            _fail(f"{label} is not a regular non-symlink file")
        if maximum_bytes is not None and info.st_size > maximum_bytes:
            _fail(f"{label} exceeds its native parser byte limit")
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                _fail(f"{label} changed during read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            _fail(f"{label} changed during read")
        return b"".join(chunks)
    finally:
        os.close(fd)


def canonical_e0_tree() -> bytes:
    """Return 21 canonical fanout-five E0 trees rooted only at IDs 0..5."""
    rows: list[str] = []
    for tree_id in range(TREE_COUNT):
        root = SLOW_ROOTS[tree_id % len(SLOW_ROOTS)]
        order = (root, *(replica for replica in range(N) if replica != root))
        rows.append("fan:5 pipe:2 " + " ".join(map(str, order)))
    return ("\n".join(rows) + "\n").encode("ascii")


def _e0_tree(value: Mapping[str, object]) -> tuple[bytes, dict[str, str]]:
    if set(value) != {"path", "sha256", "topology_digest"}:
        _fail("Epoch-0 tree binding schema differs")
    path = Path(str(value["path"]))
    raw = _regular(path, "preflight-approved Epoch-0 tree", maximum_bytes=8 * 1024)
    digest = _hex(value["sha256"], "Epoch-0 tree SHA-256", length=64)
    if _sha(raw) != digest:
        _fail("Epoch-0 tree bytes differ from caller-pinned SHA-256")
    try:
        rows = raw.decode("ascii").splitlines()
    except UnicodeDecodeError:
        _fail("Epoch-0 tree is not ASCII")
    if len(rows) != TREE_COUNT:
        _fail("Epoch-0 tree does not contain exactly 21 trees")
    for tree_id, row in enumerate(rows):
        fields = row.split()
        if len(fields) != N + 2 or fields[:2] != ["fan:5", "pipe:2"]:
            _fail("Epoch-0 tree row does not use the frozen fanout/pipeline shape")
        try:
            members = [int(field, 10) for field in fields[2:]]
        except ValueError:
            _fail("Epoch-0 tree contains a non-integer replica ID")
        if set(members) != set(range(N)) or len(set(members)) != N:
            _fail("Epoch-0 tree row is not an N31 permutation")
        if members[0] != SLOW_ROOTS[tree_id % len(SLOW_ROOTS)]:
            _fail("Epoch-0 tree does not retain the frozen slow-root schedule")
    result = {"path": str(path.resolve()), "sha256": digest}
    topology_digest = _hex(value["topology_digest"], "Epoch-0 topology digest", length=64)
    if topology_digest == "0" * 64:
        _fail("Epoch-0 topology digest must not be zero")
    result["topology_digest"] = topology_digest
    return raw, result


def _identities(value: Mapping[str, object]) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, str]]:
    if set(value) != {"bls", "tls", "issuer"}:
        _fail("identity map schema differs")
    bls, tls, issuer = value["bls"], value["tls"], value["issuer"]
    if not isinstance(bls, list) or len(bls) != N or not isinstance(tls, list) or len(tls) != N + 1 or not isinstance(issuer, dict):
        _fail("identity map does not cover 31 replicas plus manager TLS")
    def key_row(row: object, keys: set[str], label: str) -> dict[str, str]:
        if not isinstance(row, dict) or set(row) != keys:
            _fail(f"{label} identity schema differs")
        return {key: _hex(row[key], f"{label} {key}") for key in keys}
    bls_rows = [key_row(row, {"pub", "sec"}, f"BLS {index}") for index, row in enumerate(bls)]
    tls_rows = [key_row(row, {"crt", "sec", "cid"}, f"TLS {index}") for index, row in enumerate(tls)]
    issuer_row = key_row(issuer, {"pub", "sec"}, "issuer")
    if len({row["pub"] for row in bls_rows}) != N or len({row["cid"] for row in tls_rows}) != N + 1:
        _fail("identity public values are not unique")
    return bls_rows, tls_rows, issuer_row


def _identity_bundle(bls: Sequence[Mapping[str, str]], tls: Sequence[Mapping[str, str]], issuer: Mapping[str, str]) -> bytes:
    rows = [f"bls:{index}:{row['pub']}:{row['sec']}" for index, row in enumerate(bls)]
    rows.extend(f"tls:{index}:{row['crt']}:{row['sec']}:{row['cid']}" for index, row in enumerate(tls))
    rows.append(f"issuer:{issuer['pub']}:{issuer['sec']}")
    return ("\n".join(rows) + "\n").encode("ascii")


def _identity_public_fingerprint(bls: Sequence[Mapping[str, str]], tls: Sequence[Mapping[str, str]], issuer: Mapping[str, str]) -> str:
    rows = ["kauri-operator-capacity-identity-public-v1"]
    rows.extend(f"bls:{index}:{row['pub']}" for index, row in enumerate(bls))
    rows.extend(f"tls:{index}:{row['crt']}:{row['cid']}" for index, row in enumerate(tls))
    rows.append(f"issuer:{issuer['pub']}")
    return _sha(("\n".join(rows) + "\n").encode("ascii"))


def _materialized_public_identity_fingerprint(
    bls: Sequence[Mapping[str, str]], tls: Sequence[Mapping[str, str]], issuer: Mapping[str, str],
) -> str:
    """Fingerprint only public fields present in the materialized runtime seam.

    The native parity receipt also covers the manager TLS common name.  That
    field has no native configuration or manager-argv consumer, so it remains
    receipt-bound rather than being misrepresented as an active binding.
    """
    rows = ["kauri-operator-capacity-materialized-identity-public-v1"]
    rows.extend(f"bls:{index}:{row['pub']}" for index, row in enumerate(bls))
    rows.extend(f"tls:{index}:{row['crt']}:{row['cid']}" for index, row in enumerate(tls[:-1]))
    rows.append(f"manager-tls:{tls[-1]['crt']}")
    rows.append(f"issuer:{issuer['pub']}")
    return _sha(("\n".join(rows) + "\n").encode("ascii"))


def _identity_public_projection(
    bls: Sequence[Mapping[str, str]], tls: Sequence[Mapping[str, str]], issuer: Mapping[str, str],
) -> bytes:
    """Persist the public fields needed to reconstruct the native receipt.

    This is not a new authority: the archived native parity receipt below is
    the independent expected fingerprint.  The projection only makes its
    public, indexed inputs available to the later no-launch audit.
    """
    return _canonical_json({
        "schema_version": 1,
        "kind": "kauri-operator-capacity-materialized-public-identity-v1",
        "bls_public_keys": [row["pub"] for row in bls],
        "replica_tls": [{"certificate": row["crt"], "common_name": row["cid"]} for row in tls[:-1]],
        "manager_tls": {"certificate": tls[-1]["crt"], "common_name": tls[-1]["cid"]},
        "issuer_public_key": issuer["pub"],
    })


def _identity_parity_receipt(value: Mapping[str, object], *, bls: Sequence[Mapping[str, str]],
                             tls: Sequence[Mapping[str, str]], issuer: Mapping[str, str],
                             source_revision: str, verifier_binary: Path,
                             verifier_sha256: str) -> tuple[str, bytes]:
    if set(value) != {"path", "sha256"}:
        _fail("native identity-parity receipt binding schema differs")
    path = Path(str(value["path"]))
    expected_sha = _hex(value["sha256"], "native identity-parity receipt caller-pinned SHA-256", length=64)
    raw = _regular(path, "native identity-parity receipt", maximum_bytes=16 * 1024)
    if _sha(raw) != expected_sha:
        _fail("native identity-parity receipt differs from caller-pinned SHA-256")
    receipt = _strict_json_object(raw, "native identity-parity receipt")
    if set(receipt) != _IDENTITY_PARITY_RECEIPT_KEYS:
        _fail("native identity-parity receipt schema differs")
    if (receipt["schema_version"] != 1 or receipt["kind"] != _IDENTITY_PARITY_RECEIPT_KIND or
            receipt["verdict"] != _IDENTITY_PARITY_RECEIPT_VERDICT or
            receipt["source_revision"] != source_revision or receipt["bls_replicas"] != N or
            receipt["tls_identities"] != N + 1):
        _fail("native identity-parity receipt does not bind this materialization")
    expected_bundle = _sha(_identity_bundle(bls, tls, issuer))
    expected_public = _identity_public_fingerprint(bls, tls, issuer)
    if (_hex(receipt["identity_bundle_sha256"], "native identity bundle SHA-256", length=64) != expected_bundle or
            _hex(receipt["public_identity_fingerprint"], "native public identity fingerprint", length=64) != expected_public):
        _fail("native identity-parity receipt does not match exact identity inputs")
    # A caller-authored JSON receipt is not evidence that the native key-pair
    # checks ran. Re-run the externally pinned verifier over the exact bytes.
    with tempfile.TemporaryDirectory(prefix="kauri-identity-parity-") as directory:
        private = Path(directory) / "identities.bundle"
        verified = Path(directory) / "verified.json"
        _write_new(private, _identity_bundle(bls, tls, issuer))
        command = (str(verifier_binary), "--identity-bundle", str(private),
                   "--source-revision", source_revision,
                   "--expected-public-fingerprint", expected_public,
                   "--output", str(verified))
        try:
            invoked = subprocess.run(command, capture_output=True, check=False, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            _fail("pinned native identity-parity verifier could not complete")
            raise AssertionError from exc
        if invoked.returncode != 0:
            _fail("pinned native identity-parity verifier rejected exact identities")
        observed = _regular(verified, "fresh native identity-parity receipt", maximum_bytes=16 * 1024)
        if _sha(observed) != expected_sha or observed != raw:
            _fail("native identity-parity receipt differs from fresh verifier output")
    if _sha(_regular(verifier_binary, "pinned native identity-parity verifier", maximum_bytes=512 * 1024 * 1024)) != verifier_sha256:
        _fail("native identity-parity verifier changed during verification")
    return expected_sha, raw


def _identity_verifier_approval(value: Mapping[str, object], *, verifier_binary: Path,
                                manager_binary: Path, app_binary: Path,
                                source_revision: str) -> tuple[str, str]:
    if set(value) != {"path", "sha256"}:
        _fail("external tool-identity approval binding schema differs")
    expected_sha = _hex(value["sha256"], "external tool-identity approval SHA-256", length=64)
    raw = _regular(Path(str(value["path"])), "external tool-identity approval", maximum_bytes=64 * 1024)
    if _sha(raw) != expected_sha:
        _fail("external tool-identity approval differs from caller pin")
    document = _strict_json_object(raw, "external tool-identity approval")
    binaries = document.get("binary_sha256")
    if (document.get("schema_version") != 1 or document.get("kind") != _IDENTITY_APPROVAL_KIND or
            document.get("verdict") != "EXTERNAL_TOOL_IDENTITY_APPROVED" or
            document.get("revision") != source_revision or
            not isinstance(document.get("approval_ref"), str) or not document["approval_ref"] or
            not isinstance(document.get("approved_at_utc"), str) or not document["approved_at_utc"] or
            not isinstance(binaries, dict) or set(binaries) != REQUIRED_BINARIES):
        _fail("external tool-identity approval does not bind the native verifier")
    verifier_sha = _hex(binaries["identity_parity_verifier"], "approved identity verifier SHA-256", length=64)
    manager_sha = _hex(binaries["adaptation_manager"], "approved adaptation manager SHA-256", length=64)
    app_sha = _hex(binaries["hotstuff_app"], "approved hotstuff app SHA-256", length=64)
    if _sha(_regular(verifier_binary, "approved native identity-parity verifier", maximum_bytes=512 * 1024 * 1024)) != verifier_sha:
        _fail("native identity-parity verifier differs from external approval")
    if _sha(_regular(manager_binary, "approved adaptation manager", maximum_bytes=512 * 1024 * 1024)) != manager_sha:
        _fail("adaptation manager differs from external approval")
    if _sha(_regular(app_binary, "approved hotstuff app", maximum_bytes=512 * 1024 * 1024)) != app_sha:
        _fail("hotstuff app differs from external approval")
    return expected_sha, verifier_sha


def _stage_a(value: Mapping[str, object]) -> dict[str, str]:
    keys = {
        "envelope_path", "envelope_sha256", "label_issuer_id", "label_issuer_reference",
        "label_issuer_public_key_hex", "label_issuer_public_key_fingerprint", "approved_capacity_digest",
    }
    if set(value) != keys:
        _fail("Stage-A input schema differs")
    envelope = Path(str(value["envelope_path"]))
    raw = _regular(envelope, "Stage-A envelope", maximum_bytes=32 * 1024)
    digest = _hex(value["envelope_sha256"], "Stage-A wire SHA-256", length=64)
    if _sha(raw) != digest:
        _fail("Stage-A envelope bytes differ from pinned SHA-256")
    issuer_id = value["label_issuer_id"]
    if type(issuer_id) is not int or issuer_id <= 0:
        _fail("Stage-A label issuer ID is invalid")
    reference = value["label_issuer_reference"]
    if not isinstance(reference, str) or not reference or len(reference) > 128:
        _fail("Stage-A label issuer reference is invalid")
    return {
        "envelope_path": str(envelope.resolve()), "envelope_sha256": digest,
        "label_issuer_id": str(issuer_id), "label_issuer_reference": reference,
        "label_issuer_public_key_hex": _hex(value["label_issuer_public_key_hex"], "Stage-A public key", length=66),
        "label_issuer_public_key_fingerprint": _hex(value["label_issuer_public_key_fingerprint"], "Stage-A fingerprint", length=64),
        "approved_capacity_digest": _hex(value["approved_capacity_digest"], "Stage-A capacity digest", length=64),
    }


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"


def _strict_json_object(raw: bytes, label: str) -> dict[str, object]:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                _fail(f"{label} contains a duplicate JSON field")
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"{label} is not strict ASCII JSON")
        raise AssertionError from exc
    if not isinstance(value, dict):
        _fail(f"{label} is not a JSON object")
    return value


def _stage_a_receipt(value: Mapping[str, object], *, stage: Mapping[str, str], tree: Mapping[str, str],
                     arm: str, source_revision: str) -> str:
    if set(value) != {"path", "sha256"}:
        _fail("Stage-A native receipt binding schema differs")
    path = Path(str(value["path"]))
    expected_sha = _hex(value["sha256"], "Stage-A native receipt caller-pinned SHA-256", length=64)
    raw = _regular(path, "Stage-A native verifier receipt", maximum_bytes=16 * 1024)
    if _sha(raw) != expected_sha:
        _fail("Stage-A native verifier receipt differs from caller-pinned SHA-256")
    receipt = _strict_json_object(raw, "Stage-A native verifier receipt")
    if set(receipt) != _STAGE_A_RECEIPT_KEYS:
        _fail("Stage-A native verifier receipt schema differs")
    if (receipt["schema_version"] != 1 or receipt["kind"] != _STAGE_A_RECEIPT_KIND or
            receipt["verdict"] != _STAGE_A_RECEIPT_VERDICT):
        _fail("Stage-A native verifier receipt is not an accepted no-execution receipt")
    if type(receipt["issuer_id"]) is not int or receipt["issuer_id"] <= 0:
        _fail("Stage-A native verifier receipt issuer ID is invalid")
    if type(receipt["verification_monotonic_raw_ns"]) is not int or receipt["verification_monotonic_raw_ns"] < 0:
        _fail("Stage-A native verifier receipt timestamp is invalid")
    hex_fields = {
        "envelope_wire_sha256", "envelope_canonical_digest", "approved_capacity_digest",
        "issuer_public_key_fingerprint", "epoch0_tree_file_sha256",
        "epoch0_consensus_digest", "epoch0_topology_digest",
    }
    for field in hex_fields:
        _hex(receipt[field], f"Stage-A native verifier receipt {field}", length=64)
    expected = {
        "envelope_wire_sha256": stage["envelope_sha256"],
        "approved_capacity_digest": stage["approved_capacity_digest"],
        "issuer_id": int(stage["label_issuer_id"]),
        "issuer_reference": stage["label_issuer_reference"],
        "issuer_public_key_fingerprint": stage["label_issuer_public_key_fingerprint"],
        "arm": _ARMS[arm], "source_revision": source_revision,
        "epoch0_tree_file_sha256": tree["sha256"],
        "epoch0_topology_digest": tree["topology_digest"],
    }
    if any(receipt[field] != expected_value for field, expected_value in expected.items()):
        _fail("Stage-A native verifier receipt does not bind materialization inputs")
    return expected_sha


def materialize_operator_capacity_v3(
    root: Path, *, arm: str, run_id: str, source_instance: str, ports: Mapping[str, int],
    binaries: Mapping[str, Path], identities: Mapping[str, object], identity_parity_receipt: Mapping[str, object],
    tool_identity_approval: Mapping[str, object], epoch0_tree: Mapping[str, object], stage_a: Mapping[str, object],
    stage_a_verifier_receipt: Mapping[str, object], source_revision: str,
    stage_b_issuer_reference: str, hard_deadline_ns: int,
    cluster_timing_profile: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Write a fresh v3 all-live input set and return actual argv only in memory."""
    root = Path(root)
    aggregation_seconds = timing.aggregation_seconds(cluster_timing_profile)
    if root.exists() or root.is_symlink() or arm not in _ARMS:
        _fail("materialization root must be fresh and arm predeclared")
    if (not isinstance(run_id, str) or not run_id or len(run_id) > 128 or
            not isinstance(source_instance, str) or not source_instance or len(source_instance) > 128 or
            not isinstance(stage_b_issuer_reference, str) or not stage_b_issuer_reference or len(stage_b_issuer_reference) > 128 or
            type(hard_deadline_ns) is not int or hard_deadline_ns <= 0):
        _fail("run identity, Stage-B reference, or hard deadline is invalid")
    source_revision = _hex(source_revision, "source revision", length=40)
    if set(ports) != {"peer_base", "client_base", "manager"} or any(type(value) is not int or value <= 0 or value > 65535 for value in ports.values()):
        _fail("port mapping schema differs")
    if ports["peer_base"] + N - 1 > 65535 or ports["client_base"] + N - 1 > 65535:
        _fail("replica port range exceeds uint16")
    peer_ports = set(range(ports["peer_base"], ports["peer_base"] + N))
    client_ports = set(range(ports["client_base"], ports["client_base"] + N))
    if peer_ports.intersection(client_ports) or ports["manager"] in peer_ports | client_ports:
        _fail("manager, peer, and client port assignments overlap")
    if set(binaries) != {"adaptation_manager", "hotstuff_app", "identity_parity_verifier"}:
        _fail("binary map schema differs")
    manager_binary = Path(binaries["adaptation_manager"]); app_binary = Path(binaries["hotstuff_app"])
    verifier_binary = Path(binaries["identity_parity_verifier"])
    manager_sha = _sha(_regular(manager_binary, "adaptation manager")); app_sha = _sha(_regular(app_binary, "hotstuff app"))
    approval_sha, verifier_sha = _identity_verifier_approval(
        tool_identity_approval, verifier_binary=verifier_binary,
        manager_binary=manager_binary, app_binary=app_binary,
        source_revision=source_revision)
    bls, tls, issuer = _identities(identities); stage = _stage_a(stage_a)
    identity_receipt_sha, identity_receipt_raw = _identity_parity_receipt(
        identity_parity_receipt, bls=bls, tls=tls, issuer=issuer,
        source_revision=source_revision, verifier_binary=verifier_binary,
        verifier_sha256=verifier_sha)
    tree_raw, tree_binding = _e0_tree(epoch0_tree)
    stage_a_receipt_sha = _stage_a_receipt(
        stage_a_verifier_receipt, stage=stage, tree=tree_binding, arm=arm,
        source_revision=source_revision)

    root.mkdir(mode=0o700)
    for directory in (
        root / "config",
        root / "raw",
        root / "transitions",
        root / "transitions/e0-to-e1-operator-capacity",
    ):
        directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    tree = root / "config/epoch0.tree"; _write_new(tree, tree_raw)
    # The manager must consume an immutable materialized copy, never the
    # caller's mutable preflight location.  The digest is retained in the
    # manifest and rechecked at the runner's spawn edge.
    stage_envelope = root / "config/stage-a-envelope.wire"
    _write_new(stage_envelope, _regular(Path(stage["envelope_path"]), "Stage-A envelope", maximum_bytes=32 * 1024))
    identity_receipt = root / "config/identity-parity-receipt.json"
    identity_projection = root / "config/identity-public-projection.json"
    _write_new(identity_receipt, identity_receipt_raw, mode=0o644)
    _write_new(identity_projection, _identity_public_projection(bls, tls, issuer), mode=0o644)
    main = root / "config/hotstuff.gen.conf"
    main_lines = [
        "block-size = 1", "fan-out = 5", "async_blocks = 2", "piped_latency = 1",
        "nworker = 2", "repnworker = 2", "pace-maker = dummy", "proposer = 0",
        "base-timeout = 2.0", "prop-delay = 0.1", f"aggregation-timeout = {aggregation_seconds:.1f}",
        f"leader-progress-timeout = {timing.leader_progress_seconds(cluster_timing_profile):.1f}",
        "leader-activation-grace = 1.0",
        "client-ip = 127.0.0.1", "tree-generation = file", f"tree-generation-fpath = {tree}",
        "tree-switch-period = 2", "epoch-protocol-mode = adaptive_v3",
        "experiment-exact-timeout-attempt-evidence-v3 = true",
        "epoch-change-issuer-id = 1", f"epoch-change-issuer-public-key = {issuer['pub']}",
        "epoch-change-minimum-activation-delay = 5", "epoch-change-maximum-activation-delay = 5",
        "epoch-change-maximum-block-extra-bytes = 4096", "epoch-change-maximum-ancestry-blocks = 128",
        f"epoch-manager-address = 127.0.0.1:{ports['manager']}", f"epoch-manager-tls-cert = {tls[N]['crt']}",
        "max-rep-msg = 4194304",
    ]
    for replica in range(N):
        main_lines.append(f"replica = 127.0.0.1:{ports['peer_base'] + replica};{ports['client_base'] + replica}, {bls[replica]['pub']}, {tls[replica]['cid']}")
        main_lines.append(f"activation-readiness-member = {replica},{bls[replica]['pub']}")
    _write_new(main, ("\n".join(main_lines) + "\n").encode("ascii"))
    replica_paths: list[Path] = []
    for replica in range(N):
        path = root / f"config/replica-{replica}.conf"
        _write_new(path, (f"privkey = {bls[replica]['sec']}\ntls-privkey = {tls[replica]['sec']}\ntls-cert = {tls[replica]['crt']}\nidx = {replica}\n").encode("ascii"))
        replica_paths.append(path)
    transition = _canonical_json({
        "policy_intent": "performance_optimization", "evidence_window_rule": "fresh_exact_predecessor_after_common_commit",
        "transition_artifact_id": "e0-to-e1-operator-capacity", "bundle_path": "transitions/e0-to-e1-operator-capacity/successor.bundle",
        "evidence_snapshot_path": "transitions/e0-to-e1-operator-capacity/evidence-snapshot.json",
        "predecessor_epoch_number": 0, "successor_epoch_number": 1, "minimum_predecessor_residency_ms": 0,
        "minimum_post_baseline_observation_ms": 0, "apply_shape_selection": False, "policy_parameters": {},
    }).decode("ascii").strip()
    stage_b = root / "raw/stage-b-authorization.wire"; consumption = root / "raw/consumption.json"; bundle = root / "transitions/e0-to-e1-operator-capacity/successor.bundle"
    manager_events = root / "raw/manager-events.jsonl"
    verifier_arguments = [
        "--epoch0-tree-file", str(tree), "--stage-a-envelope-wire", str(stage_envelope),
        "--issuer-id", stage["label_issuer_id"], "--issuer-reference", stage["label_issuer_reference"],
        "--issuer-public-key-hex", stage["label_issuer_public_key_hex"],
        "--issuer-public-key-fingerprint", stage["label_issuer_public_key_fingerprint"],
        "--approved-capacity-digest", stage["approved_capacity_digest"],
        "--arm", _ARMS[arm], "--source-revision", source_revision,
    ]
    manager_argv = [str(manager_binary), "--protocol-mode", "adaptive_v3", "--listen", f"127.0.0.1:{ports['manager']}",
                    "--tls-privkey", tls[N]["sec"], "--tls-cert", tls[N]["crt"], "--issuer-id", "1", "--issuer-private-key", issuer["sec"],
                    "--activation-delay-blocks", "5", "--convergence-deadline-seconds", str(timing.convergence_seconds(cluster_timing_profile)), "--tree-fanout", "5", "--pipeline-stretch", "2",
                    "--shape-candidate-fanouts", "5", "--shape-deterministic-seed", "1", "--transition-request", transition,
                    "--bundle-output", str(bundle), "--epoch-zero-tree-file", str(tree), "--structured-event-run-id", run_id,
                    "--structured-event-source-instance", source_instance, "--structured-event-output", str(manager_events),
                    "--activation-readiness-release-count", str(N), "--activation-readiness-maximum-delivery-attempts", "1", "--activation-readiness-retry-interval-ticks", "30000",
                    "--operator-capacity-stage-a-envelope", str(stage_envelope), "--operator-capacity-stage-a-wire-sha256", stage["envelope_sha256"],
                    "--operator-capacity-label-issuer-id", stage["label_issuer_id"], "--operator-capacity-label-issuer-reference", stage["label_issuer_reference"],
                    "--operator-capacity-label-issuer-public-key-hex", stage["label_issuer_public_key_hex"], "--operator-capacity-label-issuer-public-key-fingerprint", stage["label_issuer_public_key_fingerprint"],
                    "--operator-capacity-approved-capacity-digest", stage["approved_capacity_digest"], "--operator-capacity-hard-deadline-ns", str(hard_deadline_ns),
                    "--operator-capacity-stage-b-authorization-output", str(stage_b), "--operator-capacity-consumption-output", str(consumption),
                    "--operator-capacity-stage-b-issuer-reference", stage_b_issuer_reference]
    for replica in range(N):
        manager_argv.extend(("--replica", f"{replica},127.0.0.1:{ports['peer_base'] + replica},{tls[replica]['crt']}", "--activation-readiness-member", f"{replica},{bls[replica]['pub']}"))
    observer_instance = f"{source_instance}-replica-0"
    replica_argv = [[str(app_binary), "--conf", str(main), "--conf", str(path), "--structured-event-run-id", run_id,
                     "--structured-event-source-instance", f"{source_instance}-replica-{replica}", "--structured-event-output", str(root / f"raw/replica-{replica}.jsonl"),
                     "--structured-event-commit-observer-id", "replica-0", "--structured-event-commit-observer-instance", observer_instance] for replica, path in enumerate(replica_paths)]
    artifacts = {str(path.relative_to(root)): _sha(_regular(path, str(path))) for path in (tree, stage_envelope, identity_receipt, identity_projection, main, *replica_paths)}
    synthetic_workload = {"kind": _SYNTHETIC_WORKLOAD, "source_revision": source_revision,
                          "hotstuff_app_sha256": app_sha,
                          "main_config_sha256": artifacts["config/hotstuff.gen.conf"],
                          "block_size": 1, "initial_beat_delay_ms": 10_000,
                          "beat_interval_ms": 50, "transaction_count_per_block": 1,
                          "post_e1_window_ns": _POST_E1_WINDOW_NS}
    manifest = {"schema_version": 1, "kind": "kauri-n31-operator-capacity-v3-materialization-v1", "verdict": "MATERIALIZED_NO_EXECUTION", "claim_eligible": False, "figure_eligible": False,
                "arm": arm, "stage_a_native_arm": _ARMS[arm], "protocol": {"N": N, "Q": Q, "tree_count": TREE_COUNT}, "slow_root_ids": list(SLOW_ROOTS),
                "revision": source_revision, "epoch0_tree": {"sha256": _sha(tree_raw), "topology_digest": tree_binding["topology_digest"]},
                "binary_sha256": {"adaptation_manager": manager_sha, "hotstuff_app": app_sha,
                                  "identity_parity_verifier": verifier_sha}, "artifact_sha256": artifacts,
                "manager_argv_sha256": _sha(_canonical_json(manager_argv)), "replica_argv_sha256": [_sha(_canonical_json(argv)) for argv in replica_argv],
                "synthetic_workload": synthetic_workload,
                "stage_a_envelope_sha256": stage["envelope_sha256"], "stage_a_verifier_receipt_sha256": stage_a_receipt_sha,
                "stage_a_verifier_arguments": verifier_arguments,
                "identity_parity_receipt_sha256": identity_receipt_sha,
                # This public projection is later recomputed from the exact
                # materialized config files and argv.  It deliberately says
                # nothing about whether the native parser consumed private
                # key bytes; that needs native loaded-config evidence.
                "public_identity_fingerprint": _identity_public_fingerprint(bls, tls, issuer),
                "tool_identity_approval_receipt_sha256": approval_sha,
                "stage_b_authorization_output": str(stage_b.relative_to(root)), "consumption_output": str(consumption.relative_to(root)), "bundle_output": str(bundle.relative_to(root))}
    if cluster_timing_profile is not None:
        manifest["cluster_timing_profile"] = dict(cluster_timing_profile)
    _write_new(root / "materialization-manifest.json", _canonical_json(manifest), mode=0o644)
    return {"manifest": manifest, "manager_argv": tuple(manager_argv), "replica_argv": tuple(tuple(argv) for argv in replica_argv)}
