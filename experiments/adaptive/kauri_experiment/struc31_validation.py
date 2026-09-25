"""Independent fail-closed validation for STRUC31 component/model artifacts.

This module intentionally does not import the C++ producer or tree-policy
implementation.  It derives the frozen finite population and checks every
serialized result directly.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import Any


class Struc31ValidationError(ValueError):
    """Raised when an artifact deviates from the frozen STRUC31 contract."""


MEMBERS = tuple(range(31))
FANOUTS = (2, 5)
COHORTS = ("nonbaseline", "baseline")
TREE_COUNT = 21
QUORUM = 21
PIPELINE_STRETCH = 2
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise Struc31ValidationError(message)


def _require_int(value: Any, field: str) -> int:
    _require(type(value) is int, f"{field} must be an integer")
    return value


def _first_leaf_index(member_count: int, fanout: int) -> int:
    return 0 if member_count == 1 else ((member_count - 2) // fanout) + 1


def _expected_ids(cohort: str, k: int) -> list[int]:
    if cohort == "baseline":
        return list(range(k))
    if cohort == "nonbaseline":
        return list(range(21, 21 + k))
    raise Struc31ValidationError(f"unknown cohort: {cohort!r}")


def _expect_ints(value: Any, field: str) -> list[int]:
    _require(isinstance(value, list) and all(type(item) is int for item in value),
             f"{field} must be an integer list")
    return value


def _validate_producer(producer: Any) -> None:
    _require(isinstance(producer, Mapping), "producer must be an object")
    _require(set(producer) == {"revision", "producer_source_sha256", "tree_policy_source_sha256",
                               "producer_binary_sha256", "command"},
             "producer has missing or extra fields")
    revision = producer.get("revision")
    _require(isinstance(revision, str) and _REVISION.fullmatch(revision) is not None,
             "producer.revision must be a full lowercase Git revision")
    for key in ("producer_source_sha256", "tree_policy_source_sha256", "producer_binary_sha256"):
        value = producer.get(key)
        _require(isinstance(value, str) and _SHA256.fullmatch(value) is not None,
                 f"producer.{key} must be lowercase SHA-256")
    command = producer.get("command")
    expected_command = (
        "struc31-placement-matrix --canonical-stdout-v1"
        f" --revision {revision}"
        f" --producer-source-sha256 {producer['producer_source_sha256']}"
        f" --tree-policy-source-sha256 {producer['tree_policy_source_sha256']}"
        f" --producer-binary-sha256 {producer['producer_binary_sha256']}"
    )
    _require(command == expected_command, "producer.command must exactly bind producer provenance")


def _validate_manifest(manifest: Any) -> None:
    _require(isinstance(manifest, Mapping), "input_manifest must be an object")
    _require(set(manifest) == {"membership", "required_distinct_tree_roots", "consensus_quorum_Q",
                               "pipeline_stretch", "fanouts", "constraint_basis", "coverage_scope"},
             "input manifest has missing or extra fields")
    _require(_expect_ints(manifest.get("membership"), "membership") == list(MEMBERS),
             "membership must be exactly 0..30")
    _require(_require_int(manifest.get("required_distinct_tree_roots"), "root requirement") == TREE_COUNT,
             "tree root requirement must remain 21")
    _require(_require_int(manifest.get("consensus_quorum_Q"), "consensus quorum") == QUORUM,
             "consensus quorum metadata must remain 21")
    _require(_require_int(manifest.get("pipeline_stretch"), "pipeline stretch") == PIPELINE_STRETCH,
             "pipeline stretch must remain 2")
    _require(_expect_ints(manifest.get("fanouts"), "fanouts") == list(FANOUTS),
             "fanouts must be [2, 5]")
    _require(manifest.get("constraint_basis") == "model_policy_constrained_leaves",
             "artifact must state the model constrained-leaf basis")
    _require(manifest.get("coverage_scope") ==
             "declared-40-cell-matrix-not-all-subsets-v1",
             "artifact must not overclaim exhaustive subset coverage")


def _validate_valid_cell(cell: Mapping[str, Any], fanout: int, cohort: str, k: int) -> None:
    _require(set(cell) == {"fanout", "cohort", "k", "ineligible_ids", "eligible_count",
                           "first_leaf_index", "leaf_capacity", "status", "roots",
                           "preserved_baseline_count", "fallback_roots", "topology_sha256",
                           "explanation_sha256", "trees"}, "valid cell has missing or extra fields")
    _require(_require_int(cell.get("fanout"), "cell fanout") == fanout,
             "cell fanout differs from canonical identity")
    _require(_require_int(cell.get("k"), "cell k") == k,
             "cell k differs from canonical identity")
    _require(cell.get("cohort") == cohort, "cell cohort differs from canonical identity")
    expected_ids = _expected_ids(cohort, k)
    leaf_start = _first_leaf_index(len(MEMBERS), fanout)
    _require(cell.get("status") == "valid", "k=1..10 cells must be valid")
    _require(_expect_ints(cell.get("ineligible_ids"), "ineligible_ids") == expected_ids,
             "cell ineligible IDs differ from frozen cohort")
    _require(_require_int(cell.get("eligible_count"), "eligible count") == 31 - k, "incorrect eligible count")
    _require(_require_int(cell.get("first_leaf_index"), "first leaf index") == leaf_start, "incorrect first leaf index")
    _require(_require_int(cell.get("leaf_capacity"), "leaf capacity") == 31 - leaf_start, "incorrect leaf capacity")
    roots = _expect_ints(cell.get("roots"), "roots")
    _require(len(roots) == TREE_COUNT and len(set(roots)) == TREE_COUNT,
             "valid cell must have 21 distinct roots")
    _require(not set(roots).intersection(expected_ids), "ineligible member is a root")
    expected_preserved = TREE_COUNT if cohort == "nonbaseline" else TREE_COUNT - k
    _require(_require_int(cell.get("preserved_baseline_count"), "preserved baseline count") == expected_preserved,
             "incorrect baseline-root preservation count")
    fallbacks = _expect_ints(cell.get("fallback_roots"), "fallback_roots")
    expected_fallbacks = 0 if cohort == "nonbaseline" else k
    _require(len(fallbacks) == expected_fallbacks, "incorrect fallback-root count")
    _require(not set(fallbacks).intersection(expected_ids), "ineligible fallback root")
    if cohort == "nonbaseline":
        _require(roots == list(range(TREE_COUNT)),
                 "nonbaseline roots must preserve the full baseline vector")
        _require(fallbacks == [], "nonbaseline cohort must not use fallback roots")
    else:
        _require(roots[k:] == list(range(k, TREE_COUNT)),
                 "unconstrained baseline roots must remain in their tree slots")
        _require(fallbacks == roots[:k],
                 "fallback roots must be bound to constrained baseline tree slots")
    for key in ("topology_sha256", "explanation_sha256"):
        value = cell.get(key)
        _require(isinstance(value, str) and _SHA256.fullmatch(value) is not None,
                 f"{key} must be lowercase SHA-256")

    trees = cell.get("trees")
    _require(isinstance(trees, list) and len(trees) == TREE_COUNT,
             "valid cell must contain exactly 21 complete trees")
    topology = []
    explanation = [*(f"{root}," for root in roots), ";",
                   *(f"{fallback}," for fallback in fallbacks)]
    for tree_id, tree in enumerate(trees):
        _require(isinstance(tree, Mapping), "tree must be an object")
        _require(set(tree) == {"tree_id", "members_breadth_first", "constrained_positions"},
                 "tree has missing or extra fields")
        _require(_require_int(tree.get("tree_id"), "tree ID") == tree_id,
                 "tree IDs must be canonical 0..20")
        members = _expect_ints(tree.get("members_breadth_first"), "tree members")
        _require(len(members) == 31 and sorted(members) == list(MEMBERS),
                 "tree must be a complete membership permutation")
        _require(members[0] == roots[tree_id], "tree root does not match roots vector")
        topology.extend((f"{tree_id}:", *(f"{member}," for member in members), ";"))
        positions = tree.get("constrained_positions")
        _require(isinstance(positions, list) and len(positions) == k,
                 "every constrained member needs exactly one position per tree")
        actual: dict[int, int] = {}
        for position in positions:
            _require(isinstance(position, Mapping), "constrained position must be an object")
            _require(set(position) == {"replica_id", "position"},
                     "constrained position has missing or extra fields")
            replica = position.get("replica_id")
            index = position.get("position")
            _require(type(replica) is int and type(index) is int,
                     "constrained position values must be integers")
            _require(replica in expected_ids and replica not in actual,
                     "constrained position has wrong or duplicate replica")
            _require(leaf_start <= index < 31 and members[index] == replica,
                     "constrained member is not in its physical leaf position")
            actual[replica] = index
        _require(sorted(actual) == expected_ids, "missing constrained member position")
        _require([position["replica_id"] for position in positions] == expected_ids,
                 "constrained positions must use canonical replica order")
        for position in positions:
            explanation.append(
                f"{tree_id}:{position['replica_id']}:{position['position']};")
    _require(cell["topology_sha256"] == hashlib.sha256(
        "".join(topology).encode("ascii")).hexdigest(), "topology fingerprint mismatch")
    _require(cell["explanation_sha256"] == hashlib.sha256(
        "".join(explanation).encode("ascii")).hexdigest(), "explanation fingerprint mismatch")


def _validate_boundary_cell(cell: Mapping[str, Any], fanout: int, cohort: str) -> None:
    expected_ids = _expected_ids(cohort, 11)
    leaf_start = _first_leaf_index(len(MEMBERS), fanout)
    _require(_require_int(cell.get("fanout"), "boundary fanout") == fanout,
             "boundary fanout differs from canonical identity")
    _require(_require_int(cell.get("k"), "boundary k") == 11 and cell.get("cohort") == cohort,
             "boundary identity differs from canonical input")
    _require(cell.get("status") == "insufficient_eligible_roots",
             "k=11 boundary must fail only for insufficient eligible roots")
    _require(_expect_ints(cell.get("ineligible_ids"), "ineligible_ids") == expected_ids,
             "boundary ineligible IDs differ from frozen cohort")
    _require(_require_int(cell.get("eligible_count"), "boundary eligible count") == 20, "boundary must leave 20 eligible members")
    _require(_require_int(cell.get("first_leaf_index"), "boundary leaf index") == leaf_start, "incorrect boundary leaf index")
    _require(_require_int(cell.get("leaf_capacity"), "boundary leaf capacity") == 31 - leaf_start, "incorrect boundary leaf capacity")
    _require(cell.get("trees") == [], "boundary must not serialize partial trees")
    _require(set(cell) == {"fanout", "cohort", "k", "ineligible_ids", "eligible_count",
                           "first_leaf_index", "leaf_capacity", "status", "trees"},
             "boundary must not claim root or placement output")


def validate_struc31_artifact(
    document: Any, *, repository: Path | None = None, producer_binary: Path | None = None
) -> None:
    """Validate the frozen structural matrix, plus optional provenance bindings.

    Without both bindings this proves only the declared finite structure, not
    a thesis-facing producer claim.  Use ``validate_struc31_claim_artifact``
    at that fail-closed boundary.
    """
    _require(isinstance(document, Mapping), "artifact must be a JSON object")
    _require(set(document) == {"schema", "producer", "input_manifest", "cells", "boundary_cells"},
             "artifact has missing or extra top-level fields")
    _require(document.get("schema") == "struc31-placement-matrix-v1", "unsupported schema")
    _validate_producer(document.get("producer"))
    if repository is not None:
        producer = document["producer"]
        for field, relative_path in (
            ("producer_source_sha256", "examples/struc31_placement_matrix.cpp"),
            ("tree_policy_source_sha256", "src/tree_policy.cpp"),
        ):
            dirty = subprocess.run(["git", "-C", str(repository), "diff", "--quiet", "HEAD", "--", relative_path])
            _require(dirty.returncode == 0, f"relevant source path is dirty: {relative_path}")
            source = subprocess.run(["git", "-C", str(repository), "show",
                                     f"{producer['revision']}:{relative_path}"],
                                    check=False, capture_output=True)
            _require(source.returncode == 0, f"source unavailable at declared revision: {relative_path}")
            _require(hashlib.sha256(source.stdout).hexdigest() == producer[field],
                     f"declared-revision source hash mismatch: {relative_path}")
        revision = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            check=False, capture_output=True, text=True,
        )
        _require(revision.returncode == 0 and revision.stdout.strip() == producer["revision"],
                 "artifact revision does not match checked-out repository")
    if producer_binary is not None:
        _require(producer_binary.is_file(), "producer binary unavailable for provenance check")
        _require(hashlib.sha256(producer_binary.read_bytes()).hexdigest() ==
                 document["producer"]["producer_binary_sha256"],
                 "producer binary hash mismatch")
    _validate_manifest(document.get("input_manifest"))

    cells = document.get("cells")
    _require(isinstance(cells, list) and len(cells) == 40, "artifact must contain exactly 40 valid cells")
    expected = [(fanout, cohort, k) for fanout in FANOUTS for cohort in COHORTS for k in range(1, 11)]
    actual = []
    for cell in cells:
        _require(isinstance(cell, Mapping), "cell must be an object")
        actual.append((cell.get("fanout"), cell.get("cohort"), cell.get("k")))
    _require(actual == expected, "valid cells must be complete and canonically ordered")
    for cell, (fanout, cohort, k) in zip(cells, expected, strict=True):
        _validate_valid_cell(cell, fanout, cohort, k)

    boundaries = document.get("boundary_cells")
    _require(isinstance(boundaries, list) and len(boundaries) == 2,
             "artifact must contain exactly two baseline-k=11 boundaries")
    expected_boundaries = [(fanout, "baseline") for fanout in FANOUTS]
    actual_boundaries = [(cell.get("fanout"), cell.get("cohort"), cell.get("k"))
                         if isinstance(cell, Mapping) else None for cell in boundaries]
    _require(actual_boundaries == [(fanout, cohort, 11) for fanout, cohort in expected_boundaries],
             "boundary cells must be complete and canonically ordered")
    for cell, (fanout, cohort) in zip(boundaries, expected_boundaries, strict=True):
        _require(isinstance(cell, Mapping), "boundary must be an object")
        _validate_boundary_cell(cell, fanout, cohort)


def validate_file(
    path: str, *, repository: Path | None = None, producer_binary: Path | None = None
) -> None:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise Struc31ValidationError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    raw = Path(path).read_text(encoding="utf-8")
    document = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    canonical = json.dumps(document, separators=(",", ":"), ensure_ascii=True)
    _require(raw == canonical + "\n", "artifact bytes are not canonical JSON")
    validate_struc31_artifact(document, repository=repository,
                              producer_binary=producer_binary)


def validate_struc31_claim_artifact(
    document: Any, *, repository: Path, producer_binary: Path
) -> None:
    """Validate structure and mandatory source/binary provenance for a claim."""
    validate_struc31_artifact(document, repository=repository,
                              producer_binary=producer_binary)
