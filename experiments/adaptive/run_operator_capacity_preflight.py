#!/usr/bin/env python3
"""Emit one no-launch N31 operator-capacity preflight request."""
from __future__ import annotations
import argparse
from pathlib import Path
from kauri_experiment.operator_capacity_preflight import canonical_json, canonical_request

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser()
parser.add_argument("--capacity-snapshot-wire", type=Path, required=True,
                    help="canonical raw capacity snapshot; used only by the snapshot digest helper")
parser.add_argument("--stage-a-envelope-wire", type=Path, required=True,
                    help="issuer-signed Stage-A label envelope; verified only by the native receipt")
parser.add_argument("--capacity-digest-binary", type=Path, required=True)
parser.add_argument("--epoch0-digest-binary", type=Path, required=True)
parser.add_argument("--epoch0-tree-file", type=Path, required=True)
parser.add_argument("--epoch0-arm", choices=("slow-roots", "fast-roots"), required=True)
parser.add_argument("--arm", choices=("treatment", "sham"), required=True)
parser.add_argument("--quota-profile", type=Path, required=True)
parser.add_argument("--output-root", type=Path, required=True)
parser.add_argument("--issuer-public-key-fingerprint", required=True)
parser.add_argument("--issuer-id", type=int, required=True)
parser.add_argument("--issuer-reference", required=True)
parser.add_argument("--approved-capacity-digest", required=True)
parser.add_argument("--issuer-public-key-hex", required=True)
parser.add_argument("--epoch0-topology-digest", required=True,
                    help="native Stage-A topology digest, distinct from the consensus E0 digest")
parser.add_argument("--native-envelope-verifier", type=Path, required=True)
parser.add_argument("--native-envelope-receipt-output", type=Path, required=True)
parser.add_argument("--tool-identity-document", type=Path,
                    help="unverified tool provenance; absent means pending and never launches")
parser.add_argument("--adaptation-manager", type=Path, required=True)
parser.add_argument("--keygen", type=Path, required=True)
parser.add_argument("--tls-keygen", type=Path, required=True); parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
request = canonical_request(repository=ROOT, capacity_snapshot_wire=args.capacity_snapshot_wire,
    stage_a_envelope_wire=args.stage_a_envelope_wire,
    capacity_digest_binary=args.capacity_digest_binary,
    epoch0_digest_binary=args.epoch0_digest_binary, epoch0_arm=args.epoch0_arm, epoch0_tree_file=args.epoch0_tree_file,
    arm=args.arm, quota_profile=args.quota_profile, output_root=args.output_root,
    issuer_id=args.issuer_id, issuer_reference=args.issuer_reference,
    issuer_public_key_fingerprint=args.issuer_public_key_fingerprint,
    approved_capacity_digest=args.approved_capacity_digest,
    issuer_public_key_hex=args.issuer_public_key_hex,
    epoch0_topology_digest=args.epoch0_topology_digest,
    native_envelope_verifier_binary=args.native_envelope_verifier,
    native_envelope_receipt_output=args.native_envelope_receipt_output,
    tool_identity_document=args.tool_identity_document,
    binaries={"adaptation_manager": args.adaptation_manager, "keygen": args.keygen, "tls_keygen": args.tls_keygen,
              "capacity_digest": args.capacity_digest_binary, "epoch0_digest": args.epoch0_digest_binary,
              "stage_a_envelope_verifier": args.native_envelope_verifier})
if args.output.exists() or args.output.is_symlink(): raise SystemExit("output already exists")
with args.output.open("xb") as target:
    target.write(canonical_json(request)); target.flush()
