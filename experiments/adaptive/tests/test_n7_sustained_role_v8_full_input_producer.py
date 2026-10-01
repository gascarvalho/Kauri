from __future__ import annotations
import importlib.util
from pathlib import Path
import pytest
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1]; P=ROOT/"n7-path-timeout-quorum"/"sustained_role_v8_full_input_producer.py"
s=importlib.util.spec_from_file_location("v8full",P);assert s and s.loader; subject=importlib.util.module_from_spec(s);s.loader.exec_module(subject)
def _bins():
 build=ROOT.parents[1]/"build-adaptive"
 return {"app_binary":build/"examples/hotstuff-app","manager_binary":build/"examples/adaptation-manager","keygen_binary":build/"hotstuff-keygen","tls_keygen_binary":build/"hotstuff-tls-keygen","e0_helper_binary":build/"examples/n7-epoch0-treefile-digest"}
def _prepared(tmp_path, *, arm="adaptive_e1"):
 bins=_bins()
 if not all(x.is_file() for x in bins.values()): pytest.skip("binaries absent")
 receipt=tmp_path/"receipt"; receipt.write_text("fixture\n")
 out=subject.prepare(tmp_path/"root",run_id="fixture",ports=(18471,19471,20471),start_ns=1,end_ns=82_000_000_001,binaries=bins,build_receipt=receipt,arm=arm,repository_verifier=lambda _:SimpleNamespace(revision="a"*40,worktree_clean=True))
 return tmp_path/"root",out,bins
def _seal(root,plan):
 import hashlib, json
 plan["plan_sha256"]=hashlib.sha256(subject._canon({k:v for k,v in plan.items() if k!="plan_sha256"})).hexdigest()
 request={"schema_version":1,"kind":"kauri-n7-sustained-role-v8-full-input-request-v1","plan_sha256":plan["plan_sha256"],"run_id":plan["run_id"],"no_launch":True,"no_retry":True}
 (root/subject.PLAN).write_bytes(subject._canon(plan)); (root/subject.REQUEST).write_bytes(subject._canon(request))
 return hashlib.sha256(subject._canon(request)).hexdigest()
def _seal_launch(root,plan,launch):
 import hashlib
 path=root/plan["artifacts"]["launch_arguments"]["path"]; path.write_bytes(subject._canon(launch))
 plan["artifacts"]["launch_arguments"]={"path":plan["artifacts"]["launch_arguments"]["path"],"size_bytes":len(path.read_bytes()),"sha256":hashlib.sha256(path.read_bytes()).hexdigest()}
 return _seal(root,plan)
def test_dirty_repository_fails_before_root_creation(tmp_path):
 with pytest.raises(subject.FullInputError,match="clean pushed"):
  subject.prepare(tmp_path/"root",run_id="x",ports=(1,2,3),start_ns=1,end_ns=82_000_000_001,binaries={},build_receipt=tmp_path/"no",repository_verifier=lambda _: (_ for _ in ()).throw(RuntimeError()))
 assert not (tmp_path/"root").exists()

def test_fixture_only_clean_snapshot_reaches_no_launch_root(tmp_path):
 root,out,bins=_prepared(tmp_path)
 assert len(out["plan_sha256"])==64
 import json
 plan=json.loads((root/subject.PLAN).read_text()); assert plan["state"]=="PREPARED_INPUTS_NO_LAUNCH" and "hotstuff_app" in plan["artifacts"]
 assert {"hotstuff_keygen", "hotstuff_tls_keygen"} <= set(plan["artifacts"])
 for key, binary_key in (("hotstuff_keygen", "keygen_binary"), ("hotstuff_tls_keygen", "tls_keygen_binary")):
  archived=root/plan["artifacts"][key]["path"]
  assert archived.read_bytes()==bins[binary_key].read_bytes()
 assert subject.verify(root,expected_request_sha256=out["request_sha256"])["plan_sha256"]==out["plan_sha256"]
 config=root/plan["artifacts"]["replica_configs"][0]["path"]; config.write_text("mutated\n")
 with pytest.raises(subject.FullInputError,match="descriptor"): subject.verify(root,expected_request_sha256=out["request_sha256"])


def test_fixed_e0_full_input_reopens_exact_native_control(tmp_path):
 import json
 root,out,_bins_value=_prepared(tmp_path,arm="fixed_e0")
 plan=json.loads((root/subject.FIXED_PLAN).read_text())
 assert plan["arm"]=="fixed_e0" and plan["kind"]==subject._FIXED_PLAN_KIND
 assert not (root/subject.PLAN).exists()
 assert subject.verify(root,expected_request_sha256=out["request_sha256"],arm="fixed_e0")["plan_sha256"]==out["plan_sha256"]
 with pytest.raises(subject.FullInputError):
  subject.verify(root,expected_request_sha256=out["request_sha256"])


def test_fixed_e0_full_input_rejects_self_consistent_native_control_drift(tmp_path):
 import hashlib,json
 root,_out,_bins_value=_prepared(tmp_path,arm="fixed_e0")
 plan=json.loads((root/subject.FIXED_PLAN).read_text())
 launch_path=root/plan["artifacts"]["launch_arguments"]["path"]
 launch=json.loads(launch_path.read_text())
 manager=next(row for row in launch["processes"] if row["source_id"]=="adaptive-manager")
 argv=manager["argv"]
 argv[argv.index("--scheduled-fixed-e0-window-end-monotonic-ns")+1]="82"
 process=next(row for row in plan["processes"] if row["source_id"]=="adaptive-manager")
 process["argv"]=argv
 process["argv_sha256"]=hashlib.sha256(subject._canon(argv)).hexdigest()
 launch_path.write_bytes(subject._canon(launch))
 plan["artifacts"]["launch_arguments"]=subject._desc(root,launch_path)
 plan["plan_sha256"]=hashlib.sha256(subject._canon({key:value for key,value in plan.items() if key!="plan_sha256"})).hexdigest()
 (root/subject.FIXED_PLAN).write_bytes(subject._canon(plan))
 request=json.loads((root/subject.FIXED_REQUEST).read_text())
 request["plan_sha256"]=plan["plan_sha256"]
 request_raw=subject._canon(request)
 (root/subject.FIXED_REQUEST).write_bytes(request_raw)
 with pytest.raises(subject.FullInputError,match="fixed-E0 native scheduled-control"):
  subject.verify(root,expected_request_sha256=hashlib.sha256(request_raw).hexdigest(),arm="fixed_e0")

def test_rejects_native_zero_start_and_symlink_root(tmp_path):
 with pytest.raises(subject.FullInputError,match="window"):
  subject.prepare(tmp_path/"zero",run_id="x",ports=(1,2,3),start_ns=0,end_ns=82_000_000_000,binaries={},build_receipt=tmp_path/"none",repository_verifier=lambda _:SimpleNamespace(revision="a"*40,worktree_clean=True))
 assert not (tmp_path/"zero").exists()
 target=tmp_path/"target"; alias=tmp_path/"alias"; alias.symlink_to(target)
 with pytest.raises(subject.FullInputError,match="root"):
  subject.prepare(alias,run_id="x",ports=(1,2,3),start_ns=1,end_ns=82_000_000_001,binaries={},build_receipt=tmp_path/"none",repository_verifier=lambda _:SimpleNamespace(revision="a"*40,worktree_clean=True))

@pytest.mark.parametrize("mutation,match", [("artifact","artifact membership"),("instance","source instance")])
def test_rejects_self_consistent_forged_plan(tmp_path,mutation,match):
 import json
 root,_out,_bins_value=_prepared(tmp_path); plan=json.loads((root/subject.PLAN).read_text())
 if mutation=="artifact": plan["artifacts"].pop("e0_helper")
 else: plan["processes"][1]["source_instance"]=plan["processes"][0]["source_instance"]
 digest=_seal(root,plan)
 with pytest.raises(subject.FullInputError,match=match): subject.verify(root,expected_request_sha256=digest)

def test_rejects_self_consistent_forged_launch_metadata(tmp_path):
 import hashlib,json
 root,_out,_bins_value=_prepared(tmp_path); plan=json.loads((root/subject.PLAN).read_text())
 launch_path=root/plan["artifacts"]["launch_arguments"]["path"]; launch=json.loads(launch_path.read_text())
 row=next(x for x in launch["processes"] if x["source_id"]=="replica-0")
 index=row["argv"].index("--conf"); del row["argv"][index:index+2]
 plan_row=next(x for x in plan["processes"] if x["source_id"]=="replica-0"); plan_row["argv"]=row["argv"]; plan_row["argv_sha256"]=hashlib.sha256(subject._canon(row["argv"])).hexdigest()
 digest=_seal_launch(root,plan,launch)
 with pytest.raises(subject.FullInputError,match="replica argv semantics"): subject.verify(root,expected_request_sha256=digest)

@pytest.mark.parametrize("source_id,replacement,match", [("adaptive-manager","hotstuff_app","manager executable"),("replica-0","adaptation_manager","replica executable")])
def test_rejects_self_consistent_swapped_executables(tmp_path,source_id,replacement,match):
 import hashlib,json
 root,_out,_bins_value=_prepared(tmp_path); plan=json.loads((root/subject.PLAN).read_text())
 launch=json.loads((root/plan["artifacts"]["launch_arguments"]["path"]).read_text())
 row=next(x for x in launch["processes"] if x["source_id"]==source_id); row["argv"][0]=str(root/plan["artifacts"][replacement]["path"])
 plan_row=next(x for x in plan["processes"] if x["source_id"]==source_id); plan_row["argv"]=row["argv"]; plan_row["argv_sha256"]=hashlib.sha256(subject._canon(row["argv"])).hexdigest()
 digest=_seal_launch(root,plan,launch)
 with pytest.raises(subject.FullInputError,match=match): subject.verify(root,expected_request_sha256=digest)

def test_rejects_self_consistent_missing_manager_replica_endpoints(tmp_path):
 import hashlib,json
 root,_out,_bins_value=_prepared(tmp_path); plan=json.loads((root/subject.PLAN).read_text()); launch=json.loads((root/plan["artifacts"]["launch_arguments"]["path"]).read_text())
 row=next(x for x in launch["processes"] if x["source_id"]=="adaptive-manager")
 argv=row["argv"]
 while "--replica" in argv:
  index=argv.index("--replica"); del argv[index:index+2]
 plan_row=next(x for x in plan["processes"] if x["source_id"]=="adaptive-manager"); plan_row["argv"]=argv; plan_row["argv_sha256"]=hashlib.sha256(subject._canon(argv)).hexdigest()
 digest=_seal_launch(root,plan,launch)
 with pytest.raises(subject.FullInputError,match="endpoint cardinality"): subject.verify(root,expected_request_sha256=digest)

def test_rejects_self_consistent_main_config_consensus_drift(tmp_path):
 import hashlib,json
 root,_out,_bins_value=_prepared(tmp_path); plan=json.loads((root/subject.PLAN).read_text()); main=root/plan["artifacts"]["main_config"]["path"]
 main.write_bytes(main.read_bytes().replace(b"aggregation-timeout = 0.5\n",b"aggregation-timeout = 999\n"))
 plan["artifacts"]["main_config"]={"path":plan["artifacts"]["main_config"]["path"],"size_bytes":len(main.read_bytes()),"sha256":hashlib.sha256(main.read_bytes()).hexdigest()}
 launch=json.loads((root/plan["artifacts"]["launch_arguments"]["path"]).read_text())
 for row in launch["processes"]:
  if row["source_kind"]=="replica": row["effective_options"]["main_config_sha256"]=plan["artifacts"]["main_config"]["sha256"]
 digest=_seal_launch(root,plan,launch)
 with pytest.raises(subject.FullInputError,match="frozen materialized consensus"): subject.verify(root,expected_request_sha256=digest)

@pytest.mark.parametrize("target", ["main", "replica"])
def test_rejects_self_consistent_unknown_config_key(tmp_path,target):
 import hashlib,json
 root,_out,_bins_value=_prepared(tmp_path); plan=json.loads((root/subject.PLAN).read_text()); launch=json.loads((root/plan["artifacts"]["launch_arguments"]["path"]).read_text())
 if target=="main":
  descriptor=plan["artifacts"]["main_config"]; config=root/descriptor["path"]
  config.write_bytes(config.read_bytes()+b"consensus-quorum = 1\n")
  descriptor.update({"size_bytes":len(config.read_bytes()),"sha256":hashlib.sha256(config.read_bytes()).hexdigest()})
  for row in launch["processes"]:
   if row["source_kind"]=="replica": row["effective_options"]["main_config_sha256"]=descriptor["sha256"]
 else:
  descriptor=plan["artifacts"]["replica_configs"][0]; config=root/descriptor["path"]
  config.write_bytes(config.read_bytes()+b"consensus-quorum = 1\n")
  descriptor.update({"size_bytes":len(config.read_bytes()),"sha256":hashlib.sha256(config.read_bytes()).hexdigest()})
  row=next(item for item in launch["processes"] if item["source_id"]=="replica-0"); row["effective_options"]["replica_config_sha256"]=descriptor["sha256"]
 digest=_seal_launch(root,plan,launch)
 with pytest.raises(subject.FullInputError,match="unknown key"): subject.verify(root,expected_request_sha256=digest)
