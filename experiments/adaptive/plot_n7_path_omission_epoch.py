#!/usr/bin/env python3
"""Render the source-bound C-033 N=7 omission/containment explanation.

The renderer refuses any input except a locally preserved C-033 raw root.  It
replays the raw-bundle validator and decodes the signed successor before
projecting only replica numbers, topology, and SHA-256 bindings into a figure.
It intentionally never copies identities, keys, paths, or raw events.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import stat
from typing import Any, Mapping, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_ROOT = REPOSITORY_ROOT / "experiments/adaptive/n7-path-timeout-quorum"
ARCHIVE_NAME = "n7-path-quorum-v4-d7036305-20260929-004"
EVIDENCE_REVISION = "d7036305ab63d25312dc10e0310bcd1befd5c4ea"
EXPECTED_RUN_ID = "n7-path-quorum-v4-d7036305-20260929-004"
SEALED_RECEIPT_SHA256 = "81b2d1ef3b505932214865a9eafee7495d8185a3f5567314ebc22802b5554972"
SEALED_VERDICT_SHA256 = "cf67e43527d39e821e3f0e8ebb6d8b69e7f35237f7491094e1a0f82bd59abecd"
FIGURE_BOUNDARY = (
    "One validator-replayed N=7 controlled omission example; topology and role explanation only; "
    "no throughput implication, general Byzantine attribution, or safety/liveness proof."
)
PDF_METADATA = {"Title": "C-033 N=7 selective omission containment", "Creator": "Kauri C-033 evidence renderer", "CreationDate": None, "ModDate": None}


class PlotError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(_read_regular_bytes(path, "hash input", 16 * 1024 * 1024)).hexdigest()


def _read_regular_bytes(path: Path, label: str, maximum: int) -> bytes:
    """Capture one bounded, non-symlink file read for all later projection."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode) or status.st_size > maximum:
                raise PlotError(f"{label} must be a bounded regular file")
            chunks = []
            remaining = maximum + 1
            while remaining:
                block = os.read(descriptor, min(1024 * 1024, remaining))
                if not block:
                    break
                chunks.append(block); remaining -= len(block)
            data = b"".join(chunks)
            if len(data) != status.st_size or len(data) > maximum:
                raise PlotError(f"{label} changed while being captured")
            return data
        finally:
            os.close(descriptor)
    except (OSError, PlotError) as error:
        if isinstance(error, PlotError):
            raise
        raise PlotError(f"cannot securely read {label}") from error


def _json_bytes(data: bytes, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PlotError(f"{label} is not valid JSON") from error
    if not isinstance(value, Mapping):
        raise PlotError(f"{label} must be a JSON object")
    return value


def _validator():
    source = SCENARIO_ROOT / "validator.py"
    spec = importlib.util.spec_from_file_location("kauri_c033_exact_scenario_validator", source)
    if spec is None or spec.loader is None:
        raise PlotError("cannot load the exact scenario validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _replay_revision() -> str | None:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPOSITORY_ROOT, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def _project_tree(tree_id: int, members: Sequence[int], wait_exempt: Sequence[int], fanout: int) -> Mapping[str, Any]:
    if tuple(sorted(members)) != tuple(range(7)) or fanout != 2 or len(members) != 7:
        raise PlotError("decoded tree differs from frozen N=7 binary membership")
    index = members.index(1)
    parent = None if index == 0 else members[(index - 1) // fanout]
    first_leaf = (len(members) - 2) // fanout + 1
    role = "root" if index == 0 else ("leaf" if index >= first_leaf else "internal")
    return {"tree_id": tree_id, "schedule": tuple(members), "replica_1_parent": parent,
            "replica_1_role": role,
            "wait_exempt": tuple(wait_exempt)}


def _verify_epoch0_metadata(epoch_input: Mapping[str, Any], parsed_trees: Sequence[Sequence[int]], tree_sha256: str) -> None:
    """Require the presentation metadata to agree with validated launch bytes."""
    trees = epoch_input.get("epoch0_trees")
    if (epoch_input.get("replica_count") != 7 or epoch_input.get("quorum") != 5 or
            not isinstance(trees, list) or len(trees) != 7):
        raise PlotError("Epoch-0 metadata is not bound to the validated tree file")
    expected = tuple(tuple(tree) for tree in parsed_trees)
    actual: list[tuple[int, ...]] = []
    for tree_id, tree in enumerate(trees):
        if (not isinstance(tree, Mapping) or tree.get("tree_id") != tree_id or tree.get("fanout") != 2 or
                tree.get("pipeline_depth") != 2 or tree.get("wait_exempt") != [] or
                not isinstance(tree.get("members_breadth_first"), list)):
            raise PlotError("Epoch-0 metadata schema drifted")
        actual.append(tuple(tree["members_breadth_first"]))
    if tuple(actual) != expected:
        raise PlotError("Epoch-0 metadata differs from validated tree file")


def rebuild(archive: Path) -> Mapping[str, Any]:
    """Replay C-033, decode its signed E1 bundle, and retain safe projections."""
    archive = archive.resolve()
    if archive.name != ARCHIVE_NAME or archive.is_symlink() or not archive.is_dir():
        raise PlotError("input must be the accepted C-033 archive root")
    receipt_path, verdict_path = archive / "raw-bundle-receipt.json", archive / "raw-bundle-verdict.json"
    tree_path, epoch_input_path = archive / "config/epoch0.tree", archive / "runtime/epoch-input.json"
    issuer_path, wire_path = archive / "runtime/issuer-public-key.txt", archive / "transitions/e0-to-e1-containment/successor.bundle"
    receipt_bytes = _read_regular_bytes(receipt_path, "raw-bundle receipt", 256 * 1024)
    verdict_bytes = _read_regular_bytes(verdict_path, "raw-bundle verdict", 256 * 1024)
    tree_bytes = _read_regular_bytes(tree_path, "Epoch-0 tree", 256 * 1024)
    epoch_input_bytes = _read_regular_bytes(epoch_input_path, "epoch input", 256 * 1024)
    issuer_bytes = _read_regular_bytes(issuer_path, "issuer public key", 256 * 1024)
    wire_bytes = _read_regular_bytes(wire_path, "signed Epoch-1 bundle", 256 * 1024)
    receipt, stored_verdict = _json_bytes(receipt_bytes, "raw-bundle receipt"), _json_bytes(verdict_bytes, "raw-bundle verdict")
    receipt_sha256, verdict_sha256 = hashlib.sha256(receipt_bytes).hexdigest(), hashlib.sha256(verdict_bytes).hexdigest()
    tree_sha256, epoch_input_sha256 = hashlib.sha256(tree_bytes).hexdigest(), hashlib.sha256(epoch_input_bytes).hexdigest()
    issuer_sha256, wire_sha256 = hashlib.sha256(issuer_bytes).hexdigest(), hashlib.sha256(wire_bytes).hexdigest()
    if (receipt.get("run_id") != EXPECTED_RUN_ID or stored_verdict.get("verdict") != "RAW_BUNDLE_VALIDATED" or
            receipt_sha256 != SEALED_RECEIPT_SHA256 or verdict_sha256 != SEALED_VERDICT_SHA256):
        raise PlotError("archive does not identify the accepted C-033 raw bundle")
    validator = _validator()
    try:
        replayed = validator.validate_raw_bundle(archive, receipt)
    except Exception as error:  # validator exposes a scenario-local error type
        raise PlotError("C-033 raw-bundle replay failed") from error
    if (replayed.get("verdict") != "RAW_BUNDLE_VALIDATED" or replayed.get("e1_bundle_sha256") != wire_sha256 or
            stored_verdict.get("e1_bundle_sha256") != wire_sha256):
        raise PlotError("replayed validator verdict differs from sealed C-033 verdict")
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, Mapping) or not isinstance(artifacts.get("epoch0_tree"), Mapping):
        raise PlotError("receipt lacks the validated Epoch-0 tree binding")
    if artifacts["epoch0_tree"].get("path") != "config/epoch0.tree" or artifacts["epoch0_tree"].get("sha256") != tree_sha256:
        raise PlotError("validated Epoch-0 tree bytes differ from the receipt")
    try:
        with tempfile.TemporaryDirectory(prefix="kauri-c033-tree-") as temporary:
            captured_tree = Path(temporary) / "epoch0.tree"
            captured_tree.write_bytes(tree_bytes)
            parsed_e0 = validator.runner.parse_tree_file(captured_tree)
    except Exception as error:
        raise PlotError("cannot parse the validated Epoch-0 tree file") from error
    epoch_input = _json_bytes(epoch_input_bytes, "epoch input")
    _verify_epoch0_metadata(epoch_input, parsed_e0, tree_sha256)
    e0_rows = [_project_tree(tree_id, members, (), 2) for tree_id, members in enumerate(parsed_e0)]
    if tuple(row["tree_id"] for row in e0_rows) != tuple(range(7)):
        raise PlotError("Epoch-0 tree IDs drifted")
    from kauri_experiment import factorial_validation
    try:
        issuer = issuer_bytes.decode("ascii").strip()
        bundle = factorial_validation.decode_epoch_change_bundle(wire_bytes, issuer_public_key=issuer)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise PlotError("cannot decode the signed Epoch-1 bundle") from error
    selected = tuple(replayed.get("selection_replicas", ()))
    if selected != (1,) or bundle.command.successor_epoch_number != 1 or len(bundle.trees) != 5:
        raise PlotError("signed bundle is not the accepted replica-1 containment successor")
    e1_rows = [_project_tree(tree.tree_id, tree.members, tree.wait_exempt, tree.fanout) for tree in bundle.trees]
    if any(row["replica_1_role"] != "leaf" or row["wait_exempt"] != (1,) for row in e1_rows):
        raise PlotError("signed Epoch-1 bundle does not contain replica 1 as wait-exempt leaf in every tree")
    return {"e0": e0_rows, "e1": e1_rows, "e1_bundle_sha256": wire_sha256,
            "receipt_sha256": receipt_sha256, "verdict_sha256": verdict_sha256,
            "epoch0_tree_sha256": tree_sha256, "epoch_input_sha256": epoch_input_sha256,
            "issuer_sha256": issuer_sha256, "replay_validator_source_sha256": _sha256(SCENARIO_ROOT / "validator.py"),
            "decoder_source_sha256": _sha256(Path(factorial_validation.__file__)), "replay_repository_revision": _replay_revision()}


def _build_figure(data: Mapping[str, Any]):
    try:
        import matplotlib.pyplot as plt
        from matplotlib.patches import FancyArrowPatch
    except ImportError as error:
        raise PlotError("matplotlib is required") from error
    figure = plt.figure(figsize=(11.0, 8.5))
    grid = figure.add_gridspec(2, 1, height_ratios=(1.65, 1.0), hspace=0.33)
    axis = figure.add_subplot(grid[0]); axis.axis("off")
    rows = []
    for epoch, trees in (("E0", data["e0"]), ("E1", data["e1"])):
        for tree in trees:
            marked = epoch == "E0" and tree["tree_id"] in (4, 5, 6)
            rows.append([epoch, f"T{tree['tree_id']}", " ".join(f"r{x}" for x in tree["schedule"]),
                         "r1 → r" + str(tree["replica_1_parent"]) if marked else f"r1: {tree['replica_1_role']}",
                         "wait-exempt r1" if epoch == "E1" else ("omission path" if marked else "—")])
    table = axis.table(cellText=rows, colLabels=["Epoch", "Tree", "breadth-first replica schedule", "r1 role / path", "evidence role"],
                       cellLoc="left", colLoc="left", loc="center", colWidths=[.08,.07,.39,.22,.20])
    table.auto_set_font_size(False); table.set_fontsize(8.4); table.scale(1, 1.40)
    for (row, col), cell in table.get_celld().items():
        cell.set_edgecolor("#cbd5e1")
        if row == 0: cell.set_facecolor("#0f172a"); cell.get_text().set_color("white"); cell.get_text().set_weight("bold")
        elif row in (5, 6, 7): cell.set_facecolor("#fff1f2")
        elif row >= 8: cell.set_facecolor("#ecfdf5")
    figure.text(.5, .936, "Exact schedule projection: Epoch 0 has 7 trees; signed Epoch 1 has 5 containment trees", ha="center", fontsize=10.5, fontweight="bold")
    lower = grid[1].subgridspec(1, 2, wspace=.23)
    tree_axis = figure.add_subplot(lower[0]); tree_axis.axis("off"); tree_axis.set_title("Representative E0 T4 omission path", fontsize=10, fontweight="bold")
    positions = {4:(.5,.87),1:(.28,.55),5:(.72,.55),0:(.13,.20),2:(.39,.20),3:(.61,.20),6:(.87,.20)}
    for child, parent in ((1,4),(5,4),(0,1),(2,1),(3,5),(6,5)):
        color = "#dc2626" if (child,parent)==(1,4) else "#94a3b8"
        tree_axis.add_patch(FancyArrowPatch(positions[child],positions[parent],arrowstyle="->",mutation_scale=11,color=color,linewidth=2 if color=="#dc2626" else 1.1))
    for replica, (x,y) in positions.items():
        tree_axis.text(x,y,f"r{replica}",ha="center",va="center",fontsize=10,weight="bold" if replica==1 else "normal",color="#991b1b" if replica==1 else "#0f172a",bbox={"boxstyle":"circle,pad=.36","fc":"#fee2e2" if replica==1 else "#f8fafc","ec":"#dc2626" if replica==1 else "#64748b"})
    tree_axis.text(.5,.03,"T4 exact path: r1 → r4 (one of three declared internal paths)",ha="center",fontsize=8.5,color="#991b1b")
    note = figure.add_subplot(lower[1]); note.axis("off")
    note.text(.03,.84,"Validated C-033 transition",fontsize=11,fontweight="bold",color="#065f46")
    note.text(.03,.66,"• E0: r1 internal on T4, T5, T6; paths r1→r4, r1→r5, r1→r6.\n• E1: decoded signed bundle places r1 as a wait-exempt leaf\n  in all five trees.\n• Raw-bundle validator replayed before rendering.",fontsize=9.1,va="top",linespacing=1.45)
    note.text(.03,.12,"Boundary: schedule/role explanation only — no throughput implication.",fontsize=8.5,color="#475569",wrap=True)
    figure.suptitle("C-033 selective path-local omission and containment at N=7",fontsize=14,fontweight="bold",y=.985)
    figure.text(.5,.012,FIGURE_BOUNDARY,ha="center",fontsize=7.5,color="#475569",wrap=True)
    figure.subplots_adjust(left=.045,right=.975,top=.89,bottom=.07)
    return figure


def render(archive: Path, output_png: Path, output_pdf: Path) -> Mapping[str, Any]:
    data = rebuild(archive)
    output_png.parent.mkdir(parents=True, exist_ok=True); output_pdf.parent.mkdir(parents=True, exist_ok=True)
    figure = _build_figure(data); figure.savefig(output_png, dpi=220, metadata={"Software":"Kauri C-033 evidence renderer"}); figure.savefig(output_pdf, metadata=PDF_METADATA)
    import matplotlib.pyplot as plt
    plt.close(figure)
    manifest = {"schema_version":1,"kind":"kauri-c033-n7-omission-epoch-figure-manifest-v1","archive_id":ARCHIVE_NAME,"evidence_execution_revision":EVIDENCE_REVISION,"replay_repository_revision":data["replay_repository_revision"],"raw_bundle_verdict":"RAW_BUNDLE_VALIDATED","figure_boundary":FIGURE_BOUNDARY,"input_sha256":{key:data[key] for key in ("receipt_sha256","verdict_sha256","epoch0_tree_sha256","epoch_input_sha256","e1_bundle_sha256")},"generator_source_sha256":_sha256(Path(__file__)),"replay_validator_source_sha256":data["replay_validator_source_sha256"],"decoder_source_sha256":data["decoder_source_sha256"],"outputs":{path.name:{"sha256":_sha256(path),"size_bytes":path.stat().st_size} for path in (output_png,output_pdf)}}
    manifest_path = output_png.parent / "figure-manifest.json"; manifest_path.write_bytes(json.dumps(manifest,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()+b"\n")
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--archive",type=Path,required=True); parser.add_argument("--output-png",type=Path,required=True); parser.add_argument("--output-pdf",type=Path,required=True); args=parser.parse_args(argv)
    try: render(args.archive,args.output_png,args.output_pdf)
    except PlotError as error: parser.error(str(error))
    return 0

if __name__ == "__main__": raise SystemExit(main())
