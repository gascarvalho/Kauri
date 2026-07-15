/**
 * Deterministic tree placement from an immutable adaptation snapshot.
 */

#ifndef HOTSTUFF_TREE_POLICY_H_INCLUDED
#define HOTSTUFF_TREE_POLICY_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include "hotstuff/adaptation.h"
#include "hotstuff/configuration.h"

namespace hotstuff
{

constexpr std::uint32_t kTreePolicySchemaVersion = 1;
constexpr std::size_t kMaximumTreePolicyMembers = 4'096;
constexpr std::uint32_t kMaximumTreePolicyTrees = 256;
constexpr std::uint32_t kMaximumTreePolicyFanout = 4'096;
constexpr std::uint32_t kMaximumTreePolicyPipelineStretch = 4'096;
constexpr std::size_t kMaximumTreePolicyVersionBytes = 64;

enum class TreePolicyKind : std::uint8_t
{
    fault_containment = 1,
    performance_optimization = 2,
};

enum class TreeReplicaRole : std::uint8_t
{
    root = 1,
    internal = 2,
    leaf = 3,
};

enum class RootSelectionReason : std::uint8_t
{
    preserved_eligible_baseline = 1,
    fallback_ineligible_baseline = 2,
    fallback_duplicate_baseline = 3,
    fallback_missing_baseline = 4,
    highest_ranked_eligible = 5,
};

enum class ReplicaPlacementReason : std::uint8_t
{
    selected_root = 1,
    balanced_eligible_internal = 2,
    seeded_eligible_leaf = 3,
    constrained_ineligible_leaf = 4,
};

struct TreeShape
{
    std::uint32_t fanout{0};
    std::uint32_t pipeline_stretch{0};
    std::uint32_t tree_count{0};
};

struct BaselineRoot
{
    std::uint32_t tree_id{0};
    ReplicaID replica_id{0};
};

struct FaultContainmentPolicy
{
    std::vector<BaselineRoot> baseline_roots;
};

struct PerformanceOptimizationPolicy
{};

struct TreePlacementInput
{
    std::vector<ReplicaID> membership;
    TreeShape shape;
    std::uint64_t generation_seed{0};
    std::string policy_version;
};

struct RootDecision
{
    std::uint32_t tree_id{0};
    std::optional<ReplicaID> requested_baseline_root;
    ReplicaID chosen_root{0};
    std::uint32_t chosen_rank{0};
    RootSelectionReason reason{
        RootSelectionReason::highest_ranked_eligible};
};

struct ReplicaRoleDecision
{
    std::uint32_t tree_id{0};
    ReplicaID replica_id{0};
    std::uint32_t rank{0};
    ResponsivenessClass classification{
        ResponsivenessClass::insufficient_evidence};
    bool eligible{false};
    std::uint32_t position{0};
    TreeReplicaRole role{TreeReplicaRole::leaf};
    ReplicaPlacementReason reason{
        ReplicaPlacementReason::constrained_ineligible_leaf};
};

struct TreePlacementExplanation
{
    std::uint32_t schema_version{kTreePolicySchemaVersion};
    TreePolicyKind policy_kind{TreePolicyKind::fault_containment};
    std::string policy_version;
    std::uint64_t generation_seed{0};
    std::string evidence_snapshot_id;
    std::uint64_t evidence_cutoff{0};
    std::vector<RootDecision> root_decisions;
    std::vector<ReplicaRoleDecision> replica_roles;
};

class TreePlacementResult final
{
public:
    TreePlacementResult(const TreePlacementResult &) = default;
    TreePlacementResult(TreePlacementResult &&) = default;
    TreePlacementResult &operator=(const TreePlacementResult &) = delete;
    TreePlacementResult &operator=(TreePlacementResult &&) = delete;

    const std::vector<EpochTreeDefinition> &trees() const noexcept
    {
        return trees_;
    }

    const TreePlacementExplanation &explanation() const noexcept
    {
        return explanation_;
    }

private:
    friend TreePlacementResult build_tree_placement(
        const TreePlacementInput &input,
        const AdaptationSnapshot &snapshot,
        const FaultContainmentPolicy &policy);

    friend TreePlacementResult build_tree_placement(
        const TreePlacementInput &input,
        const AdaptationSnapshot &snapshot,
        const PerformanceOptimizationPolicy &policy);

    TreePlacementResult() = default;

    std::vector<EpochTreeDefinition> trees_;
    TreePlacementExplanation explanation_;
};

TreePlacementResult build_tree_placement(
    const TreePlacementInput &input,
    const AdaptationSnapshot &snapshot,
    const FaultContainmentPolicy &policy);

TreePlacementResult build_tree_placement(
    const TreePlacementInput &input,
    const AdaptationSnapshot &snapshot,
    const PerformanceOptimizationPolicy &policy);

} // namespace hotstuff

#endif
