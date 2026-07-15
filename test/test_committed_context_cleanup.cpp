#include <cstdint>
#include <memory>
#include <optional>
#include <set>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/proposal_context.h"
#include "support/bls_fixtures.h"

using hotstuff::ConfigurationId;
using hotstuff::DataStream;
using hotstuff::ProposalContextEvent;
using hotstuff::ProposalContextLifecycle;
using hotstuff::ProposalContextMetadata;
using hotstuff::ProposalContextStatus;
using hotstuff::ProposalKey;
using hotstuff::ProposalTransitionResult;
using hotstuff::ProposalTreeSnapshot;
using hotstuff::QuorumCertAggBLS;
using hotstuff::ReplicaID;
using hotstuff::quorum_cert_bt;
using hotstuff::uint256_t;
using hotstuff::test::BlsTestCore;

namespace
{

uint256_t digest(const std::string &label)
{
    return DataStream(label).get_hash();
}

ConfigurationId configuration(std::uint32_t epoch,
                              std::uint32_t tree,
                              const std::string &definition)
{
    return ConfigurationId{epoch, tree, digest(definition)};
}

ProposalTreeSnapshot root_tree()
{
    ProposalTreeSnapshot tree;
    tree.local_replica = 0;
    tree.root = 0;
    tree.parent = std::nullopt;
    tree.direct_children = {1, 2};
    tree.assigned_subtree = {0, 1, 2, 3, 4, 5, 6};
    tree.child_subtrees = {{1, {1, 3, 4}}, {2, {2, 5, 6}}};
    tree.fanout = 2;
    tree.pipeline_stretch = 2;
    return tree;
}

ProposalTreeSnapshot non_root_tree()
{
    ProposalTreeSnapshot tree;
    tree.local_replica = 1;
    tree.root = 0;
    tree.parent = 0;
    tree.direct_children = {3, 4};
    tree.assigned_subtree = {1, 3, 4};
    tree.child_subtrees = {{3, {3}}, {4, {4}}};
    tree.fanout = 2;
    tree.pipeline_stretch = 2;
    return tree;
}

ProposalTreeSnapshot leaf_tree()
{
    ProposalTreeSnapshot tree;
    tree.local_replica = 3;
    tree.root = 0;
    tree.parent = 1;
    tree.assigned_subtree = {3};
    tree.fanout = 2;
    tree.pipeline_stretch = 2;
    return tree;
}

ProposalContextMetadata metadata(const ProposalKey &key)
{
    return ProposalContextMetadata{key, root_tree(), 5};
}

ProposalContextMetadata metadata(const ProposalKey &key,
                                 ProposalTreeSnapshot tree)
{
    return ProposalContextMetadata{key, std::move(tree), 5};
}

quorum_cert_bt empty_accumulator(BlsTestCore &core,
                                 const ProposalKey &key)
{
    return new QuorumCertAggBLS(core.get_config(), key);
}

template<typename Lifecycle, typename = void>
struct has_committed_block_cleanup : std::false_type
{};

template<typename Lifecycle>
struct has_committed_block_cleanup<
    Lifecycle,
    std::void_t<decltype(std::declval<Lifecycle &>()
                             .close_committed_block(
                                 std::declval<const uint256_t &>()))>>
    : std::true_type
{};

template<typename Lifecycle>
void check_committed_block_cleanup()
{
    if constexpr (!has_committed_block_cleanup<Lifecycle>::value)
    {
        FAIL("ProposalContextLifecycle must expose "
             "close_committed_block(block_hash) and return every exact key "
             "closed through its block-hash secondary index");
    }
    else
    {
        Lifecycle contexts;
        BlsTestCore core(7);
        const auto shared_hash = digest("committed-shared-block");
        const ProposalKey key_a{
            configuration(61, 1, "committed-configuration-a"),
            shared_hash};
        const ProposalKey key_b{
            configuration(62, 2, "committed-configuration-b"),
            shared_hash};
        const ProposalKey unrelated{
            configuration(63, 3, "unrelated-configuration"),
            digest("unrelated-open-block")};

        auto lease_a = contexts.admit_remote(metadata(key_a));
        auto lease_b = contexts.admit_remote(metadata(key_b));
        auto unrelated_lease = contexts.admit_remote(metadata(unrelated));
        REQUIRE(lease_a.has_value());
        REQUIRE(lease_b.has_value());
        REQUIRE(unrelated_lease.has_value());

        REQUIRE(contexts.initialize_accumulator(
            *lease_a, empty_accumulator(core, key_a)));
        REQUIRE(contexts.initialize_accumulator(
            *lease_b, empty_accumulator(core, key_b)));
        REQUIRE(contexts.record_latency_start(*lease_a, 1));
        REQUIRE(contexts.record_latency_start(*lease_b, 2));

        std::size_t cancellations_a = 0;
        std::size_t cancellations_b = 0;
        auto owner_a = std::make_shared<int>(1);
        auto owner_b = std::make_shared<int>(2);
        std::weak_ptr<int> owner_a_lifetime = owner_a;
        std::weak_ptr<int> owner_b_lifetime = owner_b;
        REQUIRE(contexts.arm_timer(
                    *lease_a,
                    [owner_a, &cancellations_a]() {
                        ++cancellations_a;
                    }) != 0);
        REQUIRE(contexts.arm_timer(
                    *lease_b,
                    [owner_b, &cancellations_b]() {
                        ++cancellations_b;
                    }) != 0);
        owner_a.reset();
        owner_b.reset();

        REQUIRE(contexts.transition(
                    *lease_b,
                    ProposalContextEvent::aggregation_timeout) ==
                ProposalTransitionResult::retained_open);
        const auto timed_out = contexts.snapshot(key_b);
        REQUIRE(timed_out.has_value());
        CHECK(timed_out->pass_through);

        const auto before = contexts.storage_stats();
        CHECK(before.retained_tree_snapshots == 3);
        CHECK(before.retained_runtime_states == 3);
        CHECK(before.retained_accumulators == 2);
        CHECK(before.retained_latency_entries == 2);
        CHECK(before.terminal_tombstones == 0);

        // The lifecycle is the final owner of both committed proposal trees.
        lease_a.reset();
        lease_b.reset();
        const auto closed = contexts.close_committed_block(shared_hash);
        const std::set<ProposalKey> closed_keys(
            closed.begin(), closed.end());
        CHECK(closed.size() == 2);
        CHECK(closed_keys == std::set<ProposalKey>{key_a, key_b});

        CHECK(contexts.context_status(key_a) ==
              ProposalContextStatus::terminal_closed);
        CHECK(contexts.context_status(key_b) ==
              ProposalContextStatus::terminal_closed);
        CHECK(contexts.context_status(unrelated) ==
              ProposalContextStatus::admitted_open);
        CHECK(contexts.revalidate(*unrelated_lease));
        CHECK(cancellations_a == 1);
        CHECK(cancellations_b == 1);
        CHECK(owner_a_lifetime.expired());
        CHECK(owner_b_lifetime.expired());

        const auto after = contexts.storage_stats();
        CHECK(after.retained_tree_snapshots == 1);
        CHECK(after.retained_runtime_states == 1);
        CHECK(after.retained_accumulators == 0);
        CHECK(after.retained_latency_entries == 0);
        CHECK(after.terminal_tombstones == 2);

        const auto repeated =
            contexts.close_committed_block(shared_hash);
        CHECK(repeated.empty());
        CHECK(cancellations_a == 1);
        CHECK(cancellations_b == 1);
        CHECK(contexts.revalidate(*unrelated_lease));
        CHECK(contexts.storage_stats().terminal_tombstones == 2);
    }
}

} // namespace

TEST_CASE("committed blocks close every same-hash exact context",
          "[rem-a06-02][proposal-context][commit][secondary-index]"
          "[intentional-red]")
{
    check_committed_block_cleanup<ProposalContextLifecycle>();
}

TEST_CASE("role-terminal contexts remain discoverable until block commit",
          "[proposal-retirement][proposal-context][terminal-before-commit]"
          "[secondary-index][intentional-red]")
{
    ProposalContextLifecycle contexts;
    const auto shared_hash = digest("terminal-before-commit-shared-block");
    const ProposalKey leaf_key{
        configuration(65, 1, "terminal-before-commit-leaf"),
        shared_hash};
    const ProposalKey non_root_key{
        configuration(66, 1, "terminal-before-commit-non-root"),
        shared_hash};
    const ProposalKey root_key{
        configuration(67, 1, "terminal-before-commit-root"),
        shared_hash};

    auto leaf = contexts.admit_remote(metadata(leaf_key, leaf_tree()));
    REQUIRE(leaf.has_value());
    REQUIRE(contexts.record_local_signer(*leaf));
    REQUIRE(contexts.transition(
                *leaf, ProposalContextEvent::leaf_vote_enqueued) ==
            ProposalTransitionResult::terminal_closed);

    auto non_root = contexts.admit_remote(
        metadata(non_root_key, non_root_tree()));
    REQUIRE(non_root.has_value());
    REQUIRE(contexts.record_local_signer(*non_root));
    REQUIRE(contexts.record_verified_direct(*non_root, 3, 3));
    REQUIRE(contexts.record_verified_direct(*non_root, 4, 4));
    REQUIRE(contexts.transition(
                *non_root,
                ProposalContextEvent::non_root_aggregate_enqueued) ==
            ProposalTransitionResult::terminal_closed);

    auto root = contexts.admit_local(metadata(root_key, root_tree()));
    REQUIRE(root.has_value());
    REQUIRE(contexts.record_local_signer(*root));
    REQUIRE(contexts.record_verified_aggregate(
        *root, 1, std::set<ReplicaID>{1, 3, 4}));
    REQUIRE(contexts.record_verified_aggregate(
        *root, 2, std::set<ReplicaID>{2}));
    REQUIRE(contexts.transition(
                *root, ProposalContextEvent::root_qc_published) ==
            ProposalTransitionResult::terminal_closed);

    CHECK(contexts.context_status(leaf_key) ==
          ProposalContextStatus::terminal_closed);
    CHECK(contexts.context_status(non_root_key) ==
          ProposalContextStatus::terminal_closed);
    CHECK(contexts.context_status(root_key) ==
          ProposalContextStatus::terminal_closed);

    const auto committed = contexts.close_committed_block(shared_hash);
    const std::set<ProposalKey> committed_keys(
        committed.begin(), committed.end());
    CHECK(committed.size() == 3);
    CHECK(committed_keys ==
          std::set<ProposalKey>{leaf_key, non_root_key, root_key});
    CHECK(contexts.close_committed_block(shared_hash).empty());
}

TEST_CASE("exact close still isolates a same-hash configuration",
          "[rem-a06-02][proposal-context][commit][same-hash][control]")
{
    ProposalContextLifecycle contexts;
    const auto shared_hash = digest("exact-close-shared-block");
    const ProposalKey key_a{
        configuration(64, 1, "exact-close-configuration-a"),
        shared_hash};
    const ProposalKey key_b{
        configuration(64, 1, "exact-close-configuration-b"),
        shared_hash};
    auto lease_a = contexts.admit_remote(metadata(key_a));
    auto lease_b = contexts.admit_remote(metadata(key_b));
    REQUIRE(lease_a.has_value());
    REQUIRE(lease_b.has_value());

    REQUIRE(contexts.close(key_a, ProposalContextEvent::committed));
    CHECK(contexts.context_status(key_a) ==
          ProposalContextStatus::terminal_closed);
    CHECK(contexts.context_status(key_b) ==
          ProposalContextStatus::admitted_open);
    CHECK_FALSE(contexts.revalidate(*lease_a));
    CHECK(contexts.revalidate(*lease_b));
}
