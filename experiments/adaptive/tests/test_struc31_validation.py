"""Adversarial tests for the independent STRUC31 artifact validator."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from experiments.adaptive.kauri_experiment.struc31_validation import (
    Struc31ValidationError,
    validate_struc31_artifact,
    validate_file,
)


REPOSITORY = Path(__file__).resolve().parents[3]


def _binary() -> Path:
    configured = os.environ.get("STRUC31_MATRIX_BINARY")
    candidate = Path(configured) if configured else (
        REPOSITORY / "build-adaptive/examples/struc31-placement-matrix")
    assert candidate.is_file(), "build struc31-placement-matrix or set STRUC31_MATRIX_BINARY"
    return candidate


@pytest.fixture()
def artifact() -> dict:
    producer = REPOSITORY / "examples/struc31_placement_matrix.cpp"
    policy = REPOSITORY / "src/tree_policy.cpp"
    output = subprocess.check_output(
        [
            str(_binary()),
            "--canonical-stdout-v1",
            "--revision", subprocess.check_output(
                ["git", "-C", str(REPOSITORY), "rev-parse", "HEAD"], text=True
            ).strip(),
            "--producer-source-sha256", hashlib.sha256(producer.read_bytes()).hexdigest(),
            "--tree-policy-source-sha256", hashlib.sha256(policy.read_bytes()).hexdigest(),
            "--producer-binary-sha256", hashlib.sha256(_binary().read_bytes()).hexdigest(),
        ],
        text=True,
    )
    return json.loads(output)


def _reject(document: dict) -> None:
    with pytest.raises(Struc31ValidationError):
        validate_struc31_artifact(document)


def test_producer_requires_exactly_one_canonical_stdout_flag() -> None:
    producer = REPOSITORY / "examples/struc31_placement_matrix.cpp"
    policy = REPOSITORY / "src/tree_policy.cpp"
    arguments = [
        "--revision", "a" * 40,
        "--producer-source-sha256", hashlib.sha256(producer.read_bytes()).hexdigest(),
        "--tree-policy-source-sha256", hashlib.sha256(policy.read_bytes()).hexdigest(),
        "--producer-binary-sha256", hashlib.sha256(_binary().read_bytes()).hexdigest(),
    ]
    omitted = subprocess.run([str(_binary()), *arguments], capture_output=True, text=True)
    duplicated = subprocess.run(
        [str(_binary()), "--canonical-stdout-v1", "--canonical-stdout-v1", *arguments],
        capture_output=True, text=True,
    )
    unknown_claim = subprocess.run(
        [str(_binary()), "--canonical-stdout-v1", *arguments,
         "--claim", "live-throughput-proof"],
        capture_output=True, text=True,
    )
    assert omitted.returncode == 2
    assert duplicated.returncode == 2
    assert unknown_claim.returncode == 2


def test_accepts_frozen_42_cell_artifact(artifact: dict) -> None:
    validate_struc31_artifact(artifact)
    assert len(artifact["cells"]) == 40
    assert [(cell["fanout"], cell["cohort"], cell["k"])
            for cell in artifact["boundary_cells"]] == [(2, "baseline", 11), (5, "baseline", 11)]


def test_file_validation_rejects_noncanonical_or_duplicate_json_keys(
    artifact: dict, tmp_path: Path
) -> None:
    artifact_path = tmp_path / "matrix.json"
    artifact_path.write_text(json.dumps(artifact, separators=(",", ":")) + "\n", encoding="utf-8")
    validate_file(str(artifact_path))

    artifact_path.write_text("{\"schema\":\"one\",\"schema\":\"two\"}\n", encoding="utf-8")
    with pytest.raises(Struc31ValidationError, match="duplicate JSON key"):
        validate_file(str(artifact_path))

    artifact_path.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(Struc31ValidationError, match="not canonical JSON"):
        validate_file(str(artifact_path))


@pytest.mark.parametrize("field", ["revision", "producer_source_sha256", "tree_policy_source_sha256",
                                    "producer_binary_sha256"])
def test_rejects_bound_provenance_mismatch(artifact: dict, field: str) -> None:
    document = copy.deepcopy(artifact)
    document["producer"][field] = "0" * (40 if field == "revision" else 64)
    with pytest.raises(Struc31ValidationError):
        validate_struc31_artifact(document, repository=REPOSITORY, producer_binary=_binary())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: document["cells"][0]["roots"].__setitem__(1, document["cells"][0]["roots"][0]),
        lambda document: document["cells"][0]["trees"][0]["members_breadth_first"].pop(),
        lambda document: document["cells"][0].__setitem__("status", "insufficient_eligible_roots"),
        lambda document: document["cells"][0].__setitem__("first_leaf_index", 0),
        lambda document: document["boundary_cells"][0].__setitem__("trees", [[0]]),
        lambda document: document["producer"].__setitem__("tree_policy_source_sha256", "f" * 63),
        lambda document: document["cells"][0].__setitem__("topology_sha256", "0" * 64),
        lambda document: document["cells"][10]["roots"].__setitem__(0, 20),
        lambda document: document["cells"][0]["ineligible_ids"].__setitem__(0, True),
        lambda document: document["cells"][0].__setitem__("fanout", True),
        lambda document: document["cells"][0]["trees"][0].__setitem__("tree_id", False),
        lambda document: document["producer"].__setitem__("command", document["producer"]["command"] + " --extra"),
        lambda document: document["producer"].__setitem__("claim", "improved throughput"),
        lambda document: document["input_manifest"].__setitem__("generalizes_to_all_subsets", True),
        lambda document: document["cells"][0]["trees"][0]["constrained_positions"][0].__setitem__("diagnosis", "resource slow"),
        lambda document: document["cells"][1]["trees"][0]["constrained_positions"].reverse(),
    ],
)
def test_rejects_adversarial_mutations(artifact: dict, mutate) -> None:
    document = copy.deepcopy(artifact)
    mutate(document)
    _reject(document)
