"""Fresh signed W18 inputs with shared keys/capacity labels within each block."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import struct
import subprocess

from . import operator_capacity_v3_cluster as cluster
from . import operator_capacity_v3_backend as backend
from . import operator_capacity_v3_materializer as mat
from . import factorial_validation as fv


def invoke(argv):
    result = subprocess.run([str(x) for x in argv], capture_output=True, timeout=30)
    if result.returncode:
        raise cluster.ClusterError("native input tool rejected: " + Path(str(argv[0])).name)
    return result.stdout


def prepare(root, *, shared_path, private, run_id, arm, regime, repo, build_receipt,
            booking_id, approval_reference):
    os.umask(0o077)
    build, _ = cluster.read(build_receipt)
    revision = build["repository_revision"]
    cluster.repository_state(repo, revision)
    tools = {name: Path(row["path"]) for name, row in build["binaries"].items()}
    private.mkdir(mode=0o700)
    if not shared_path.exists():
        def key(row):
            public, secret = row.split()
            return {"pub": public.removeprefix("pub:"), "sec": secret.removeprefix("sec:")}
        bls = [key(row) for row in invoke([tools["keygen"], "--secure-bls-preallocation", "--num", 31,
            "--algo", "bls"]).decode().splitlines()]
        tls = []
        for row in invoke([tools["tls_keygen"], "--num", 32]).decode().splitlines():
            certificate, secret, common = row.split()
            tls.append({"crt": certificate.removeprefix("crt:"), "sec": secret.removeprefix("sec:"),
                        "cid": common.removeprefix("cid:")})
        issuer = key(invoke([tools["keygen"], "--num", 1, "--algo", "secp256k1"]).decode().strip())
        label = key(invoke([tools["keygen"], "--num", 1, "--algo", "secp256k1"]).decode().strip())
        cluster.write(shared_path, {"bls": bls, "tls": tls, "issuer": issuer, "label": label,
            "snapshot_start_raw_ns": cluster.clock()})
    shared, _ = cluster.read(shared_path)
    identities = {name: shared[name] for name in ("bls", "tls", "issuer")}
    parsed = mat._identities(identities)
    (private / "identities.bundle").write_bytes(mat._identity_bundle(*parsed))
    (private / "label.sec").write_text(shared["label"]["sec"], encoding="ascii")
    (private / "epoch0.tree").write_bytes(mat.canonical_e0_tree())
    trees = tuple(fv.Tree(tree_id=i, fanout=5, pipeline_stretch=2,
        members=tuple(int(x) for x in row.split()[2:]), wait_exempt=())
        for i, row in enumerate(mat.canonical_e0_tree().decode().splitlines()))
    e0 = cluster.sha(fv._epoch_canonical_bytes(epoch_number=0, previous_epoch_digest="0" * 64,
        membership_digest=fv._membership_digest(tuple(range(31))), generation_seed=0,
        policy_version="adaptive-v2-bootstrap", evidence_snapshot_id="adaptive-v2-bootstrap-epoch-zero",
        evidence_cutoff=0, trees=trees))
    start = shared["snapshot_start_raw_ns"]; until = start + 1800_000_000_000
    reference = "w18-cluster-capacity-labels"; reference_bytes = reference.encode()
    wire = (b"kauri-operator-capacity-snapshot-wire-v1" + struct.pack(">II", 1, len(reference_bytes)) +
        reference_bytes + struct.pack(">I", 0) + bytes.fromhex(e0) + struct.pack(">BQQI", 1, start, until, 31) +
        b"".join(struct.pack(">HB", i, 1 if i < 6 else 2) for i in range(31)))
    (private / "snapshot.wire").write_bytes(wire)
    capacity = invoke([tools["capacity_digest"], "--validate-n31", e0, private / "snapshot.wire"]).decode().strip()
    native_arm = mat._ARMS[arm]
    invoke([tools["stage_a_envelope_signer"], "--epoch0-tree-file", private / "epoch0.tree",
        "--capacity-snapshot-wire", private / "snapshot.wire", "--issuer-private-key-file", private / "label.sec",
        "--issuer-id", 73, "--issuer-reference", reference, "--arm", native_arm,
        "--valid-from-monotonic-raw-ns", start, "--valid-until-monotonic-raw-ns", until,
        "--source-revision", revision, "--wire-output", private / "stage-a.wire",
        "--receipt-output", private / "stage-a-produced.json"])
    fingerprint = cluster.sha(bytes.fromhex(shared["label"]["pub"]))
    invoke([tools["stage_a_envelope_verifier"], "--epoch0-tree-file", private / "epoch0.tree",
        "--stage-a-envelope-wire", private / "stage-a.wire", "--issuer-id", 73,
        "--issuer-reference", reference, "--issuer-public-key-hex", shared["label"]["pub"],
        "--issuer-public-key-fingerprint", fingerprint, "--approved-capacity-digest", capacity,
        "--arm", native_arm, "--source-revision", revision, "--output", private / "stage-a-verified.json"])
    verified, _ = cluster.read(private / "stage-a-verified.json")
    identity = mat._identity_public_fingerprint(*parsed)
    invoke([tools["identity_parity_verifier"], "--identity-bundle", private / "identities.bundle",
        "--source-revision", revision, "--expected-public-fingerprint", identity,
        "--output", private / "identity-verified.json"])
    sha_path = lambda path: cluster.sha(path.read_bytes())
    cluster.write(private / "tool-approval.json", {"schema_version": 1,
        "kind": "kauri-n31-operator-capacity-tool-identity-approval-v1", "verdict": "EXTERNAL_TOOL_IDENTITY_APPROVED",
        "revision": revision, "approval_ref": approval_reference,
        "approved_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "binary_sha256": {name: build["binaries"][name]["sha256"] for name in tools if name != "readiness_verifier"}})
    result = mat.materialize_operator_capacity_v3(root, arm=arm, run_id=run_id,
        source_instance=run_id + "-manager", ports={"peer_base": 18570, "client_base": 19570, "manager": 20570},
        binaries={name: tools[name] for name in ("adaptation_manager", "hotstuff_app", "identity_parity_verifier")},
        identities=identities, identity_parity_receipt={"path": private / "identity-verified.json", "sha256": sha_path(private / "identity-verified.json")},
        tool_identity_approval={"path": private / "tool-approval.json", "sha256": sha_path(private / "tool-approval.json")},
        epoch0_tree={"path": private / "epoch0.tree", "sha256": sha_path(private / "epoch0.tree"), "topology_digest": verified["epoch0_topology_digest"]},
        stage_a={"envelope_path": private / "stage-a.wire", "envelope_sha256": sha_path(private / "stage-a.wire"),
            "label_issuer_id": 73, "label_issuer_reference": reference, "label_issuer_public_key_hex": shared["label"]["pub"],
            "label_issuer_public_key_fingerprint": fingerprint, "approved_capacity_digest": capacity},
        stage_a_verifier_receipt={"path": private / "stage-a-verified.json", "sha256": sha_path(private / "stage-a-verified.json")},
        source_revision=revision, stage_b_issuer_reference="w18-cluster-epoch-manager", hard_deadline_ns=240_000_000_000)
    cluster.write(root / "private-argv.json", {"manager": list(result["manager_argv"]),
        "replicas": [list(command) for command in result["replica_argv"]]})
    quota_name = ("n31-static-resource-cpu-sham-quota-v1.json" if regime == "heterogeneous"
                  else "n31-operator-capacity-homogeneous-control-quota-v1.json")
    quota = repo / "experiments/adaptive/profiles" / quota_name
    plan = backend.prepare_no_launch_backend(materialization_root=root,
        manager_argv=result["manager_argv"], replica_argv=result["replica_argv"], quota_profile=quota,
        cluster_physical_regime=regime)
    request = cluster.build_request(plan, root=root, run_id=run_id, build_receipt=build_receipt, booking_id=booking_id)
    cluster.write(private / "request.json", request)
    approved = {**request, "kind": cluster.APPROVAL_KIND, "request_sha256": cluster.sha(cluster.canonical(request)),
        "approval_reference": approval_reference, "approved_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    cluster.write(private / "approval.json", approved)
    return {"root": root, "request_path": private / "request.json", "approval_path": private / "approval.json",
        "approval_sha": cluster.sha(cluster.canonical(approved)), "build_receipt": build_receipt,
        "quota_profile": quota, "repo": repo, "tool_identity_approval": private / "tool-approval.json",
        "native_receipt": private / "stage-a-verified.json"}
