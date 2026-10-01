"""No-launch backend plan for a prospective W18 all-live N31 arm.

This is intentionally a process *materializer/auditor*, not a launcher.  It
only accepts an immutable materializer output and makes all future process,
quota, Stage-B, and cleanup obligations explicit.  A caller cannot turn its
result into evidence or a live run through this module.
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

from .operator_capacity_preflight import _validate_quota_profile_bytes


N = 31
Q = 21
_HEX = frozenset("0123456789abcdef")
_ARMS = {"sham": "exact_copy_sham", "treatment": "fast_priority_treatment"}
_MANIFEST_KIND = "kauri-n31-operator-capacity-v3-materialization-v1"
_IDENTITY_RECEIPT_KIND = "kauri-operator-capacity-native-identity-parity-receipt-v1"
_IDENTITY_RECEIPT_FIELDS = {
    "schema_version", "kind", "verdict", "source_revision", "identity_bundle_sha256",
    "public_identity_fingerprint", "bls_replicas", "tls_identities",
}
_IDENTITY_PROJECTION_FIELDS = {
    "schema_version", "kind", "bls_public_keys", "replica_tls", "manager_tls", "issuer_public_key",
}
_FROZEN_TRANSITION_REQUEST = {
    "policy_intent": "performance_optimization",
    "evidence_window_rule": "fresh_exact_predecessor_after_common_commit",
    "transition_artifact_id": "e0-to-e1-operator-capacity",
    "bundle_path": "transitions/e0-to-e1-operator-capacity/successor.bundle",
    "evidence_snapshot_path": "transitions/e0-to-e1-operator-capacity/evidence-snapshot.json",
    "predecessor_epoch_number": 0,
    "successor_epoch_number": 1,
    "minimum_predecessor_residency_ms": 0,
    "minimum_post_baseline_observation_ms": 0,
    "apply_shape_selection": False,
    "policy_parameters": {},
}
_SYNTHETIC_CONFIG = {
    "block-size": "1", "fan-out": "5", "async_blocks": "2", "piped_latency": "1",
    "nworker": "2", "repnworker": "2", "pace-maker": "dummy", "proposer": "0",
    "base-timeout": "2.0", "prop-delay": "0.1", "aggregation-timeout": "0.5",
    "leader-progress-timeout": "5.0", "leader-activation-grace": "1.0",
    "client-ip": "127.0.0.1", "tree-generation": "file", "tree-switch-period": "2",
    "epoch-protocol-mode": "adaptive_v3", "epoch-change-issuer-id": "1",
    "experiment-exact-timeout-attempt-evidence-v3": "true",
    "epoch-change-minimum-activation-delay": "5", "epoch-change-maximum-activation-delay": "5",
    "epoch-change-maximum-block-extra-bytes": "4096", "epoch-change-maximum-ancestry-blocks": "128",
    "max-rep-msg": "4194304",
}


class OperatorCapacityV3BackendError(RuntimeError):
    pass


def _fail(message: str) -> None:
    raise OperatorCapacityV3BackendError(message)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _validate_readiness_rows(replicas: Sequence[str], readiness: Sequence[str],
                             manager_argv: Sequence[str]) -> None:
    try:
        expected = [f"{replica},{row.split(',')[1].strip()}"
                    for replica, row in enumerate(replicas)]
    except IndexError as exc:
        _fail("materialized replica public-key row is malformed")
        raise AssertionError from exc
    manager_members = [manager_argv[index + 1] for index in range(1, len(manager_argv), 2)
                       if manager_argv[index] == "--activation-readiness-member"]
    if len(expected) != N or list(readiness) != expected or manager_members != expected:
        _fail("materialized adaptive-v3 readiness membership differs from exact N31 keys")


def validate_synthetic_main_config(raw: bytes, *, root: Path, manager_argv: Sequence[str]) -> dict[str, str]:
    """Parse singleton options as Salticidae does: trim around ``=``.

    ``replica`` and ``activation-readiness-member`` are indexed repeatable
    options. Other repeated keys cannot override a frozen value.
    """
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        _fail("materialized main config is not ASCII")
        raise AssertionError from exc
    parsed: dict[str, str] = {}
    replicas: list[str] = []
    readiness: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        if "=" not in line:
            _fail("materialized main config line lacks assignment")
        key, value = (part.strip() for part in line.split("=", 1))
        if not key or not value:
            _fail("materialized main config assignment is malformed")
        if key not in {"replica", "activation-readiness-member"} and key in parsed:
            _fail("materialized main config repeats a singleton key")
        if key == "replica":
            replicas.append(value)
        elif key == "activation-readiness-member":
            readiness.append(value)
        else:
            parsed[key] = value
    allowed = set(_SYNTHETIC_CONFIG) | {"tree-generation-fpath", "epoch-manager-address", "epoch-manager-tls-cert", "epoch-change-issuer-public-key"}
    if set(parsed) != allowed:
        _fail("materialized main config contains unpinned native options")
    if any(parsed.get(key) != value for key, value in _SYNTHETIC_CONFIG.items()):
        _fail("materialized main config does not pin the frozen synthetic cadence")
    if parsed.get("tree-generation-fpath") != str(Path(root).resolve() / "config/epoch0.tree"):
        _fail("materialized main config tree path is not the pinned E0 artifact")
    if (parsed.get("epoch-manager-address") != _value(manager_argv, "--listen") or
            parsed.get("epoch-manager-tls-cert") != _value(manager_argv, "--tls-cert")):
        _fail("materialized main config manager identity differs from manager argv")
    if len(replicas) != N or any(not row.startswith("127.0.0.1:") or ";" not in row for row in replicas):
        _fail("materialized main config does not contain exactly N31 replica rows")
    _validate_readiness_rows(replicas, readiness, manager_argv)
    # Row identity fields await the separate identity-projection gate.  Here
    # we bind row count/syntax and the per-replica config index only.
    return {key: parsed[key] for key in _SYNTHETIC_CONFIG}


def _replica_config(raw: bytes, replica: int) -> dict[str, str]:
    """Apply Salticidae's trimmed assignment semantics to one replica file."""
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        _fail("replica config is not ASCII")
        raise AssertionError from exc
    parsed: dict[str, str] = {}
    for line in lines:
        if not line.strip():
            continue
        if "=" not in line:
            _fail("replica config line lacks assignment")
        key, value = (part.strip() for part in line.split("=", 1))
        if not key or not value:
            _fail("replica config assignment is malformed")
        if key in parsed:
            _fail("replica config repeats a singleton key")
        parsed[key] = value
    if set(parsed) != {"privkey", "tls-privkey", "tls-cert", "idx"}:
        _fail("replica config schema differs from materializer output")
    if parsed["idx"] != str(replica):
        _fail("replica config does not bind its exact replica index")
    return parsed


def _validate_replica_config(raw: bytes, replica: int) -> None:
    _replica_config(raw, replica)


def _public_identity_fingerprint(
    bls: Sequence[str], tls: Sequence[tuple[str, str]], issuer_public_key: str,
) -> str:
    """Return the native parity verifier's public indexed projection."""
    rows = ["kauri-operator-capacity-identity-public-v1"]
    rows.extend(f"bls:{replica}:{public_key}" for replica, public_key in enumerate(bls))
    rows.extend(
        f"tls:{replica}:{certificate}:{common_name}"
        for replica, (certificate, common_name) in enumerate(tls)
    )
    rows.append(f"issuer:{issuer_public_key}")
    return _sha(("\n".join(rows) + "\n").encode("ascii"))


def _main_public_identity(raw: bytes, *, root: Path, manager_argv: Sequence[str]) -> tuple[list[str], list[str], str, str]:
    """Parse the public identity fields consumed from the shared config.

    The main config carries indexed BLS public keys, TLS common names, the
    manager TLS certificate, and the issuer public key.  The exact manager
    argv and the per-replica configs provide the remaining public links.
    """
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        _fail("materialized main config is not ASCII")
        raise AssertionError from exc
    parsed: dict[str, str] = {}
    replicas: list[str] = []
    readiness: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        if "=" not in line:
            _fail("materialized main config line lacks assignment")
        key, value = (part.strip() for part in line.split("=", 1))
        if not key or not value:
            _fail("materialized main config assignment is malformed")
        if key == "replica":
            replicas.append(value)
        elif key == "activation-readiness-member":
            readiness.append(value)
        elif key in parsed:
            _fail("materialized main config repeats a singleton key")
        else:
            parsed[key] = value
    if len(replicas) != N:
        _fail("materialized main config does not contain exactly N31 replica rows")
    _validate_readiness_rows(replicas, readiness, manager_argv)
    bls: list[str] = []
    common_names: list[str] = []
    for replica, row in enumerate(replicas):
        try:
            endpoint, public_key, common_name = (part.strip() for part in row.split(","))
        except ValueError:
            _fail("materialized main config has an invalid public replica identity")
        if not endpoint.startswith("127.0.0.1:") or ";" not in endpoint:
            _fail("materialized main config has an invalid replica endpoint")
        _hex_public(public_key, f"main config replica {replica} BLS public key")
        _hex_public(common_name, f"main config replica {replica} TLS common name")
        bls.append(public_key)
        common_names.append(common_name)
    manager_certificate = parsed.get("epoch-manager-tls-cert")
    issuer_public_key = parsed.get("epoch-change-issuer-public-key")
    if manager_certificate is None or issuer_public_key is None:
        _fail("materialized main config lacks public manager or issuer identity")
    _hex_public(manager_certificate, "main config manager TLS certificate")
    _hex_public(issuer_public_key, "main config issuer public key")
    return bls, common_names, manager_certificate, issuer_public_key


def validate_materialized_public_identity(
    *, root: Path, manager_argv: Sequence[str], receipt_sha256: str, expected_fingerprint: str,
    source_revision: str,
) -> str:
    """Cross-bind public identities in materialized config and manager argv.

    This validates only public values.  It cannot prove a replica's private
    key was accepted by the native parser, so callers must retain that boundary
    separately instead of promoting this projection to loaded-config proof.
    """
    receipt_sha256 = _hex(receipt_sha256, "native identity-parity receipt SHA-256")
    expected_fingerprint = _hex(expected_fingerprint, "public identity fingerprint")
    receipt_raw = _read_regular(root / "config/identity-parity-receipt.json", "archived native identity-parity receipt")
    if _sha(receipt_raw) != receipt_sha256:
        # The caller passes the receipt digest first; retain this branch only
        # to make a misuse fail closed before interpreting its JSON.
        _fail("archived native identity-parity receipt differs from manifest pin")
    receipt = _json(receipt_raw, "archived native identity-parity receipt")
    if (set(receipt) != _IDENTITY_RECEIPT_FIELDS or receipt.get("schema_version") != 1 or
            receipt.get("kind") != _IDENTITY_RECEIPT_KIND or
            receipt.get("verdict") != "NATIVE_IDENTITY_PARITY_VERIFIED_NO_EXECUTION" or
            receipt.get("source_revision") != source_revision or
            receipt.get("bls_replicas") != N or receipt.get("tls_identities") != N + 1):
        _fail("archived native identity-parity receipt schema differs")
    native_fingerprint = _hex(receipt.get("public_identity_fingerprint"), "native public identity fingerprint")
    projection = _json(_read_regular(root / "config/identity-public-projection.json", "public identity projection"),
                       "public identity projection")
    if set(projection) != _IDENTITY_PROJECTION_FIELDS or projection.get("schema_version") != 1 or projection.get("kind") != "kauri-operator-capacity-materialized-public-identity-v1":
        _fail("public identity projection schema differs")
    main_raw = _read_regular(root / "config/hotstuff.gen.conf", "materialized main config")
    bls, common_names, manager_certificate, issuer_public_key = _main_public_identity(
        main_raw, root=root, manager_argv=manager_argv)
    if _value(manager_argv, "--tls-cert") != manager_certificate:
        _fail("manager TLS certificate differs from materialized main config")
    repeated = [(manager_argv[index], manager_argv[index + 1]) for index in range(1, len(manager_argv), 2)]
    offset = len(_MANAGER_SINGLETON_FLAGS)
    tls: list[tuple[str, str]] = []
    private_bls: list[str] = []
    private_tls: list[str] = []
    for replica in range(N):
        replica_value = repeated[offset + 2 * replica][1]
        readiness_value = repeated[offset + 2 * replica + 1][1]
        replica_id, _endpoint, certificate = replica_value.split(",", 2)
        readiness_id, public_key = readiness_value.split(",", 1)
        if replica_id != str(replica) or readiness_id != str(replica):
            _fail("manager argv has an invalid indexed public identity")
        if public_key != bls[replica]:
            _fail("manager readiness public key differs from materialized main config")
        replica_config = _replica_config(
            _read_regular(root / f"config/replica-{replica}.conf", f"replica-{replica} config"), replica)
        if replica_config["tls-cert"] != certificate:
            _fail("replica TLS certificate differs from manager argv")
        tls.append((certificate, common_names[replica]))
        private_bls.append(replica_config["privkey"])
        private_tls.append(replica_config["tls-privkey"])
    if (projection.get("bls_public_keys") != bls or projection.get("issuer_public_key") != issuer_public_key or
            projection.get("replica_tls") != [
                {"certificate": certificate, "common_name": common_name}
                for certificate, common_name in tls
            ]):
        _fail("materialized public identity projection differs from active config or manager argv")
    manager_projection = projection.get("manager_tls")
    if (not isinstance(manager_projection, dict) or set(manager_projection) != {"certificate", "common_name"} or
            manager_projection["certificate"] != manager_certificate):
        _fail("materialized public identity projection differs from manager TLS identity")
    manager_common_name = _hex_public(manager_projection["common_name"], "projection manager TLS common name")
    fingerprint = _public_identity_fingerprint(
        bls, [*tls, (manager_certificate, manager_common_name)], issuer_public_key)
    if fingerprint != native_fingerprint or fingerprint != expected_fingerprint:
        _fail("materialized public identity projection differs from native parity receipt")
    identity_bundle = _identity_bundle_bytes(
        bls=bls, bls_secrets=private_bls, tls=[*tls, (manager_certificate, manager_common_name)],
        tls_secrets=[*private_tls, _value(manager_argv, "--tls-privkey")],
        issuer_public_key=issuer_public_key, issuer_secret=_value(manager_argv, "--issuer-private-key"))
    if _sha(identity_bundle) != _hex(receipt.get("identity_bundle_sha256"), "native identity bundle SHA-256"):
        _fail("materialized full identity bundle differs from native parity receipt")
    return fingerprint


def _identity_bundle_bytes(*, bls: Sequence[str], bls_secrets: Sequence[str],
                           tls: Sequence[tuple[str, str]], tls_secrets: Sequence[str],
                           issuer_public_key: str, issuer_secret: str) -> bytes:
    if len(bls) != N or len(bls_secrets) != N or len(tls) != N + 1 or len(tls_secrets) != N + 1:
        _fail("materialized full identity bundle does not cover N31 plus manager")
    rows = [f"bls:{index}:{public}:{secret}" for index, (public, secret) in enumerate(zip(bls, bls_secrets))]
    rows.extend(f"tls:{index}:{certificate}:{secret}:{common_name}" for index, ((certificate, common_name), secret) in enumerate(zip(tls, tls_secrets)))
    rows.append(f"issuer:{issuer_public_key}:{issuer_secret}")
    return ("\n".join(rows) + "\n").encode("ascii")


def rerun_materialized_native_identity_parity(
    *, root: Path, manager_argv: Sequence[str], receipt_sha256: str,
    expected_fingerprint: str, source_revision: str, verifier_binary: Path,
) -> str:
    """Re-run the externally approved native verifier over reconstructed bytes."""
    fingerprint = validate_materialized_public_identity(
        root=root, manager_argv=manager_argv, receipt_sha256=receipt_sha256,
        expected_fingerprint=expected_fingerprint, source_revision=source_revision)
    receipt_raw = _read_regular(root / "config/identity-parity-receipt.json", "archived native identity-parity receipt")
    bls, common_names, manager_certificate, issuer_public_key = _main_public_identity(
        _read_regular(root / "config/hotstuff.gen.conf", "materialized main config"), root=root, manager_argv=manager_argv)
    tls: list[tuple[str, str]] = []
    bls_secrets: list[str] = []
    tls_secrets: list[str] = []
    for replica in range(N):
        replica_value = manager_argv[1 + 2 * (len(_MANAGER_SINGLETON_FLAGS) + 2 * replica) + 1]
        certificate = replica_value.split(",", 2)[2]
        config = _replica_config(_read_regular(root / f"config/replica-{replica}.conf", f"replica-{replica} config"), replica)
        tls.append((certificate, common_names[replica])); bls_secrets.append(config["privkey"]); tls_secrets.append(config["tls-privkey"])
    projection = _json(_read_regular(root / "config/identity-public-projection.json", "public identity projection"), "public identity projection")
    manager = projection["manager_tls"]
    bundle = _identity_bundle_bytes(bls=bls, bls_secrets=bls_secrets,
        tls=[*tls, (manager_certificate, manager["common_name"])], tls_secrets=[*tls_secrets, _value(manager_argv, "--tls-privkey")],
        issuer_public_key=issuer_public_key, issuer_secret=_value(manager_argv, "--issuer-private-key"))
    with tempfile.TemporaryDirectory(prefix="kauri-w18-parity-") as directory:
        bundle_path, output_path = Path(directory) / "identities.bundle", Path(directory) / "receipt.json"
        bundle_path.write_bytes(bundle)
        try:
            invoked = subprocess.run((str(verifier_binary), "--identity-bundle", str(bundle_path), "--source-revision", source_revision,
                                      "--expected-public-fingerprint", fingerprint, "--output", str(output_path)), capture_output=True, check=False, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            _fail("approved native identity-parity verifier could not complete")
            raise AssertionError from exc
        if invoked.returncode != 0 or _read_regular(output_path, "fresh native identity-parity receipt") != receipt_raw:
            _fail("approved native identity-parity verifier differs from archived receipt")
    return fingerprint


def _validate_replica_argv(argv: Sequence[str], *, root: Path, replica: int) -> None:
    """Accept only the materializer's config-first replica command grammar."""
    flags = (
        "--conf", "--conf", "--structured-event-run-id",
        "--structured-event-source-instance", "--structured-event-output",
        "--structured-event-commit-observer-id",
        "--structured-event-commit-observer-instance",
    )
    if len(argv) != 15 or not argv[0] or tuple(argv[index] for index in range(1, 15, 2)) != flags:
        _fail("replica argv differs from the materializer command shape")
    if (Path(argv[2]).resolve(strict=False) != root / "config/hotstuff.gen.conf" or
            Path(argv[4]).resolve(strict=False) != root / "config" / f"replica-{replica}.conf"):
        _fail("replica argv does not bind the exact materialized configuration")
    source_instance = argv[8]
    suffix = f"-replica-{replica}"
    if (not argv[6] or not source_instance.endswith(suffix) or len(source_instance) == len(suffix) or
            argv[10] != str(root / "raw" / f"replica-{replica}.jsonl") or
            argv[12] != "replica-0" or argv[14] != f"{source_instance[:-len(suffix)]}-replica-0"):
        _fail("replica argv does not bind the retained structured-event contract")


def _hex(value: object, label: str, length: int = 64) -> str:
    if not isinstance(value, str) or len(value) != length or any(char not in _HEX for char in value):
        _fail(f"{label} is not lower-case hexadecimal")
    return value


def _hex_public(value: object, label: str) -> str:
    """Accept the native public-key encodings without imposing digest width."""
    if (not isinstance(value, str) or not value or len(value) % 2 or
            len(value) > 32 * 1024 or any(char not in _HEX for char in value)):
        _fail(f"{label} is not lower-case hexadecimal")
    return value


def _read_regular(path: Path, label: str, maximum: int = 512 * 1024) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        _fail(f"{label} is not a readable regular file")
        raise AssertionError from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size < 0 or before.st_size > maximum:
            _fail(f"{label} is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                _fail(f"{label} changed during read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            _fail(f"{label} changed during read")
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            _fail(f"{label} changed during read")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _json(raw: bytes, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                _fail(f"{label} repeats a JSON field")
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


def _value(argv: Sequence[str], option: str) -> str:
    positions = [index for index, value in enumerate(argv) if value == option]
    if len(positions) != 1 or positions[0] + 1 >= len(argv):
        _fail(f"manager argv lacks exactly one {option}")
    return argv[positions[0] + 1]


_MANAGER_SINGLETON_FLAGS = (
    "--protocol-mode", "--listen", "--tls-privkey", "--tls-cert", "--issuer-id",
    "--issuer-private-key", "--activation-delay-blocks", "--convergence-deadline-seconds",
    "--tree-fanout", "--pipeline-stretch", "--shape-candidate-fanouts",
    "--shape-deterministic-seed", "--transition-request", "--bundle-output",
    "--epoch-zero-tree-file", "--structured-event-run-id",
    "--structured-event-source-instance", "--structured-event-output",
    "--activation-readiness-release-count",
    "--activation-readiness-maximum-delivery-attempts",
    "--activation-readiness-retry-interval-ticks",
    "--operator-capacity-stage-a-envelope", "--operator-capacity-stage-a-wire-sha256",
    "--operator-capacity-label-issuer-id", "--operator-capacity-label-issuer-reference",
    "--operator-capacity-label-issuer-public-key-hex",
    "--operator-capacity-label-issuer-public-key-fingerprint",
    "--operator-capacity-approved-capacity-digest", "--operator-capacity-hard-deadline-ns",
    "--operator-capacity-stage-b-authorization-output",
    "--operator-capacity-consumption-output", "--operator-capacity-stage-b-issuer-reference",
)
_MANAGER_FIXED_VALUES = {
    "--protocol-mode": "adaptive_v3", "--issuer-id": "1",
    "--activation-delay-blocks": "5", "--convergence-deadline-seconds": "30",
    "--tree-fanout": "5", "--pipeline-stretch": "2",
    "--shape-candidate-fanouts": "5", "--shape-deterministic-seed": "1",
    # This no-fault W18 comparison requires every replica to activate E1.
    # R=N controls certificate recipients; the consensus quorum remains Q.
    "--activation-readiness-release-count": str(N),
    "--activation-readiness-maximum-delivery-attempts": "1",
    # The native CLI converts ticks from milliseconds to RAW nanoseconds.
    # With one delivery attempt, allow its ACK within the 30-second window.
    "--activation-readiness-retry-interval-ticks": "30000",
}


def _validate_manager_argv(argv: Sequence[str], *, root: Path) -> None:
    """Accept only the materializer's ordered manager command grammar.

    The manifest digest is an integrity binding, not an authorization for a
    caller to mint a different command.  Parse the complete argv before any
    individual value lookup so appended, duplicated, and unknown switches are
    rejected even if the manifest digest is refreshed to match them.
    """
    expected_length = 1 + 2 * (len(_MANAGER_SINGLETON_FLAGS) + 2 * N)
    if len(argv) != expected_length or not isinstance(argv[0], str) or not argv[0]:
        _fail("manager argv differs from the materializer command grammar")
    pairs = [(argv[index], argv[index + 1]) for index in range(1, len(argv), 2)]
    if any(not isinstance(flag, str) or not isinstance(value, str) or not flag or not value
           for flag, value in pairs):
        _fail("manager argv differs from the materializer command grammar")
    singleton = pairs[:len(_MANAGER_SINGLETON_FLAGS)]
    if tuple(flag for flag, _ in singleton) != _MANAGER_SINGLETON_FLAGS:
        _fail("manager argv differs from the materializer command grammar")
    if any(value != _MANAGER_FIXED_VALUES.get(flag, value) for flag, value in singleton
           if flag in _MANAGER_FIXED_VALUES):
        _fail("manager argv changes a frozen cadence or protocol parameter")
    values = dict(singleton)
    if (not values["--listen"].startswith("127.0.0.1:") or
            not values["--tls-privkey"] or not values["--tls-cert"] or
            not values["--issuer-private-key"] or not values["--structured-event-run-id"] or
            not values["--structured-event-source-instance"] or
            not values["--operator-capacity-stage-b-issuer-reference"]):
        _fail("manager argv does not bind required dynamic identity inputs")
    try:
        if int(values["--listen"].rsplit(":", 1)[1]) not in range(1, 65536):
            raise ValueError
        if int(values["--operator-capacity-hard-deadline-ns"]) <= 0:
            raise ValueError
        if int(values["--operator-capacity-label-issuer-id"]) <= 0:
            raise ValueError
    except ValueError:
        _fail("manager argv has an invalid dynamic numeric value")
    for flag in ("--operator-capacity-stage-a-wire-sha256",
                 "--operator-capacity-label-issuer-public-key-fingerprint",
                 "--operator-capacity-approved-capacity-digest"):
        _hex(values[flag], f"manager {flag}")
    for flag in ("--tls-privkey", "--tls-cert"):
        _hex_public(values[flag], f"manager {flag}")
    _hex(values["--issuer-private-key"], "manager --issuer-private-key")
    if len(values["--operator-capacity-label-issuer-public-key-hex"]) != 66 or any(
            char not in _HEX for char in values["--operator-capacity-label-issuer-public-key-hex"]):
        _fail("manager public key does not have the materializer format")
    expected_repeat_flags = tuple(item for replica in range(N) for item in (
        "--replica", "--activation-readiness-member"))
    repeated = pairs[len(_MANAGER_SINGLETON_FLAGS):]
    if tuple(flag for flag, _ in repeated) != expected_repeat_flags:
        _fail("manager argv differs from the materializer command grammar")
    for replica in range(N):
        replica_value = repeated[2 * replica][1]
        readiness_value = repeated[2 * replica + 1][1]
        try:
            replica_id, endpoint, certificate = replica_value.split(",", 2)
            readiness_id, public_key = readiness_value.split(",", 1)
            port = int(endpoint.rsplit(":", 1)[1])
        except ValueError:
            _fail("manager argv has an invalid replica identity binding")
        if (replica_id != str(replica) or readiness_id != str(replica) or
                not endpoint.startswith("127.0.0.1:") or port not in range(1, 65536) or
                not certificate or not public_key):
            _fail("manager argv has an invalid replica identity binding")
        _hex_public(certificate, "manager replica TLS certificate")
        _hex_public(public_key, "manager readiness public key")
    # Keep every root-relative input/output check centralized at its use site.
    # This call also rejects an absolute path outside this materialization.
    _under(root, values["--structured-event-output"], "manager event output")


def _argv_digest(argv: Sequence[str]) -> str:
    if any(not isinstance(value, str) or not value for value in argv):
        _fail("argv contains an invalid value")
    return _sha(json.dumps(list(argv), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n")


def _under(root: Path, candidate: str, label: str) -> Path:
    path = Path(candidate)
    try:
        resolved = path.resolve(strict=False)
        resolved.relative_to(root.resolve())
    except ValueError:
        _fail(f"{label} escapes the exact materialization root")
    return resolved


def _transition_policy(manager_argv: Sequence[str]) -> str:
    raw = _value(manager_argv, "--transition-request")
    document = _json(raw.encode("ascii"), "manager transition request")
    if document != _FROZEN_TRANSITION_REQUEST:
        _fail("manager transition request does not bind the frozen W18 E0-to-E1 contract")
    return "performance_optimization"


def validate_materialized_manager_argv(
    *, root: Path, manager_argv: Sequence[str], expected_sha256: object,
) -> None:
    """Validate the full frozen manager command retained for raw replay."""
    _validate_manager_argv(manager_argv, root=root)
    if _argv_digest(manager_argv) != expected_sha256:
        _fail("retained manager argv differs from frozen materialization manifest")
    _transition_policy(manager_argv)


def validate_materialized_synthetic_workload(manifest: Mapping[str, object]) -> dict[str, object]:
    """Validate the complete frozen W18 synthetic drive identity without I/O."""
    workload = manifest.get("synthetic_workload")
    artifacts = manifest.get("artifact_sha256")
    binaries = manifest.get("binary_sha256")
    expected = {"kind", "source_revision", "hotstuff_app_sha256", "main_config_sha256",
                "block_size", "initial_beat_delay_ms", "beat_interval_ms",
                "transaction_count_per_block", "post_e1_window_ns"}
    if (not isinstance(workload, dict) or set(workload) != expected or
            not isinstance(artifacts, dict) or not isinstance(binaries, dict) or
            workload.get("kind") != "replica-local-synthetic-v1" or
            workload.get("source_revision") != manifest.get("revision") or
            workload.get("hotstuff_app_sha256") != binaries.get("hotstuff_app") or
            workload.get("main_config_sha256") != artifacts.get("config/hotstuff.gen.conf") or
            workload.get("block_size") != 1 or workload.get("initial_beat_delay_ms") != 10_000 or
            workload.get("beat_interval_ms") != 50 or workload.get("transaction_count_per_block") != 1 or
            workload.get("post_e1_window_ns") != 30 * 1_000_000_000):
        _fail("synthetic workload identity differs from frozen W18 contract")
    return dict(workload)


def prepare_no_launch_backend(
    *, materialization_root: Path, manager_argv: Sequence[str],
    replica_argv: Sequence[Sequence[str]], quota_profile: Path,
) -> dict[str, object]:
    """Audit one frozen arm and return a non-executable process/cleanup plan.

    It writes nothing and starts nothing.  Native optimization-first admission
    is already checked by the manager, but no process runner exists here, so
    this remains a review-only plan rather than a launch authorization.
    """
    root = Path(materialization_root)
    if root.is_symlink() or not root.is_dir():
        _fail("materialization root is not a real directory")
    manifest_path = root / "materialization-manifest.json"
    manifest = _json(_read_regular(manifest_path, "materialization manifest"), "materialization manifest")
    required = {
        "schema_version", "kind", "verdict", "claim_eligible", "figure_eligible",
        "arm", "stage_a_native_arm", "protocol", "slow_root_ids", "revision",
        "epoch0_tree", "binary_sha256", "artifact_sha256", "manager_argv_sha256",
        "replica_argv_sha256", "synthetic_workload", "stage_a_envelope_sha256",
        "stage_a_verifier_receipt_sha256", "identity_parity_receipt_sha256",
        "public_identity_fingerprint",
        "tool_identity_approval_receipt_sha256", "stage_a_verifier_arguments",
        "stage_b_authorization_output", "consumption_output", "bundle_output",
    }
    if set(manifest) != required:
        _fail("materialization manifest schema differs")
    if (manifest["schema_version"] != 1 or manifest["kind"] != _MANIFEST_KIND or
            manifest["verdict"] != "MATERIALIZED_NO_EXECUTION" or
            manifest["claim_eligible"] is not False or manifest["figure_eligible"] is not False or
            manifest["arm"] not in _ARMS or manifest["stage_a_native_arm"] != _ARMS[manifest["arm"]] or
            manifest["protocol"] != {"N": N, "Q": Q, "tree_count": Q} or
            manifest["slow_root_ids"] != list(range(6))):
        _fail("materialization manifest is not the frozen W18 no-launch shape")
    _hex(manifest["revision"], "materialization revision", 40)
    for field in (
        "stage_a_envelope_sha256", "stage_a_verifier_receipt_sha256",
        "identity_parity_receipt_sha256",
        "public_identity_fingerprint",
        "tool_identity_approval_receipt_sha256",
    ):
        _hex(manifest[field], field)
    if not isinstance(manifest["binary_sha256"], dict) or set(manifest["binary_sha256"]) != {
        "adaptation_manager", "hotstuff_app", "identity_parity_verifier",
    }:
        _fail("materialization binary map is invalid")
    for name, digest in manifest["binary_sha256"].items():
        _hex(digest, f"binary {name}")
    workload = validate_materialized_synthetic_workload(manifest)
    if not isinstance(manifest["artifact_sha256"], dict):
        _fail("materialization artifact map is invalid")
    expected_artifacts = {"config/epoch0.tree", "config/stage-a-envelope.wire", "config/identity-parity-receipt.json", "config/identity-public-projection.json", "config/hotstuff.gen.conf"} | {
        f"config/replica-{replica}.conf" for replica in range(N)
    }
    if set(manifest["artifact_sha256"]) != expected_artifacts:
        _fail("materialization artifact map does not cover exactly N31 configuration")
    for relative, digest in manifest["artifact_sha256"].items():
        _hex(digest, f"artifact {relative}")
        if _sha(_read_regular(root / relative, f"artifact {relative}")) != digest:
            _fail("materialization artifact digest differs")
    for replica in range(N):
        raw_replica = _read_regular(root / f"config/replica-{replica}.conf", f"replica-{replica} config")
        _validate_replica_config(raw_replica, replica)
    if manifest["artifact_sha256"]["config/epoch0.tree"] != manifest["epoch0_tree"]["sha256"]:
        _fail("materialized Epoch-0 tree artifact differs from the manifest binding")
    validate_materialized_manager_argv(
        root=root, manager_argv=manager_argv,
        expected_sha256=manifest["manager_argv_sha256"])
    validate_synthetic_main_config(_read_regular(root / "config/hotstuff.gen.conf", "materialized main config"),
                                   root=root, manager_argv=manager_argv)
    public_identity_fingerprint = validate_materialized_public_identity(
        root=root, manager_argv=manager_argv,
        receipt_sha256=manifest["identity_parity_receipt_sha256"],
        expected_fingerprint=manifest["public_identity_fingerprint"], source_revision=manifest["revision"])
    if not isinstance(manifest["replica_argv_sha256"], list) or len(replica_argv) != N or len(manifest["replica_argv_sha256"]) != N:
        _fail("replica argv set does not cover exactly N31")
    for replica, argv in enumerate(replica_argv):
        if _argv_digest(argv) != manifest["replica_argv_sha256"][replica]:
            _fail("replica argv differs from frozen materialization manifest")
        _validate_replica_argv(argv, root=root, replica=replica)
    if _value(manager_argv, "--protocol-mode") != "adaptive_v3" or "--experiment-byzantine-mode" in manager_argv:
        _fail("manager argv is not all-live adaptive-v3")
    manager_event = _under(root, _value(manager_argv, "--structured-event-output"), "manager event output")
    epoch0_tree = _under(root, _value(manager_argv, "--epoch-zero-tree-file"), "manager Epoch-0 tree input")
    stage_a_envelope = _under(root, _value(manager_argv, "--operator-capacity-stage-a-envelope"), "manager Stage-A envelope input")
    stage_b = _under(root, _value(manager_argv, "--operator-capacity-stage-b-authorization-output"), "Stage-B authorization output")
    consumption = _under(root, _value(manager_argv, "--operator-capacity-consumption-output"), "consumption output")
    bundle = _under(root, _value(manager_argv, "--bundle-output"), "successor bundle output")
    expected_paths = {
        manager_event: root / "raw/manager-events.jsonl", stage_b: root / manifest["stage_b_authorization_output"],
        consumption: root / manifest["consumption_output"], bundle: root / manifest["bundle_output"],
    }
    if any(actual != expected for actual, expected in expected_paths.items()) or epoch0_tree != root / "config/epoch0.tree" or stage_a_envelope != root / "config/stage-a-envelope.wire":
        _fail("manager output path differs from the exact materialization manifest")
    if (_sha(_read_regular(epoch0_tree, "materialized Epoch-0 tree")) != manifest["epoch0_tree"]["sha256"] or
            _sha(_read_regular(stage_a_envelope, "materialized Stage-A envelope")) != manifest["stage_a_envelope_sha256"] or
            _value(manager_argv, "--operator-capacity-stage-a-wire-sha256") != manifest["stage_a_envelope_sha256"]):
        _fail("manager input differs from the exact materialized Stage-A/Epoch-0 binding")
    expected_verifier_arguments = [
        "--epoch0-tree-file", str(epoch0_tree), "--stage-a-envelope-wire", str(stage_a_envelope),
        "--issuer-id", _value(manager_argv, "--operator-capacity-label-issuer-id"),
        "--issuer-reference", _value(manager_argv, "--operator-capacity-label-issuer-reference"),
        "--issuer-public-key-hex", _value(manager_argv, "--operator-capacity-label-issuer-public-key-hex"),
        "--issuer-public-key-fingerprint", _value(manager_argv, "--operator-capacity-label-issuer-public-key-fingerprint"),
        "--approved-capacity-digest", _value(manager_argv, "--operator-capacity-approved-capacity-digest"),
        "--arm", _ARMS[manifest["arm"]], "--source-revision", manifest["revision"],
    ]
    if manifest["stage_a_verifier_arguments"] != expected_verifier_arguments:
        _fail("persisted native Stage-A verifier invocation differs from the executable plan")
    raw = root / "raw"
    transition = root / "transitions/e0-to-e1-operator-capacity"
    if not raw.is_dir() or not transition.is_dir() or any(raw.iterdir()) or any(transition.iterdir()):
        _fail("materialization raw and transition outputs must be fresh before backend planning")
    quota_raw = _read_regular(Path(quota_profile), "frozen CPU quota profile", 64 * 1024)
    try:
        _validate_quota_profile_bytes(quota_raw)
    except Exception as exc:
        _fail("quota profile differs from frozen W18 N31 ownership contract")
        raise AssertionError from exc
    policy = _transition_policy(manager_argv)
    if policy != "performance_optimization":
        _fail("W18 operator-capacity materialization requires optimization-first policy")
    return {
        "schema_version": 1,
        "kind": "kauri-n31-operator-capacity-v3-no-launch-backend-plan-v1",
        "verdict": "BACKEND_PLAN_REVIEW_REQUIRED_NO_EXECUTION",
        "claim_eligible": False, "figure_eligible": False, "launch_permitted": False,
        "materialization_manifest_sha256": _sha(_read_regular(manifest_path, "materialization manifest")),
        "arm": manifest["arm"], "revision": manifest["revision"], "automatic_retries": 0,
        "epoch0_tree": dict(manifest["epoch0_tree"]),
        "binary_sha256": dict(manifest["binary_sha256"]),
        "public_identity_fingerprint": public_identity_fingerprint,
        "synthetic_workload": dict(manifest["synthetic_workload"]),
        "post_e1_window_ns": manifest["synthetic_workload"]["post_e1_window_ns"],
        "quota_ownership": {
            "replica_ids": list(range(N)), "launcher": "systemd-user-scope-cpu-quota-v1",
            "manager_visibility": "none", "scope_count": N,
        },
        "stage_a": {
            "envelope_sha256": manifest["stage_a_envelope_sha256"],
            "native_receipt_sha256": manifest["stage_a_verifier_receipt_sha256"],
            "identity_parity_receipt_sha256": manifest["identity_parity_receipt_sha256"],
            "tool_identity_approval_receipt_sha256":
                manifest["tool_identity_approval_receipt_sha256"],
            "verifier_arguments": list(manifest["stage_a_verifier_arguments"]),
        },
        "stage_b": {
            "authorization_output": str(stage_b.relative_to(root)),
            "consumption_output": str(consumption.relative_to(root)),
            "must_be_exclusively_written_before_successor_publication": True,
            "must_be_independently_verified_after_manager_terminal": True,
        },
        "cleanup_contract": {
            "required_order": ["stop_quota_monitor", "terminate_manager_and_replicas", "terminate_owned_replica_scopes", "verify_scope_cleanup"],
            "all_31_replica_scope_ownership_required": True,
            "manager_exit_zero_and_success_terminal_required": True,
            "raw_retention_required": True,
        },
        "native_policy_order_repaired": True,
        # This plan remains non-executable by itself.  A separate runner may
        # consume it only after it reopens the external authorization and the
        # complete Stage-A authority chain at the process-spawn edge.
        "execution_blocker": "EXTERNAL_AUTHORIZATION_AND_PRESPAWN_AUTHORITY_REQUIRED",
    }


def execution_not_implemented() -> None:
    """Hard stop: the plan is never a process-launch authorization."""
    _fail("W18 v3 backend execution is blocked: no process runner is implemented")
