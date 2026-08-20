#include "catch.hpp"

#include <memory>
#include <stdexcept>
#include <utility>
#include <vector>

#include "hotstuff/adaptation_manager_profile.h"
#include "hotstuff/adaptive_manager_session_facade.h"

namespace {

using namespace hotstuff;

constexpr std::uint64_t kSeed = 0xa2f7;

std::vector<ReplicaID> members()
{
    return {0, 1, 2, 3, 4, 5, 6};
}

PrivKeySecp256k1 issuer_key()
{
    PrivKeySecp256k1 key;
    key.from_hex("4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
    return key;
}

AdaptiveV2ManagerSessionConfig v2_config(
    const std::vector<ReplicaID> &replicas,
    const AdaptiveV2ManagerRuntimeShape &shape)
{
    AdaptiveV2ManagerSessionConfig config;
    config.active_tree_id = 0;
    config.activation_generation = 3;
    config.ingress_limits = shape.ingress_limits;
    config.controller.selection.required_nonresponsive = shape.required_nonresponsive;
    config.controller.selection.minimum_score_drop = shape.minimum_score_drop;
    config.controller.selection.minimum_timeouts_per_reporter = 2;
    config.controller.selection.maximum_post_baseline_timeout_attempts =
        shape.maximum_post_baseline_timeout_attempts;
    auto &responsiveness = config.controller.selection.responsiveness_policy;
    responsiveness.policy_version = "facade-v2";
    responsiveness.attempt_window = 32;
    responsiveness.minimum_attempts = 2;
    responsiveness.minimum_response_rate_ppm = 750000;
    responsiveness.maximum_timeout_rate_ppm = 250000;
    responsiveness.trailing_timeout_streak = 2;
    responsiveness.latency_percentile_basis_points = 5000;
    config.controller.selection.snapshot_seed = kSeed;
    config.controller.placement = {replicas, shape.tree_shape, kSeed, "facade-v2"};
    config.controller.activation_delay_blocks = 5;
    config.controller.issuer_id = 17;
    config.controller.issuer_private_key = issuer_key();
    config.controller.bundle_limits = shape.bundle_limits;
    config.retry_interval_ticks = 2;
    config.maximum_attempts_per_recipient = 3;
    config.convergence_window_ticks = 100;
    return config;
}

struct BlsMembership {
    std::vector<std::pair<ReplicaID, PubKeyBLS>> public_keys;

    explicit BlsMembership(const std::vector<ReplicaID> &replicas)
    {
        for (const auto replica : replicas) {
            auto key = std::make_shared<PrivKeyBLS>();
            key->from_rand();
            public_keys.emplace_back(replica, PubKeyBLS(*key));
        }
    }
};

AdaptiveV3ManagerSessionConfig v3_config(
    const std::vector<ReplicaID> &replicas,
    const AdaptiveV2ManagerRuntimeShape &shape,
    const EpochDefinitionInput &initial, const BlsMembership &bls)
{
    auto v2 = v2_config(replicas, shape);
    AdaptiveV3ManagerSessionConfig config;
    config.active_tree_id = v2.active_tree_id;
    config.activation_generation = v2.activation_generation;
    config.ingress_limits = v2.ingress_limits;
    config.controller = v2.controller;
    config.controller.successor_protocol_mode = EpochProtocolMode::adaptive_v3;
    for (std::size_t index = 0; index < shape.tree_shape.tree_count; ++index) {
        const auto &tree = initial.trees.at(index);
        config.controller.transition_policy.containment_baseline_roots.push_back(
            {tree.tree_id, tree.members_breadth_first.front()});
    }
    config.readiness_membership = bls.public_keys;
    config.required_release_count = replicas.size();
    config.maximum_delivery_attempts = 3;
    config.retry_interval_ticks = 2;
    config.pre_certificate_window_ticks = 100;
    config.delivery_window_ticks = 100;
    config.residency_ticks = 65000;
    config.expected_cycle_count = 2;
    config.wire_limits = {64 * 1024, replicas.size()};
    return config;
}

AdaptiveV2TransitionPolicy containment_policy(
    const EpochDefinitionInput &initial, std::uint32_t tree_count)
{
    AdaptiveV2TransitionPolicy policy;
    policy.intent = TreePolicyKind::fault_containment;
    const auto member_count = initial.trees.front().members_breadth_first.size();
    for (std::uint32_t index = 0; index < tree_count; ++index) {
        const auto &tree = initial.trees.at(index);
        policy.containment_baseline_roots.push_back({
            tree.tree_id,
            static_cast<ReplicaID>((tree.tree_id + 2) % member_count)});
    }
    return policy;
}

TEST_CASE("adaptive manager facade partitions v2 without changing convergence ownership",
          "[adaptive-v2][manager-session][facade]")
{
    const auto replicas = members();
    const auto shape = *derive_adaptive_v2_manager_runtime_shape(replicas, 2, 2);
    const auto initial = *derive_adaptive_v2_cyclic_epoch_zero(replicas, 2, 2);
    AdaptiveManagerSessionFacadeConfig config;
    config.mode = AdaptiveManagerSessionMode::adaptive_v2;
    config.v2 = v2_config(replicas, shape);

    AdaptiveManagerSessionFacade facade(replicas, initial, std::move(config));
    REQUIRE(facade.mode() == AdaptiveManagerSessionMode::adaptive_v2);
    REQUIRE(facade.v2() != nullptr);
    REQUIRE(facade.v3() == nullptr);
    REQUIRE(facade.v2_terminal_records() != nullptr);
    REQUIRE(facade.v3_terminal_records() == nullptr);
    REQUIRE(facade.begin_cycle(
        containment_policy(initial, shape.tree_shape.tree_count)));
    REQUIRE(facade.controller_audit().has_value());
    REQUIRE_FALSE(facade.v2_convergence_status().has_value());
}

TEST_CASE("adaptive manager facade exposes v3 fault and audit ownership without identity input",
          "[adaptive-v3][manager-session][facade]")
{
    const auto replicas = members();
    const auto shape = *derive_adaptive_v2_manager_runtime_shape(replicas, 2, 2);
    const auto initial = *derive_adaptive_v2_cyclic_epoch_zero(replicas, 2, 2);
    const BlsMembership bls(replicas);
    AdaptiveManagerSessionFacadeConfig config;
    config.mode = AdaptiveManagerSessionMode::adaptive_v3;
    config.v3 = v3_config(replicas, shape, initial, bls);

    AdaptiveManagerSessionFacade facade(replicas, initial, std::move(config));
    REQUIRE(facade.v2() == nullptr);
    REQUIRE(facade.v3() != nullptr);
    REQUIRE(facade.v3_status() == AdaptiveV3ManagerSessionStatus::idle);
    REQUIRE(facade.begin_cycle(
        containment_policy(initial, shape.tree_shape.tree_count)));
    REQUIRE(facade.v3_arm_hard_deadline(1'000'000));
    REQUIRE(facade.controller_audit().has_value());
    REQUIRE_FALSE(facade.v3_e2_eligible(0));
    REQUIRE(facade.v3_terminal_records() != nullptr);
}

TEST_CASE("adaptive manager facade rejects missing or mixed protocol configuration",
          "[adaptive][manager-session][facade]")
{
    const auto replicas = members();
    const auto shape = *derive_adaptive_v2_manager_runtime_shape(replicas, 2, 2);
    const auto initial = *derive_adaptive_v2_cyclic_epoch_zero(replicas, 2, 2);
    const BlsMembership bls(replicas);

    AdaptiveManagerSessionFacadeConfig missing;
    REQUIRE_THROWS_AS(
        AdaptiveManagerSessionFacade(replicas, initial, std::move(missing)),
        std::invalid_argument);

    AdaptiveManagerSessionFacadeConfig mixed;
    mixed.mode = AdaptiveManagerSessionMode::adaptive_v2;
    mixed.v2 = v2_config(replicas, shape);
    mixed.v3 = v3_config(replicas, shape, initial, bls);
    REQUIRE_THROWS_AS(
        AdaptiveManagerSessionFacade(replicas, initial, std::move(mixed)),
        std::invalid_argument);
}

} // namespace
