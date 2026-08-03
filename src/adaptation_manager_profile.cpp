#include "hotstuff/adaptation_manager_profile.h"

#include <algorithm>
#include <limits>
#include <utility>

namespace hotstuff
{
namespace
{

constexpr std::uint32_t kTimeoutsPerReporter = 2;
constexpr std::size_t kMaximumExactProposals = 8192;
constexpr std::size_t kMaximumEvidenceRecords = 131072;
constexpr std::size_t kMaximumQuarantinedRecords = 1024;
constexpr std::size_t kMaximumQuarantinedBytes = 256 * 1024;
constexpr std::size_t kMaximumQuarantinedSignerEntries = 8192;
constexpr std::size_t kMaximumQuarantinedPerReporter = 128;
constexpr std::size_t kMinimumDefinitionPayloadBytes = 32 * 1024;
constexpr std::size_t kMinimumBundlePayloadBytes = 64 * 1024;
constexpr std::size_t kMaximumCommandBytes = 4096;
constexpr std::size_t kDefinitionFixedAllowanceBytes = 1024;
constexpr std::size_t kDefinitionBytesPerMemberEntry = 16;
constexpr std::size_t kBundleFramingAllowanceBytes = 1024;

bool checked_add(
    std::size_t left,
    std::size_t right,
    std::size_t &result) noexcept
{
    if (left > std::numeric_limits<std::size_t>::max() - right)
        return false;
    result = left + right;
    return true;
}

bool checked_multiply(
    std::size_t left,
    std::size_t right,
    std::size_t &result) noexcept
{
    if (left != 0 &&
        right > std::numeric_limits<std::size_t>::max() / left)
    {
        return false;
    }
    result = left * right;
    return true;
}

bool canonical_contiguous_membership(
    const std::vector<ReplicaID> &membership) noexcept
{
    if (membership.empty() ||
        membership.size() > kMaximumAdaptiveV2ManagerMembers ||
        membership.size() > kMaximumTreePolicyMembers ||
        membership.size() - 1 >
            std::numeric_limits<ReplicaID>::max())
    {
        return false;
    }

    for (std::size_t index = 0; index < membership.size(); ++index)
    {
        if (membership[index] != static_cast<ReplicaID>(index))
            return false;
    }
    return true;
}

std::optional<std::uint32_t> minimum_score_drop(
    std::uint32_t fault_threshold) noexcept
{
    if (fault_threshold == std::numeric_limits<std::uint32_t>::max())
        return std::nullopt;
    const auto reporters = fault_threshold + 1U;
    if (reporters >
        std::numeric_limits<std::uint32_t>::max() /
            kTimeoutsPerReporter)
    {
        return std::nullopt;
    }
    return reporters * kTimeoutsPerReporter;
}

std::optional<AdaptiveV2ManagerIngressLimits> ingress_limits(
    std::uint32_t replica_count)
{
    if (replica_count == 0 ||
        replica_count > kMaximumQuarantinedRecords)
    {
        return std::nullopt;
    }
    const auto maximum_quarantined_per_reporter =
        std::min(
            kMaximumQuarantinedPerReporter,
            kMaximumQuarantinedRecords /
                static_cast<std::size_t>(replica_count));
    AdaptiveV2ManagerIngressLimits limits;
    limits.maximum_members = replica_count;
    limits.readiness_wire.maximum_payload_bytes = 256;
    limits.lifecycle_wire.maximum_payload_bytes = 512;
    limits.evidence_wire = {4096, 8, replica_count};
    limits.proposal_index = {kMaximumExactProposals, 16};
    limits.evidence_store = {
        kMaximumEvidenceRecords, kMaximumEvidenceRecords};
    limits.lifecycle = {
        kMaximumQuarantinedRecords,
        kMaximumQuarantinedBytes,
        replica_count,
        kMaximumQuarantinedSignerEntries,
        kMaximumQuarantinedRecords,
        replica_count,
        maximum_quarantined_per_reporter};
    limits.lifecycle_accounting = {
        kMaximumQuarantinedRecords,
        kMaximumQuarantinedBytes,
        kMaximumQuarantinedSignerEntries};
    limits.maximum_pending_lifecycle_facts_per_source =
        kMaximumAdaptiveV2PendingLifecycleFactsPerSource;
    return limits;
}

std::optional<EpochChangeBundleLimits> bundle_limits(
    const ByzantineQuorum &quorum)
{
    std::size_t member_entries = 0;
    std::size_t entries_per_tree = 0;
    std::size_t definition_bytes = 0;
    std::size_t bundle_bytes = 0;
    if (!checked_add(
            quorum.replica_count,
            quorum.fault_threshold,
            entries_per_tree) ||
        !checked_multiply(
            quorum.quorum, entries_per_tree, member_entries) ||
        !checked_multiply(
            member_entries,
            kDefinitionBytesPerMemberEntry,
            definition_bytes) ||
        !checked_add(
            definition_bytes,
            kDefinitionFixedAllowanceBytes,
            definition_bytes) ||
        !checked_add(
            definition_bytes, kMaximumCommandBytes, bundle_bytes) ||
        !checked_add(
            bundle_bytes,
            kBundleFramingAllowanceBytes,
            bundle_bytes))
    {
        return std::nullopt;
    }

    return EpochChangeBundleLimits{
        std::max(kMinimumBundlePayloadBytes, bundle_bytes),
        kMaximumCommandBytes,
        EpochWireLimits{
            std::max(
                kMinimumDefinitionPayloadBytes, definition_bytes),
            quorum.quorum,
            quorum.replica_count,
            128,
            quorum.fault_threshold}};
}

} // namespace

std::optional<AdaptiveV2ManagerRuntimeShape>
derive_adaptive_v2_manager_runtime_shape(
    const std::vector<ReplicaID> &membership,
    std::uint32_t tree_fanout,
    std::uint32_t pipeline_stretch) noexcept
{
    if (!canonical_contiguous_membership(membership) ||
        tree_fanout == 0 ||
        tree_fanout > kMaximumTreePolicyFanout ||
        pipeline_stretch == 0 ||
        pipeline_stretch > kMaximumTreePolicyPipelineStretch)
    {
        return std::nullopt;
    }

    const auto quorum = derive_byzantine_quorum(membership.size());
    if (!quorum.has_value() || quorum->fault_threshold == 0 ||
        quorum->quorum > kMaximumTreePolicyTrees)
    {
        return std::nullopt;
    }

    const auto score_drop = minimum_score_drop(
        quorum->fault_threshold);
    const auto derived_ingress_limits = ingress_limits(
        quorum->replica_count);
    const auto derived_bundle_limits = bundle_limits(*quorum);
    if (!score_drop.has_value() ||
        !derived_ingress_limits.has_value() ||
        !derived_bundle_limits.has_value())
        return std::nullopt;

    AdaptiveV2ManagerRuntimeShape shape;
    shape.quorum = *quorum;
    shape.required_nonresponsive = quorum->fault_threshold;
    shape.minimum_score_drop = *score_drop;
    shape.maximum_post_baseline_timeout_attempts =
        derived_ingress_limits->evidence_store
            .maximum_accepted_records;
    shape.tree_shape = {
        tree_fanout, pipeline_stretch, quorum->quorum};
    shape.ingress_limits = *derived_ingress_limits;
    shape.bundle_limits = *derived_bundle_limits;
    return shape;
}

std::optional<EpochDefinitionInput>
derive_adaptive_v2_cyclic_epoch_zero(
    const std::vector<ReplicaID> &membership,
    std::uint32_t tree_fanout,
    std::uint32_t pipeline_stretch)
{
    if (!derive_adaptive_v2_manager_runtime_shape(
             membership, tree_fanout, pipeline_stretch)
             .has_value())
    {
        return std::nullopt;
    }

    std::vector<EpochTreeDefinition> trees;
    trees.reserve(membership.size());
    for (std::size_t root = 0; root < membership.size(); ++root)
    {
        std::vector<ReplicaID> breadth_first;
        breadth_first.reserve(membership.size());
        for (std::size_t offset = 0;
             offset < membership.size();
             ++offset)
        {
            breadth_first.push_back(
                membership[(root + offset) % membership.size()]);
        }
        trees.push_back(EpochTreeDefinition{
            static_cast<std::uint32_t>(root),
            tree_fanout,
            pipeline_stretch,
            std::move(breadth_first),
            {}});
    }
    return adaptive_v2_epoch_zero_input(
        membership, std::move(trees));
}

} // namespace hotstuff
