#!/usr/bin/env python3
"""Record an actual clean build inside W19's existing exclusive reservation."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import socket
import subprocess

from sustained_role_v8_live import KAURI, LiveError, base, booking_row, canonical, sha, write

BUILD = ["cmake", "--build", "build-adaptive", "--clean-first", "--target", "hotstuff-app",
         "adaptation-manager", "hotstuff-keygen", "hotstuff-tls-keygen", "n7-epoch0-treefile-digest", "-j2"]
CONFIGURE = ["cmake", "-S", ".", "-B", "build-adaptive", "-DCMAKE_BUILD_TYPE=Release",
             "-DBUILD_TESTING=OFF", "-DBUILD_TEST=OFF", "-DHOTSTUFF_DEBUG_LOG=OFF",
             "-DHOTSTUFF_NORMAL_LOG=ON", "-DHOTSTUFF_PROTO_LOG=OFF", "-DHOTSTUFF_MSG_STAT=ON",
             "-DHOTSTUFF_TWO_STEP=OFF"]
BINARIES = {"hotstuff_app": "build-adaptive/examples/hotstuff-app",
            "adaptation_manager": "build-adaptive/examples/adaptation-manager",
            "hotstuff_keygen": "build-adaptive/hotstuff-keygen",
            "hotstuff_tls_keygen": "build-adaptive/hotstuff-tls-keygen",
            "epoch0_treefile_digest": "build-adaptive/examples/n7-epoch0-treefile-digest"}


def output(command):
    return subprocess.check_output(command, cwd=KAURI, text=True).strip()


def run(destination, expected_revision):
    revision = base.verify_repository_state(KAURI).revision
    if revision != expected_revision or socket.gethostname() != "proteina02":
        raise LiveError("clean build host/source differs from pinned revision")
    current_booking = output(["gsd_manager", "-N", "proteina02", "booking", "ls", "-c", "-u", "gascarvalho", "-m", "exclusive"])
    booking_row(current_booking, now=datetime.now(timezone.utc))
    if destination.exists() or destination.is_symlink() or destination != destination.resolve():
        raise LiveError("build evidence destination must be fresh and canonical")
    destination.mkdir(mode=0o700)
    write(destination / "booking-before-build.json", {"stdout": current_booking,
          "observed_utc": datetime.now(timezone.utc).isoformat()})
    log = destination / "build.log"
    with os.fdopen(os.open(log, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as stream:
        for command in (CONFIGURE, BUILD):
            stream.write(("COMMAND: " + " ".join(command) + "\n").encode()); stream.flush()
            result = subprocess.run(command, cwd=KAURI, stdout=stream, stderr=subprocess.STDOUT, timeout=1800)
            if result.returncode:
                raise LiveError("native configure/clean build failed; preserved build log")
        stream.flush(); os.fsync(stream.fileno())
    if base.verify_repository_state(KAURI).revision != revision:
        raise LiveError("source changed during build")
    cache = (KAURI / "build-adaptive/CMakeCache.txt").read_bytes()
    if b"CMAKE_BUILD_TYPE:STRING=Release\n" not in cache or b"HOTSTUFF_TWO_STEP:BOOL=OFF\n" not in cache:
        raise LiveError("effective build flags differ from declared Release/three-step configuration")
    with (destination / "CMakeCache.txt").open("xb") as out:
        out.write(cache)
    binaries = {}
    for name, relative in BINARIES.items():
        path = KAURI / relative
        if path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
            raise LiveError("native build lacks a required executable")
        raw = path.read_bytes()
        binaries[name] = {"path": str(path), "size_bytes": len(raw), "sha256": sha(raw)}
    compiler = shutil.which("c++")
    payload = {"schema_version": 1, "kind": "kauri-w19-cluster-build-provenance-v1",
        "repository_revision": revision, "origin_revision": revision,
        "repository_branch": "feature/adaptive-epoch-throughput", "repository_clean_after_build": True,
        "host": "proteina02", "linux_boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "build_exit_code": 0, "build_command": BUILD, "build_log_path": str(log),
        "build_log_sha256": sha(log.read_bytes()), "build_type": "Release",
        "cmake_version": output(["cmake", "--version"]).splitlines()[0], "cxx_compiler": compiler,
        "cxx_compiler_version": output([compiler, "--version"]).splitlines()[0],
        "submodule_status": output(["git", "submodule", "status"]).splitlines(),
        "recorded_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "binaries": binaries}
    write(destination / "build-provenance.json", payload)
    print(canonical({"receipt": str(destination / "build-provenance.json"),
                     "sha256": sha(canonical(payload)), "revision": revision}).decode(), end="")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    run(args.output, args.revision)
