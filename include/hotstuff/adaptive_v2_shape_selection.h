/**
 * Pure, deterministic successor tree-shape selection.
 */

#ifndef HOTSTUFF_ADAPTIVE_V2_SHAPE_SELECTION_H_INCLUDED
#define HOTSTUFF_ADAPTIVE_V2_SHAPE_SELECTION_H_INCLUDED

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include "hotstuff/adaptation.h"
#include "hotstuff/configuration.h"

namespace hotstuff
{

constexpr std::uint32_t kShapeV1SchemaVersion = 1;
constexpr const char *kShapeV1SelectorVersion = "shape-v1";
constexpr const char *kShapeV1TieRule =
    "lower-latency-risk-churn-current-canonical-v1";
constexpr const char *kShapeV1ReferenceTreeRule =
    "lowest-tree-id-prefix-q-v1";

struct ShapeV1Config
{
    std::vector<std::uint32_t> candidate_fanouts{2, 3, 5};
    std::uint32_t fixed_pipeline_stretch{2};
    std::uint64_t deterministic_seed{0};
    std::string selector_version{kShapeV1SelectorVersion};
    std::string tie_rule{kShapeV1TieRule};
    std::string reference_tree_rule{kShapeV1ReferenceTreeRule};
};

/** Immutable per-replica summary derived from one accepted evidence prefix. */
struct ShapeV1ReplicaEvidence
{
    ReplicaID replica_id{0};
    ResponsivenessClass classification{
        ResponsivenessClass::insufficient_evidence};
    std::uint32_t attempt_count{0};
    RatePpm timeout_rate_ppm{0};
    std::optional<std::uint64_t> latency_percentile_us;
};

struct ShapeV1Input
{
    std::uint32_t schema_version{kShapeV1SchemaVersion};
    std::uint32_t epoch_number{0};
    uint256_t epoch_digest;
    std::vector<EpochTreeDefinition> current_trees;
    std::vector<ShapeV1ReplicaEvidence> evidence;
    std::uint64_t evidence_cutoff{0};
    std::vector<std::uint32_t> candidate_fanouts;
    /**
     * Successor tree count Q. After sorting current_trees by tree_id,
     * shape-v1 scores the first Q while validating and digesting all
     * predecessor trees.
     */
    std::uint32_t tree_count{0};
    std::uint32_t fixed_pipeline_stretch{0};
    std::uint64_t deterministic_seed{0};
    std::string selector_version;
    std::string tie_rule;
    std::string reference_tree_rule;
};

enum class ShapeV1CandidateRejection : std::uint8_t
{
    none = 0,
    fanout_out_of_range,
    wait_exempt_would_influence,
    missing_influential_evidence,
    score_overflow,
};

struct ShapeV1CandidateScore
{
    std::uint32_t fanout{0};
    ShapeV1CandidateRejection rejection{
        ShapeV1CandidateRejection::none};
    std::uint32_t depth{0};
    std::uint64_t risk{0};
    std::uint64_t latency{0};
    std::uint64_t churn{0};
    bool switch_threshold_satisfied{false};
};

/**
 * Purely recomputed choice; threshold flags align with the input table.
 * Rejected and current candidates always receive a false flag.
 */
struct ShapeV1CanonicalChoice
{
    std::uint32_t selected_fanout{0};
    std::vector<bool> switch_thresholds;
};

std::optional<ShapeV1CanonicalChoice>
recompute_shape_v1_canonical_choice(
    const std::vector<ShapeV1CandidateScore> &candidates,
    std::uint32_t current_fanout) noexcept;

enum class ShapeV1Status : std::uint8_t
{
    selected = 1,
    invalid_input,
    no_feasible_candidate,
};

/** Canonical audit record; application is finalized outside the selector. */
struct ShapeDecisionRecord
{
    std::uint32_t schema_version{kShapeV1SchemaVersion};
    ShapeV1Status status{ShapeV1Status::invalid_input};
    std::string selector_version;
    std::string tie_rule;
    std::uint32_t epoch_number{0};
    uint256_t epoch_digest;
    uint256_t current_topology_digest;
    std::uint64_t evidence_cutoff{0};
    uint256_t evidence_digest;
    std::uint32_t predecessor_tree_count{0};
    /** Successor count and canonical reference-prefix length (Q). */
    std::uint32_t tree_count{0};
    std::uint32_t fixed_pipeline_stretch{0};
    std::uint64_t deterministic_seed{0};
    std::uint32_t current_fanout{0};
    std::uint32_t selected_fanout{0};
    std::uint32_t applied_fanout{0};
    std::string reference_tree_rule;
    std::vector<ShapeV1CandidateScore> candidates;
    uint256_t decision_digest;
};

ShapeDecisionRecord select_shape_v1(const ShapeV1Input &input) noexcept;

/** Set only the applied/control fanout and refresh the canonical digest. */
bool finalize_shape_v1_application(
    ShapeDecisionRecord &record,
    bool apply_selected) noexcept;

bytearray_t canonical_serialize_shape_decision_record(
    const ShapeDecisionRecord &record);

uint256_t compute_shape_decision_digest(
    const ShapeDecisionRecord &record);

bool valid_shape_decision_record(
    const ShapeDecisionRecord &record) noexcept;

} // namespace hotstuff

#endif
