#!/usr/bin/env python3
"""Emit one no-launch N31 operator-capacity preflight request."""
from __future__ import annotations
import argparse
from pathlib import Path
from kauri_experiment.operator_capacity_preflight import canonical_json, canonical_request

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser()
parser.add_argument("--snapshot-wire", type=Path, required=True)
parser.add_argument("--capacity-digest-binary", type=Path, required=True)
parser.add_argument("--epoch0-digest-binary", type=Path, required=True)
parser.add_argument("--epoch0-tree-file", type=Path, required=True)
parser.add_argument("--epoch0-arm", choices=("slow-roots", "fast-roots"), required=True)
parser.add_argument("--arm", choices=("treatment", "sham"), required=True)
parser.add_argument("--quota-profile", type=Path, required=True)
parser.add_argument("--output-root", type=Path, required=True)
parser.add_argument("--issuer-public-key-fingerprint", required=True)
parser.add_argument("--app", type=Path, required=True); parser.add_argument("--keygen", type=Path, required=True)
parser.add_argument("--tls-keygen", type=Path, required=True); parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
request = canonical_request(repository=ROOT, snapshot_wire=args.snapshot_wire, capacity_digest_binary=args.capacity_digest_binary,
    epoch0_digest_binary=args.epoch0_digest_binary, epoch0_arm=args.epoch0_arm, epoch0_tree_file=args.epoch0_tree_file,
    arm=args.arm, quota_profile=args.quota_profile, output_root=args.output_root,
    issuer_public_key_fingerprint=args.issuer_public_key_fingerprint,
    binaries={"app": args.app, "keygen": args.keygen, "tls_keygen": args.tls_keygen,
              "capacity_digest": args.capacity_digest_binary, "epoch0_digest": args.epoch0_digest_binary})
if args.output.exists() or args.output.is_symlink(): raise SystemExit("output already exists")
with args.output.open("xb") as target:
    target.write(canonical_json(request)); target.flush()
