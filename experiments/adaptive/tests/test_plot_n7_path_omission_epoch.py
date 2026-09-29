"""Focused contracts for the C-033 schedule/role figure renderer."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import pytest
from experiments.adaptive import plot_n7_path_omission_epoch as plot

def test_boundary_and_pdf_metadata_are_claim_safe_and_clock_free() -> None:
    assert "no throughput implication" in plot.FIGURE_BOUNDARY
    assert plot.PDF_METADATA["CreationDate"] is None and plot.PDF_METADATA["ModDate"] is None

def test_project_tree_derives_r1_parent_and_leaf_role() -> None:
    assert plot._project_tree(4,(4,1,5,0,2,3,6),(),2)["replica_1_parent"] == 4
    assert plot._project_tree(0,(0,2,3,4,5,6,1),(1,),2)["replica_1_role"] == "leaf"

def test_exact_e0_and_e1_replica_1_roles() -> None:
    e0 = (
        (0, (0,2,3,1,4,5,6), "leaf"), (1, (1,2,3,4,5,6,0), "root"),
        (2, (2,3,4,0,1,5,6), "leaf"), (3, (3,4,5,6,0,1,2), "leaf"),
        (4, (4,1,5,0,2,3,6), "internal"), (5, (5,1,6,0,2,3,4), "internal"),
        (6, (6,1,0,2,3,4,5), "internal"),
    )
    for tree_id, schedule, expected_role in e0:
        assert plot._project_tree(tree_id, schedule, (), 2)["replica_1_role"] == expected_role
    e1 = ((0, (0,2,3,6,5,4,1)), (1, (5,0,4,3,2,6,1)), (2, (2,0,3,1,6,5,4)),
          (3, (3,2,4,5,6,1,0)), (4, (4,5,6,2,0,3,1)))
    for tree_id, schedule in e1:
        row = plot._project_tree(tree_id, schedule, (1,), 2)
        assert row["replica_1_role"] == "leaf" and row["wait_exempt"] == (1,)

def test_project_tree_rejects_non_frozen_topology() -> None:
    with pytest.raises(plot.PlotError, match="frozen N=7"):
        plot._project_tree(0,(0,1),(),2)

def test_epoch0_metadata_rejects_stale_schedule_despite_valid_tree_bytes() -> None:
    parsed = ((0,2,3,1,4,5,6), (1,2,3,4,5,6,0), (2,3,4,0,1,5,6),
              (3,4,5,6,0,1,2), (4,1,5,0,2,3,6), (5,1,6,0,2,3,4), (6,1,0,2,3,4,5))
    metadata = {"replica_count": 7, "quorum": 5,
                "epoch0_trees": [{"tree_id": index, "fanout": 2, "pipeline_depth": 2,
                                  "wait_exempt": [], "members_breadth_first": list(tree)} for index, tree in enumerate(parsed)]}
    plot._verify_epoch0_metadata(metadata, parsed, "a" * 64)
    metadata["epoch0_trees"][0]["members_breadth_first"] = list(reversed(parsed[0]))
    with pytest.raises(plot.PlotError, match="differs from validated tree file"):
        plot._verify_epoch0_metadata(metadata, parsed, "a" * 64)

def test_rebuild_refuses_unaccepted_archive_name(tmp_path: Path) -> None:
    with pytest.raises(plot.PlotError, match="accepted C-033 archive root"):
        plot.rebuild(tmp_path)

def test_secure_capture_refuses_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"; target.write_bytes(b"captured")
    link = tmp_path / "link"; link.symlink_to(target)
    with pytest.raises(plot.PlotError, match="cannot securely read"):
        plot._read_regular_bytes(link, "test input", 64)

def test_secure_capture_refuses_fifo_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "input.fifo"; os.mkfifo(fifo)
    with pytest.raises(plot.PlotError, match="bounded regular file"):
        plot._read_regular_bytes(fifo, "test input", 64)

def test_validator_loader_uses_the_exact_scenario_file() -> None:
    assert Path(plot._validator().__file__).resolve() == plot.SCENARIO_ROOT / "validator.py"

def test_render_writes_only_safe_hash_bindings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data={"e0":[plot._project_tree(i,tuple([i]+[x for x in range(7) if x!=i]),(),2) for i in range(7)],"e1":[plot._project_tree(i,(0,2,3,4,5,6,1),(1,),2) for i in range(5)],"receipt_sha256":"a"*64,"verdict_sha256":"b"*64,"epoch0_tree_sha256":"c"*64,"epoch_input_sha256":"d"*64,"e1_bundle_sha256":"e"*64,"replay_validator_source_sha256":"f"*64,"decoder_source_sha256":"0"*64,"replay_repository_revision":"1"*40}
    monkeypatch.setattr(plot,"rebuild",lambda _archive:data)
    png,pdf=tmp_path/"figure.png",tmp_path/"figure.pdf"; manifest=plot.render(tmp_path/plot.ARCHIVE_NAME,png,pdf)
    text=(tmp_path/"figure-manifest.json").read_text(); loaded=json.loads(text)
    assert loaded["input_sha256"]["epoch0_tree_sha256"] == "c"*64
    assert loaded["input_sha256"]["e1_bundle_sha256"] == "e"*64
    assert loaded["evidence_execution_revision"] == plot.EVIDENCE_REVISION
    assert loaded["replay_repository_revision"] == "1" * 40
    assert loaded["replay_validator_source_sha256"] == "f" * 64
    assert "issuer" not in text.lower() and "identity" not in text.lower() and "/Users/" not in text
    assert loaded["outputs"]["figure.png"]["sha256"] == hashlib.sha256(png.read_bytes()).hexdigest()
    assert manifest == loaded
