#include <cstdint>
#include <vector>

#include "catch.hpp"
#include "hotstuff/adaptation_manager_profile.h"
#include "hotstuff/adaptive_v2_manager_ingress.h"

namespace
{

using hotstuff::ReplicaID;

std::vector<ReplicaID> membership(std::size_t count)
{
    std::vector<ReplicaID> members;
    members.reserve(count);
    for (std::size_t index = 0; index < count; ++index)
        members.push_back(static_cast<ReplicaID>(index));
    return members;
}

TEST_CASE(
    "manager runtime profile preserves the existing N7 quorum shape",
    "[adaptive-v2][manager-profile][n7][unit]")
{
    const auto derived =
        hotstuff::derive_adaptive_v2_manager_runtime_shape(
            membership(7), 2, 2);

    REQUIRE(derived.has_value());
    CHECK(derived->quorum.replica_count == 7);
    CHECK(derived->quorum.fault_threshold == 2);
    CHECK(derived->quorum.quorum == 5);
    CHECK(derived->required_nonresponsive == 2);
    CHECK(derived->tree_shape.fanout == 2);
    CHECK(derived->tree_shape.pipeline_stretch == 2);
    CHECK(derived->tree_shape.tree_count == 5);
    CHECK(derived->ingress_limits.maximum_members == 7);
    CHECK(
        derived->ingress_limits.evidence_wire
            .maximum_signers_per_observation == 7);
    CHECK(
        derived->ingress_limits.lifecycle.maximum_reporter_queues == 7);
    CHECK(
        derived->ingress_limits.lifecycle.maximum_lifecycle_sources == 7);
    CHECK(
        derived->ingress_limits.lifecycle
            .maximum_quarantined_records_per_reporter == 128);
    CHECK(derived->bundle_limits.definition_limits.maximum_trees == 5);
    CHECK(
        derived->bundle_limits.definition_limits
            .maximum_members_per_tree == 7);
    CHECK(
        derived->bundle_limits.definition_limits
            .maximum_wait_exempt_leaves_per_tree == 2);
}

TEST_CASE(
    "manager runtime profile derives N31 fanout-five bounds",
    "[adaptive-v2][manager-profile][n31][unit]")
{
    const auto derived =
        hotstuff::derive_adaptive_v2_manager_runtime_shape(
            membership(31), 5, 2);

    REQUIRE(derived.has_value());
    CHECK(derived->quorum.replica_count == 31);
    CHECK(derived->quorum.fault_threshold == 10);
    CHECK(derived->quorum.quorum == 21);
    CHECK(derived->required_nonresponsive == 10);
    CHECK(derived->tree_shape.fanout == 5);
    CHECK(derived->tree_shape.pipeline_stretch == 2);
    CHECK(derived->tree_shape.tree_count == 21);
    CHECK(derived->ingress_limits.maximum_members == 31);
    CHECK(
        derived->ingress_limits.evidence_wire
            .maximum_signers_per_observation == 31);
    CHECK(
        derived->ingress_limits.lifecycle.maximum_reporter_queues == 31);
    CHECK(
        derived->ingress_limits.lifecycle.maximum_lifecycle_sources == 31);
    CHECK(derived->bundle_limits.maximum_payload_bytes > 0);
    CHECK(derived->bundle_limits.maximum_command_bytes > 0);
    CHECK(
        derived->bundle_limits.maximum_command_bytes <=
        derived->bundle_limits.maximum_payload_bytes);
    CHECK(derived->bundle_limits.definition_limits.maximum_trees == 21);
    CHECK(
        derived->bundle_limits.definition_limits
            .maximum_members_per_tree == 31);
    CHECK(
        derived->bundle_limits.definition_limits
            .maximum_wait_exempt_leaves_per_tree == 10);
}

TEST_CASE(
    "canonical epoch zero preserves every cyclic N31 tree",
    "[adaptive-v2][manager-profile][n31][epoch-zero][unit]")
{
    const auto first =
        hotstuff::derive_adaptive_v2_cyclic_epoch_zero(
            membership(31), 5, 2);
    const auto second =
        hotstuff::derive_adaptive_v2_cyclic_epoch_zero(
            membership(31), 5, 2);

    REQUIRE(first.has_value());
    REQUIRE(second.has_value());
    REQUIRE(first->trees.size() == 31);
    REQUIRE(first->trees[30].tree_id == 30);
    REQUIRE(first->trees[30].members_breadth_first.size() == 31);
    CHECK(first->trees[30].members_breadth_first.front() == 30);
    for (std::size_t index = 1; index < 31; ++index)
    {
        CHECK(
            first->trees[30].members_breadth_first[index] ==
            static_cast<ReplicaID>(index - 1));
    }

    const auto first_digest = hotstuff::compute_epoch_digest(*first);
    const auto second_digest = hotstuff::compute_epoch_digest(*second);
    CHECK(first_digest != hotstuff::uint256_t{});
    CHECK(first_digest == second_digest);
    CHECK(
        first_digest.to_hex() ==
        "145fac093343fa9cff20fcf49d85ad5443e93db14146f7854b17e28cf44f6d7a");
}

TEST_CASE(
    "derived N31 ingress limits construct the real manager ingress",
    "[adaptive-v2][manager-profile][n31][ingress][unit]")
{
    const auto members = membership(31);
    const auto derived =
        hotstuff::derive_adaptive_v2_manager_runtime_shape(
            members, 5, 2);
    const auto epoch =
        hotstuff::derive_adaptive_v2_cyclic_epoch_zero(
            members, 5, 2);

    REQUIRE(derived.has_value());
    REQUIRE(epoch.has_value());
    CHECK(
        derived->ingress_limits.lifecycle
            .maximum_quarantined_records_per_reporter == 33);
    CHECK_NOTHROW([&] {
        hotstuff::AdaptiveV2ManagerIngress ingress(
            members,
            *epoch,
            0,
            1,
            derived->ingress_limits);
    }());
}

TEST_CASE(
    "canonical epoch zero preserves the legacy N7 cyclic topology",
    "[adaptive-v2][manager-profile][n7][epoch-zero][unit]")
{
    const auto epoch =
        hotstuff::derive_adaptive_v2_cyclic_epoch_zero(
            membership(7), 2, 2);

    REQUIRE(epoch.has_value());
    REQUIRE(epoch->trees.size() == 7);
    CHECK(epoch->trees.front().members_breadth_first == membership(7));
    CHECK(
        epoch->trees.back().members_breadth_first ==
        std::vector<ReplicaID>{6, 0, 1, 2, 3, 4, 5});
    CHECK(
        hotstuff::compute_epoch_digest(*epoch).to_hex() ==
        "f550407e56cc54a8fd4e93d1997ebe658b75699f4f2a9e955f4cc829b52bec81");
}

TEST_CASE(
    "manager runtime profile rejects invalid membership and topology",
    "[adaptive-v2][manager-profile][validation][unit]")
{
    CHECK_FALSE(
        hotstuff::derive_adaptive_v2_manager_runtime_shape(
            membership(30), 5, 2)
            .has_value());
    CHECK_FALSE(
        hotstuff::derive_adaptive_v2_manager_runtime_shape(
            membership(31), 0, 2)
            .has_value());
    CHECK_FALSE(
        hotstuff::derive_adaptive_v2_manager_runtime_shape(
            membership(31), 5, 0)
            .has_value());

    auto duplicate = membership(31);
    duplicate.back() = duplicate.front();
    CHECK_FALSE(
        hotstuff::derive_adaptive_v2_manager_runtime_shape(
            duplicate, 5, 2)
            .has_value());
    CHECK_FALSE(
        hotstuff::derive_adaptive_v2_cyclic_epoch_zero(
            membership(30), 5, 2)
            .has_value());
}

} // namespace
