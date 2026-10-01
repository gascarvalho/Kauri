from __future__ import annotations
import importlib.util, json
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]; PATH=ROOT/"n7-path-timeout-quorum"/"sustained_role_v8_preparation.py"
spec=importlib.util.spec_from_file_location("w19_v8_prep",PATH); assert spec and spec.loader
subject=importlib.util.module_from_spec(spec); spec.loader.exec_module(subject)
def _bins():
    build=ROOT.parents[1]/"build-adaptive"; out={"app_binary":build/"examples/hotstuff-app","manager_binary":build/"examples/adaptation-manager","keygen_binary":build/"hotstuff-keygen","tls_keygen_binary":build/"hotstuff-tls-keygen","e0_helper_binary":build/"examples/n7-epoch0-treefile-digest"}
    if not all(x.is_file() for x in out.values()): pytest.skip("local preparation binaries unavailable")
    return out
def test_real_preparation_updates_actor_argv_and_config_metadata(tmp_path):
    result=subject.prepare_inputs(tmp_path/"root",run_id="v8-prep",ports=(18371,19371,20371),binaries=_bins(),scheduled_start_monotonic_ns=1_000_000_000,scheduled_end_monotonic_ns=83_000_000_000)
    data=json.loads((tmp_path/"root/runtime/launch-arguments.json").read_text()); rows={x["source_id"]:x for x in data["processes"] if x["source_kind"]=="replica"}
    overlay=subject.v8.native_actor_overlay(scheduled_start_monotonic_ns=1_000_000_000,scheduled_end_monotonic_ns=83_000_000_000)
    assert rows["replica-1"]["argv"][-len(overlay):]==list(overlay)
    assert all(row["effective_options"]["replica_config_sha256"] == __import__("hashlib").sha256(result["replica_configs"][int(row["source_id"].split("-")[1])].read_bytes()).hexdigest() for row in rows.values())
    manager=next(x for x in data["processes"] if x["source_kind"]=="adaptation_manager")
    assert manager["argv"]==subject.local.base.normalized_manager_argv(result["manager_command"])
    persisted=(tmp_path/"root/runtime/launch-arguments.json").read_bytes()
    for option, marker in (("--tls-privkey", "<redacted>"),
                           ("--issuer-private-key", "<redacted>"),
                           ("--tls-cert", "<fingerprinted>")):
        index=result["manager_command"].index(option)
        original=result["manager_command"][index+1]
        assert manager["argv"][manager["argv"].index(option)+1] == marker
        assert original.encode() not in persisted
    assert not (tmp_path/"root"/subject.local.PLAN).exists() and not (tmp_path/"root"/subject.local.REQUEST).exists()
    assert json.loads((tmp_path/"root/runtime/sustained-role-v8-preparation.json").read_text())["state"]=="MATERIALIZATION_COMPONENT_ONLY_NOT_LAUNCH_ELIGIBLE"
    assert len(result["replica_configs"])==7
    state=json.loads((tmp_path/"root/runtime/sustained-role-v8-preparation.json").read_text())
    selection=tmp_path/"root"/state["inherited_selection_profile"]["path"]
    assert selection.is_file() and state["inherited_selection_profile"]["sha256"]==__import__("hashlib").sha256(selection.read_bytes()).hexdigest()


def test_fixed_e0_preparation_uses_native_scheduled_control_without_successor(tmp_path):
    root=tmp_path/"fixed"
    result=subject.prepare_inputs(root,run_id="v8-fixed",ports=(18372,19372,20372),
                                  binaries=_bins(),scheduled_start_monotonic_ns=1_000_000_000,
                                  scheduled_end_monotonic_ns=83_000_000_000,arm="fixed_e0")
    argv=result["manager_command"]
    assert "--scheduled-fixed-e0-control" in argv
    assert "--fault-window-arm-control-only" not in argv
    assert "--transition-request" not in argv and "--bundle-output" not in argv
    assert all(not option.startswith("--fault-window-arm-") for option in argv)
    profile=(root/"runtime/sustained-role-v8-materialization-profile.json").read_bytes()
    digest=json.loads((root/"runtime/e0-identity-receipt.json").read_text())["epoch_digest"]
    assert argv[argv.index("--scheduled-fixed-e0-profile-sha256")+1]==__import__("hashlib").sha256(profile).hexdigest()
    assert argv[argv.index("--scheduled-fixed-e0-epoch-zero-digest")+1]==digest
    launch=json.loads((root/"runtime/launch-arguments.json").read_text())
    manager=next(row for row in launch["processes"] if row["source_id"]=="adaptive-manager")
    assert manager["argv"]==subject.local.base.normalized_manager_argv(argv)
def test_requires_fresh_root_and_frozen_overlay(tmp_path):
    with pytest.raises(subject.V8PreparationError): subject.prepare_inputs(tmp_path/"root",run_id="x",ports=(1,2,3),binaries={},scheduled_start_monotonic_ns=0,scheduled_end_monotonic_ns=1)
    with pytest.raises(TypeError): subject.prepare_inputs(tmp_path/"other",run_id="x",ports=(1,2,3),binaries={},scheduled_start_monotonic_ns=0,scheduled_end_monotonic_ns=82_000_000_000,overlay=("generic",))

def test_rejects_mutated_e0_actor_role_map(tmp_path):
    tree=tmp_path/"tree"; tree.write_text("fan:2 pipe:2 0 1 2 3 4 5 6\n"*7)
    with pytest.raises(subject.V8PreparationError,match="role map"): subject._roles(tree)
