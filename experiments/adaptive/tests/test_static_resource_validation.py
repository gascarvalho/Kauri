"""Focused adversarial fixtures for the speculative S4 source-blind validator."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from experiments.adaptive.kauri_experiment import static_resource_validation as subject
from experiments.adaptive.kauri_experiment.profiled_fault_archive import create_evidence_seal


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="ascii")


def _bundle(epoch: int, previous: str) -> object:
    return SimpleNamespace(
        epoch_number=epoch,
        epoch_digest=("a" if epoch == 0 else "b") * 64,
        previous_epoch_digest=previous,
        trees=tuple(SimpleNamespace(tree_id=tree, members=tuple([tree, *[member for member in range(31) if member != tree]])) for tree in range(21)),
    )


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, adaptive_roots: list[int] | None = None, manager_event: object | None = None, sham_transactions: int = 1, sham_duplicate_commit: bool = False, sham_no_commits: bool = False, backend_content: object | None = None) -> Path:
    root = tmp_path / "pair"
    root.mkdir(parents=True)
    if backend_content is not None:
        _write(root / "raw/backend-evidence-v1.json", backend_content)
    repository = Path(__file__).resolve().parents[3]
    shutil.copyfile(repository / "experiments/adaptive/profiles/n31-static-resource-cpu-sham-v1.json", root / "profile.json")
    shutil.copyfile(repository / "experiments/adaptive/profiles/n31-static-resource-cpu-sham-quota-v1.json", root / "cpu-contract.json")
    adaptive_roots = adaptive_roots or [6, 7, 8, 9, 10, 11, *range(12, 27)]
    bundles: dict[tuple[str, str], object] = {}
    for arm in subject.ARMS:
        baseline = _bundle(0, "bootstrap")
        successor = _bundle(1, "a" * 64)
        if arm == "adaptive":
            successor = SimpleNamespace(epoch_number=1, epoch_digest="b" * 64, previous_epoch_digest="a" * 64, trees=tuple(SimpleNamespace(tree_id=index, members=tuple([root_id, *[member for member in range(31) if member != root_id]])) for index, root_id in enumerate(adaptive_roots)))
        bundles[(arm, "epoch0.bundle")] = baseline
        bundles[(arm, "epoch1.bundle")] = successor
        arm_root = root / arm
        _write(arm_root / "raw/issuer-public-key.txt", "not-a-real-key")
        # The decoder is mocked; opaque wire bytes remain covered by seals.
        (arm_root / "epoch0.bundle").parent.mkdir(parents=True, exist_ok=True)
        (arm_root / "epoch0.bundle").write_bytes(b"e0")
        (arm_root / "epoch1.bundle").write_bytes(b"e1")
        _write(arm_root / "raw/manager.jsonl", manager_event or {"event_type": "normal_observation"})
        _write(arm_root / "raw/manager-argv.json", {"argv": ["manager", "--normal"]})
        for replica in range(31):
            _write(arm_root / f"raw/replica-{replica}.jsonl", {"replica_id": replica})
        _write(arm_root / "manager-snapshot.json", {"eligible_ranking": (list(range(31)) if arm == "sham" else adaptive_roots + [member for member in range(31) if member not in adaptive_roots])})
        _write(arm_root / "activation.json", {"epoch": 1, "members": [{"replica_id": replica, "active": True, "alive": True, "eligible": True} for replica in range(31)]})
        launch_members = [{"replica_id": replica, "cpu_quota_percent": 25 if replica < 6 else 100, "unit": f"unit-{replica}", "control_group": f"/unit-{replica}", "cpu_stat_path": f"/sys/fs/cgroup/unit-{replica}/cpu.stat", "owned_pid": replica + 100, "owned_pgid": replica + 100, "cgroup_pids": [replica + 100], "active_state": "active", "sub_state": "running", "cpu_quota_per_second_usec": (25 if replica < 6 else 100) * 10_000} for replica in range(31)]
        _write(arm_root / "runtime/cpu-launch.json", {"schema_version": 1, "launcher": "systemd-user-scope-cpu-quota-v1", "contract_id": "n31-static-resource-cpu-sham-quota-v1", "contract_sha256": hashlib.sha256((root / "cpu-contract.json").read_bytes()).hexdigest(), "manager_visibility": "none", "replicas": launch_members})
        cpu_samples = []
        service_members = []
        for replica in range(31):
            quota = 25 if replica < 6 else 100
            for timestamp, usage in ((10, 1), (20, 2)):
                cpu_samples.append({"schema_version": 1, "source_monotonic_ns": timestamp, "replica_id": replica, "cpu_quota_percent": quota, "unit": f"unit-{replica}", "control_group": f"/unit-{replica}", "cpu_stat_path": f"/sys/fs/cgroup/unit-{replica}/cpu.stat", "cpu_quota_per_second_usec": quota * 10_000, "active_state": "active", "sub_state": "running", "cpu_stat": {"usage_usec": usage, "user_usec": usage, "system_usec": 0}})
            service_members.append({"replica_id": replica, "samples": [{"monotonic_ns": 10, "completed_units": 1}, {"monotonic_ns": 20, "completed_units": 2 if replica < 6 else 3}]})
        (arm_root / "raw/cpu-samples.jsonl").write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in cpu_samples), encoding="ascii")
        _write(arm_root / "runtime/service.json", {"window": {"start_ns": 10, "end_ns": 20}, "members": service_members})
        _write(arm_root / "resource-receipt.json", {"window": {"start_ns": 10, "end_ns": 20}, "launch_path": f"{arm}/runtime/cpu-launch.json", "samples_path": f"{arm}/raw/cpu-samples.jsonl", "service_path": f"{arm}/runtime/service.json"})
        events = [{"height": 1, "block_hash": f"{arm}-one", "parent_hash": "genesis", "transactions": sham_transactions if arm == "sham" else 1, "monotonic_ns": 11}]
        if arm == "sham" and sham_duplicate_commit:
            events.append(dict(events[0]))
        if arm == "sham" and sham_no_commits:
            events = []
        _write(arm_root / "commit-window.json", {"window": {"start_ns": 10, "end_ns": 20}, "events": events})
        _write(arm_root / "cleanup.json", {"complete": True, "owned": [*(f"replica-{replica}" for replica in range(31)), "manager"]})
        _write(arm_root / "arm-manifest.json", {"schema_version": 1, "study_id": subject.STUDY_ID, "pair_id": "fixture", "arm": arm, "attempt": 1, "retry_count": 0, "issuer_key_path": f"{arm}/raw/issuer-public-key.txt", "epoch0_path": f"{arm}/epoch0.bundle", "epoch1_path": f"{arm}/epoch1.bundle", "manager_path": f"{arm}/raw/manager.jsonl", "manager_argv_path": f"{arm}/raw/manager-argv.json", "activation_path": f"{arm}/activation.json", "resource_receipt_path": f"{arm}/resource-receipt.json", "commit_path": f"{arm}/commit-window.json", "cleanup_path": f"{arm}/cleanup.json"})
        create_evidence_seal(arm_root)
    baseline_projection = [{"tree_id": tree, "members": [tree, *[member for member in range(31) if member != tree]]} for tree in range(21)]
    descriptors = {arm: {"manifest_path": f"{arm}/arm-manifest.json", "tree_sha256": create_evidence_seal.__name__, "seal_sha256": ""} for arm in subject.ARMS}
    # Read actual child seal metadata through the public verifier.
    from experiments.adaptive.kauri_experiment.profiled_fault_archive import verify_evidence_seal
    for arm in subject.ARMS:
        seal = verify_evidence_seal(root / arm)
        descriptors[arm]["tree_sha256"] = seal.tree_sha256
        descriptors[arm]["seal_sha256"] = seal.seal_sha256
    paths = sorted([path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()])
    paths.append("pair-manifest.json")
    issuer_sha = hashlib.sha256((root / "sham/raw/issuer-public-key.txt").read_bytes()).hexdigest()
    _write(root / "pair-manifest.json", {"schema_version": 1, "study_id": subject.STUDY_ID, "mode": "excluded_shakedown", "pair_id": "fixture", "execution": {"schedule": ["sham", "adaptive"], "automatic_retries": 0, "replacement_policy": "none", "claim_eligible": False, "figure_eligible": False}, "protocol": {"N": 31, "f": 10, "Q": 21, "tree_count": 21}, "bindings": {"revision": "a" * 40, "build_sha256": "b" * 64, "workload_sha256": "c" * 64, "successor_schedule_sha256": "d" * 64, "host_identity_sha256": "e" * 64, "profile_path": "profile.json", "profile_sha256": hashlib.sha256((root / "profile.json").read_bytes()).hexdigest(), "cpu_contract_path": "cpu-contract.json", "cpu_contract_sha256": hashlib.sha256((root / "cpu-contract.json").read_bytes()).hexdigest()}, "shared_epoch0_projection_sha256": hashlib.sha256(subject._canonical(baseline_projection).encode("ascii")).hexdigest(), "shared_epoch0_digest": "a" * 64, "issuer_public_key_sha256": issuer_sha, "backend_raw_evidence_path": "raw/backend-evidence-v1.json", "arms": descriptors, "paths": sorted(paths)})
    create_evidence_seal(root)
    def decode(local_root: Path, bundle_path: str, _key_path: str) -> object:
        return bundles[(Path(bundle_path).parts[0], Path(bundle_path).name)]
    monkeypatch.setattr(subject, "_decode", decode)
    return root


def test_receipt_only_fixture_fails_closed_without_backend_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(subject.StaticResourceValidationError, match="backend reconstruction not implemented"):
        subject.validate_pair(_fixture(tmp_path, monkeypatch))


def test_arbitrary_backend_file_cannot_enable_a_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _fixture(tmp_path, monkeypatch, backend_content={"claimed": "backend"})
    with pytest.raises(subject.StaticResourceValidationError, match="backend reconstruction not implemented"):
        subject.validate_pair(root)


@pytest.mark.parametrize("path", ["../outside.json", "/private/tmp/outside.json", "raw/../outside.json"])
def test_manifest_path_traversal_is_rejected(tmp_path: Path, path: str) -> None:
    root = tmp_path / "pair"
    root.mkdir()
    (tmp_path / "outside.json").write_text("{}\n", encoding="ascii")
    with pytest.raises(subject.StaticResourceValidationError, match="path"):
        subject._read(root, path, "mutation")


def test_valid_no_demotion_is_retained_as_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(subject.StaticResourceValidationError, match="backend reconstruction not implemented"):
        subject.validate_pair(_fixture(tmp_path, monkeypatch, adaptive_roots=list(range(21))))


@pytest.mark.parametrize("path", ["sham/raw/replica-7.jsonl", "adaptive/resource-receipt.json"])
def test_missing_source_is_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    root = _fixture(tmp_path, monkeypatch)
    (root / path).unlink()
    with pytest.raises(subject.StaticResourceValidationError):
        subject.validate_pair(root)


def test_manager_resource_label_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _fixture(tmp_path, monkeypatch, manager_event={"quota": 25})
    with pytest.raises(subject.StaticResourceValidationError, match="resource labels"):
        subject.validate_pair(root)


def test_tampered_pair_bytes_are_rejected_by_source_blind_seal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _fixture(tmp_path, monkeypatch)
    (root / "sham/activation.json").write_text("{}\n", encoding="ascii")
    with pytest.raises(subject.StaticResourceValidationError, match="pair evidence seal"):
        subject.validate_pair(root)


def test_sham_reordering_is_not_accepted_as_a_control(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _fixture(tmp_path, monkeypatch)
    baseline = _bundle(0, "bootstrap")
    reordered = _bundle(1, "a" * 64)
    swapped = list(reordered.trees)
    swapped[0] = SimpleNamespace(tree_id=0, members=tuple([0, 2, 1, *range(3, 31)]))
    reordered = SimpleNamespace(epoch_number=1, epoch_digest="b" * 64, previous_epoch_digest="a" * 64, trees=tuple(swapped))
    def decode(_root: Path, path: str, _key: str) -> object:
        return reordered if path == "sham/epoch1.bundle" else baseline if path.endswith("epoch0.bundle") else _bundle(1, "a" * 64)
    monkeypatch.setattr(subject, "_decode", decode)
    with pytest.raises(subject.StaticResourceValidationError, match="sham E1"):
        subject.validate_pair(root)


def test_adaptive_roots_must_bind_the_full_native_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _fixture(tmp_path, monkeypatch)
    baseline = _bundle(0, "bootstrap")
    wrong = _bundle(1, "a" * 64)
    def decode(_root: Path, path: str, _key: str) -> object:
        return baseline if path.endswith("epoch0.bundle") else wrong
    monkeypatch.setattr(subject, "_decode", decode)
    with pytest.raises(subject.StaticResourceValidationError, match="adaptive E1 roots"):
        subject.validate_pair(root)


def test_throughput_is_transaction_weighted_and_rejects_bad_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _fixture(tmp_path, monkeypatch, sham_transactions=7)
    with pytest.raises(subject.StaticResourceValidationError, match="backend reconstruction not implemented"):
        subject.validate_pair(root)
    root = _fixture(tmp_path / "zero", monkeypatch, sham_transactions=0)
    with pytest.raises(subject.StaticResourceValidationError, match="authoritative commit identity"):
        subject.validate_pair(root)
    root = _fixture(tmp_path / "negative", monkeypatch, sham_transactions=-1)
    with pytest.raises(subject.StaticResourceValidationError, match="authoritative commit identity"):
        subject.validate_pair(root)
    root = _fixture(tmp_path / "duplicate", monkeypatch, sham_duplicate_commit=True)
    with pytest.raises(subject.StaticResourceValidationError, match="authoritative commit identity"):
        subject.validate_pair(root)
    root = _fixture(tmp_path / "missing", monkeypatch, sham_no_commits=True)
    with pytest.raises(subject.StaticResourceValidationError, match="no authoritative commits"):
        subject.validate_pair(root)
