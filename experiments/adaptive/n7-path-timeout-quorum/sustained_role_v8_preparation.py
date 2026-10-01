"""Real-input v8 preparation seam; deliberately stops before v8 serialization or launch."""
from __future__ import annotations
import hashlib, importlib.util, json, os, tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

HERE = Path(__file__).resolve().parent
def _load(name: str, path: Path):
    spec=importlib.util.spec_from_file_location(name,path); assert spec and spec.loader
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module
local=_load("w19_v8_preparation_local", HERE / "sustained_role_local.py")
v8=_load("w19_v8_preparation_profile", HERE / "sustained_role_v8_profile.py")

class V8PreparationError(ValueError): pass

def _sha(path: Path) -> str: return hashlib.sha256(path.read_bytes()).hexdigest()

def _roles(tree: Path) -> None:
    try: trees=local.tree_runner.parse_tree_file(tree)
    except Exception as exc: raise V8PreparationError("E0 actor-1 role map drifted") from exc
    if len(trees)!=7 or any(1 not in row for row in trees): raise V8PreparationError("invalid E0 tree membership")
    roots=tuple(i for i,row in enumerate(trees) if row.index(1)==0)
    internal=tuple(i for i,row in enumerate(trees) if 0 < row.index(1) < 3)
    leaves=tuple(i for i,row in enumerate(trees) if row.index(1)>=3)
    if (roots,internal,leaves)!=((1,),(4,5,6),(0,2,3)): raise V8PreparationError("E0 actor-1 role map drifted")

def _atomic(path: Path, raw: bytes) -> None:
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix=".v8-")
    with os.fdopen(fd,"wb") as out: out.write(raw); out.flush(); os.fsync(out.fileno())
    os.replace(tmp,path)

def prepare_inputs(root: Path, *, run_id: str, ports: tuple[int,int,int], binaries: Mapping[str,Path],
                   scheduled_start_monotonic_ns: int, scheduled_end_monotonic_ns: int,
                   arm: str = "adaptive_e1") -> Mapping[str, Any]:
    """Run only v7's preparation generators, then make metadata match final bytes.

    The actor overlay comes only from the frozen v8 profile.  This seam still
    refuses to serialize a v8 plan or authorize a process.
    """
    if root.exists() or not root.is_absolute(): raise V8PreparationError("fresh absolute root required")
    if arm not in {"adaptive_e1", "fixed_e0"}: raise V8PreparationError("v8 arm is invalid")
    try: overlay=v8.native_actor_overlay(scheduled_start_monotonic_ns=scheduled_start_monotonic_ns, scheduled_end_monotonic_ns=scheduled_end_monotonic_ns)
    except v8.V8ProfileError as exc: raise V8PreparationError(str(exc)) from exc
    root.mkdir(mode=0o700)
    try:
        made=local.materialize_runtime_inputs(root, arm=arm, run_id=run_id,
            peer_port=ports[0], client_port=ports[1], manager_port=ports[2], hard_timeout_seconds=210,
            materialization_profile=v8.materialization_profile(), **dict(binaries))
        manager=tuple(made["manager_command"])
        if "--convergence-deadline-seconds" in manager: raise V8PreparationError("non-v8 convergence override")
        profile_raw=json.dumps(v8.materialization_profile(),sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()+b"\n"
        if arm == "adaptive_e1":
            manager += ("--convergence-deadline-seconds", "12")
        else:
            identity=json.loads((root/"runtime/e0-identity-receipt.json").read_bytes())
            digest=identity.get("epoch_digest")
            if not isinstance(digest,str) or len(digest)!=64 or any(char not in "0123456789abcdef" for char in digest):
                raise V8PreparationError("fixed-E0 native digest is invalid")
            manager += ("--scheduled-fixed-e0-control", "--scheduled-fixed-e0-run-id", run_id,
                        "--scheduled-fixed-e0-profile-sha256", hashlib.sha256(profile_raw).hexdigest(),
                        "--scheduled-fixed-e0-epoch-zero-digest", digest,
                        "--scheduled-fixed-e0-window-start-monotonic-ns", str(scheduled_start_monotonic_ns),
                        "--scheduled-fixed-e0-window-end-monotonic-ns", str(scheduled_end_monotonic_ns))
        _roles(Path(made["epoch0_tree"]))
        replicas=[tuple(row) for row in made["replica_commands"]]
        replicas[1] = replicas[1] + tuple(overlay)
        configs=tuple(Path(p) for p in made["replica_configs"])
        local._require_materialized_profile_bindings(arm=arm, epoch0_tree=Path(made["epoch0_tree"]),
            main_config=Path(made["main_config"]), replica_configs=configs, manager=manager, hard_timeout_seconds=210)
        for i,row in enumerate(replicas): local._replica_config_binding(row, main_config=Path(made["main_config"]), replica_config=configs[i])
        metadata=root/"runtime/launch-arguments.json"; doc=json.loads(metadata.read_text())
        for process in doc["processes"]:
            if process["source_kind"]=="replica":
                i=int(process["source_id"].split("-")[1]); process["argv"]=list(replicas[i]); process["effective_options"]["replica_config_sha256"]=_sha(configs[i])
            elif process["source_kind"]=="adaptation_manager": process["argv"]=local.base.normalized_manager_argv(manager)
        raw=json.dumps(doc,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()+b"\n"
        _atomic(metadata,raw)
        profile_path=root/"runtime/sustained-role-v8-materialization-profile.json"
        with profile_path.open("xb") as out: out.write(profile_raw)
        selection=HERE/"profile-v4.json"; archived_selection=root/"runtime/inherited-selection-profile-v4.json"
        with selection.open("rb") as incoming, archived_selection.open("xb") as outgoing: outgoing.write(incoming.read())
        epoch=Path(made["epoch0_tree"])
        state={"schema_version":1,"state":"MATERIALIZATION_COMPONENT_ONLY_NOT_LAUNCH_ELIGIBLE","run_id":run_id,
               "launch_arguments_sha256":_sha(metadata),"materialization_profile":{"path":str(profile_path.relative_to(root)),"sha256":_sha(profile_path)},"epoch0_tree":{"path":str(epoch.relative_to(root)),"sha256":_sha(epoch)},"inherited_selection_profile":{"path":str(archived_selection.relative_to(root)),"size_bytes":archived_selection.stat().st_size,"sha256":_sha(archived_selection)},"no_launch":True,"no_retry":True,"claim_eligible":False,"figure_eligible":False,"build_provenance":"UNVERIFIED"}
        with (root/"runtime/sustained-role-v8-preparation.json").open("xb") as out: out.write(json.dumps(state,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()+b"\n")
        return {"epoch0_tree":made["epoch0_tree"],"main_config":made["main_config"],"replica_configs":configs,"replica_commands":replicas,"manager_command":manager}
    except Exception as exc:
        # Preserve the owned fresh root for review; never attempt process cleanup because none were launched.
        (root/"runtime").mkdir(mode=0o700,exist_ok=True)
        abort=root/"runtime/sustained-role-v8-materialization-abort.json"
        if not abort.exists():
            try:
                with abort.open("xb") as out: out.write(json.dumps({"schema_version":1,"state":"ABORTED_MATERIALIZATION_NO_LAUNCH","no_retry":True,"reason":type(exc).__name__},sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()+b"\n")
            except OSError: pass
        raise
