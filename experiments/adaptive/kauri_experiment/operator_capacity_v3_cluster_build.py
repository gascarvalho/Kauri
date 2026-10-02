"""Actual clean W18 native build, pinned dependencies and exclusive booking."""
from datetime import datetime, timezone
from pathlib import Path
import os
import shutil
import socket
import subprocess

from . import operator_capacity_v3_cluster as cluster


TOOLS = {
    "adaptation_manager": "examples/adaptation-manager", "hotstuff_app": "examples/hotstuff-app",
    "keygen": "hotstuff-keygen", "tls_keygen": "hotstuff-tls-keygen",
    "capacity_digest": "examples/operator-capacity-snapshot-digest", "epoch0_digest": "examples/static-epoch0-digest",
    "stage_a_envelope_signer": "examples/operator-capacity-label-envelope-sign",
    "stage_a_envelope_verifier": "examples/operator-capacity-label-envelope-verify",
    "stage_b_authorization_verifier": "examples/operator-capacity-authorization-verify",
    "identity_parity_verifier": "examples/operator-capacity-identity-parity-verify",
    "readiness_verifier": "examples/epoch-profile-digest"}
DEPS = Path("/home/gascarvalho/kauri-24b87e63/deps")
DEPS_SHA = "c36a5ff6e67f6c8135a9db82329da2634f12e30a1b0dc496d4855816e9bb2ac7"


def dependency_tree():
    rows = []
    for path in sorted(DEPS.rglob("*")):
        relative = str(path.relative_to(DEPS))
        if path.is_symlink():
            rows.append({"path": relative, "type": "symlink", "target": os.readlink(path)})
        elif path.is_file():
            data = path.read_bytes()
            rows.append({"path": relative, "type": "file", "size": len(data), "sha256": cluster.sha(data)})
    digest = cluster.sha(cluster.canonical(rows).rstrip(b"\n"))
    if len(rows) != 85 or digest != DEPS_SHA:
        raise cluster.ClusterError("existing explicit native dependency tree changed")
    return {"root": str(DEPS), "entries": rows, "tree_sha256": digest}


def build(repo, destination, revision, booking_id):
    os.umask(0o077)
    if socket.gethostname() != "proteina02":
        raise cluster.ClusterError("wrong W18 native build host")
    cluster.repository_state(repo, revision)
    cluster.require_no_owned_native()
    booking = subprocess.check_output(["gsd_manager", "-N", "proteina02", "booking", "ls",
        "-c", "-u", "gascarvalho", "-m", "exclusive"], text=True, timeout=25)
    cluster.booking_row(booking, booking_id, datetime.now(timezone.utc), reserve_s=11500)
    destination.mkdir(mode=0o700)
    deps = dependency_tree()
    cluster.write(destination / "dependencies-before.json", deps)
    env = {**os.environ, "CMAKE_PREFIX_PATH": str(DEPS / "cmake-prefix"),
        "CMAKE_INCLUDE_PATH": str(DEPS / "root/usr/include"),
        "CMAKE_LIBRARY_PATH": str(DEPS / "root/usr/lib/x86_64-linux-gnu"),
        "PKG_CONFIG_PATH": str(DEPS / "root/usr/lib/x86_64-linux-gnu/pkgconfig"),
        "PKG_CONFIG_SYSROOT_DIR": str(DEPS / "root")}
    configure = ["cmake", "-S", ".", "-B", "build-adaptive", "-DCMAKE_BUILD_TYPE=Release",
        "-DBUILD_TESTING=OFF", "-DBUILD_TEST=OFF", "-DHOTSTUFF_DEBUG_LOG=OFF",
        "-DHOTSTUFF_NORMAL_LOG=ON", "-DHOTSTUFF_PROTO_LOG=OFF", "-DHOTSTUFF_MSG_STAT=ON",
        "-DHOTSTUFF_TWO_STEP=OFF"]
    command = ["cmake", "--build", "build-adaptive", "--clean-first", "--target",
        *(Path(path).name for path in TOOLS.values()), "-j2"]
    log = destination / "build.log"
    with log.open("xb") as stream:
        for argv in (configure, command):
            stream.write(("COMMAND: " + " ".join(argv) + "\n").encode()); stream.flush()
            subprocess.run(argv, cwd=repo, env=env, stdout=stream, stderr=subprocess.STDOUT,
                           check=True, timeout=1800)
        stream.flush(); os.fsync(stream.fileno())
    cluster.repository_state(repo, revision)
    if dependency_tree() != deps:
        raise cluster.ClusterError("native dependency bytes changed during build")
    cache = (repo / "build-adaptive/CMakeCache.txt").read_bytes()
    (destination / "CMakeCache.txt").write_bytes(cache)
    binaries = {}
    for name, relative in TOOLS.items():
        path = repo / "build-adaptive" / relative
        if path.is_symlink() or not os.access(path, os.X_OK):
            raise cluster.ClusterError("required clean-build executable absent")
        binaries[name] = {"path": str(path), "sha256": cluster.sha(path.read_bytes()), "size": path.stat().st_size}
    out = lambda args: subprocess.check_output(args, cwd=repo, text=True).strip()
    compiler = shutil.which("c++")
    receipt = {"kind": "kauri-w18-cluster-build-provenance-v1", "repository_revision": revision,
        "origin_revision": revision, "repository_clean_after_build": True, "repository_branch": "feature/adaptive-epoch-throughput",
        "host": "proteina02", "linux_boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "build_type": "Release", "build_exit_code": 0, "build_log_path": str(log),
        "build_log_sha256": cluster.sha(log.read_bytes()), "cmake_cache_sha256": cluster.sha(cache),
        "build_commands": [configure, command], "dependency_tree_sha256": DEPS_SHA,
        "binaries": binaries, "cmake_version": out(["cmake", "--version"]).splitlines()[0],
        "cxx_compiler": compiler, "cxx_version": out([compiler, "--version"]).splitlines()[0],
        "submodule_status": out(["git", "submodule", "status"]), "booking_stdout": booking,
        "booking_id": booking_id, "recorded_utc": datetime.now(timezone.utc).isoformat()}
    cluster.write(destination / "build-provenance.json", receipt)
    return receipt


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    for name in ("repo", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("revision", "booking"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    build(args.repo, args.output, args.revision, args.booking)
