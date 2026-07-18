#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/configuration.h"

/*
 * REM-A06-02 proposal-context contract.
 *
 * This test deliberately names a small production API instead of modeling an
 * active-epoch predicate in the test.  A vote/relay may be accepted only when
 * this coordinator can issue an exact, generation-bearing lease.  In
 * particular, activation is not admission and does not invalidate an older
 * admitted proposal that is still draining.
 *
 * The expected production header owns both lifecycle and configuration-
 * sensitive runtime state.  Timeout aggregation mechanics remain A06 scope;
 * this test specifies only the lifecycle fact that timeout/pass-through is an
 * open state rather than a terminal transition.
 */
#if __has_include("hotstuff/proposal_context.h")
#include "hotstuff/proposal_context.h"
#include "support/bls_fixtures.h"
#define KAURI_HAS_EXACT_PROPOSAL_CONTEXT 1
#else
#define KAURI_HAS_EXACT_PROPOSAL_CONTEXT 0
#endif

#if !KAURI_HAS_EXACT_PROPOSAL_CONTEXT

TEST_CASE("REM-A06-02 requires an exact proposal-context lifecycle",
          "[rem-a06-02][proposal-context][intentional-red]")
{
    FAIL("missing hotstuff/proposal_context.h: vote/relay state still has no "
         "unknown/buffered/open/closed/retired lifecycle or generation lease");
}

#else

namespace hotstuff
{
struct ProposalForwardingClaim;
}

using hotstuff::ConfigurationId;
using hotstuff::DataStream;
using hotstuff::EpochTreeDefinition;
using hotstuff::ProposalContextEvent;
using hotstuff::ProposalContextLease;
using hotstuff::ProposalContextLifecycle;
using hotstuff::ProposalContextMetadata;
using hotstuff::ProposalContextOrigin;
using hotstuff::ProposalContextSnapshot;
using hotstuff::ProposalContextStatus;
using hotstuff::ProposalKey;
using hotstuff::ProposalTransitionResult;
using hotstuff::ProposalTreeSnapshot;
using hotstuff::QuorumCert;
using hotstuff::QuorumCertAggBLS;
using hotstuff::ReplicaConfig;
using hotstuff::ReplicaID;
using hotstuff::part_cert_bt;
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

ProposalKey key(const ConfigurationId &configuration_id,
                const std::string &block)
{
    return ProposalKey{configuration_id, digest(block)};
}

using ChildSubtrees = std::map<ReplicaID, std::set<ReplicaID>>;

template<typename Metadata, typename = void>
struct has_frozen_global_quorum : std::false_type
{};

template<typename Metadata>
struct has_frozen_global_quorum<
    Metadata,
    std::void_t<decltype(std::declval<Metadata &>().global_quorum)>>
    : std::true_type
{};

template<typename Lifecycle, typename = void>
struct has_storage_stats : std::false_type
{};

template<typename Lifecycle>
struct has_storage_stats<
    Lifecycle,
    std::void_t<
        decltype(std::declval<const Lifecycle &>()
                     .storage_stats()
                     .retained_tree_snapshots),
        decltype(std::declval<const Lifecycle &>()
                     .storage_stats()
                     .retained_runtime_states),
        decltype(std::declval<const Lifecycle &>()
                     .storage_stats()
                     .terminal_tombstones),
        decltype(std::declval<const Lifecycle &>()
                     .storage_stats()
                     .retired_configuration_tombstones)>> : std::true_type
{};

template<typename Lifecycle, typename = void>
struct has_retirement_floor : std::false_type
{};

template<typename Lifecycle>
struct has_retirement_floor<
    Lifecycle,
    std::void_t<decltype(std::declval<Lifecycle &>()
                             .advance_retirement_floor(
                                 std::declval<std::uint32_t>()))>>
    : std::bool_constant<std::is_convertible_v<
          decltype(std::declval<Lifecycle &>().advance_retirement_floor(
              std::declval<std::uint32_t>())),
          std::size_t>>
{};

template<typename Lifecycle, typename = void>
struct has_open_context_floor_guard : std::false_type
{};

template<typename Lifecycle>
struct has_open_context_floor_guard<
    Lifecycle,
    std::void_t<decltype(std::declval<const Lifecycle &>()
                             .has_open_context_before_epoch(
                                 std::declval<std::uint32_t>()))>>
    : std::bool_constant<std::is_convertible_v<
          decltype(std::declval<const Lifecycle &>()
                       .has_open_context_before_epoch(
                           std::declval<std::uint32_t>())),
          bool>>
{};

template<typename Lifecycle, typename = void>
struct has_accumulator_runtime_api : std::false_type
{};

template<typename Lifecycle>
struct has_accumulator_runtime_api<
    Lifecycle,
    std::void_t<
        decltype(std::declval<Lifecycle &>().initialize_accumulator(
            std::declval<const ProposalContextLease &>(),
            std::declval<quorum_cert_bt>())),
        decltype(std::declval<Lifecycle &>().record_local_part(
            std::declval<const ProposalContextLease &>(),
            std::declval<const ReplicaConfig &>(),
            std::declval<ReplicaID>(),
            std::declval<const hotstuff::PartCert &>())),
        decltype(std::declval<Lifecycle &>()
                     .record_verified_direct_part(
                         std::declval<const ProposalContextLease &>(),
                         std::declval<const ReplicaConfig &>(),
                         std::declval<ReplicaID>(),
                         std::declval<ReplicaID>(),
                         std::declval<const hotstuff::PartCert &>())),
        decltype(std::declval<Lifecycle &>()
                     .record_verified_aggregate_certificate(
                         std::declval<const ProposalContextLease &>(),
                         std::declval<ReplicaID>(),
                         std::declval<const QuorumCert &>())),
        decltype(std::declval<const Lifecycle &>().clone_accumulator(
            std::declval<const ProposalContextLease &>())),
        decltype(std::declval<const Lifecycle &>()
                     .assigned_subtree_complete(
                         std::declval<const ProposalContextLease &>())),
        decltype(std::declval<Lifecycle &>().take_latency_us(
            std::declval<const ProposalContextLease &>(),
            std::declval<ReplicaID>())),
        decltype(std::declval<Lifecycle &>().shutdown())>>
    : std::true_type
{};

template<typename Lifecycle, typename = void>
struct has_frozen_root_qc_candidate : std::false_type
{};

template<typename Lifecycle>
struct has_frozen_root_qc_candidate<
    Lifecycle,
    std::void_t<decltype(
        std::declval<const Lifecycle &>().clone_publishable_root_qc(
            std::declval<const ProposalContextLease &>()))>>
    : std::bool_constant<std::is_same_v<
          decltype(std::declval<const Lifecycle &>()
                       .clone_publishable_root_qc(
                           std::declval<const ProposalContextLease &>())),
          quorum_cert_bt>>
{};

template<typename Lifecycle>
using root_qc_progress_claim_member_t = decltype(static_cast<
    bool (Lifecycle::*)(const ProposalContextLease &)>(
        &Lifecycle::claim_root_qc_progress));

template<typename Lifecycle, typename = void>
struct has_atomic_root_qc_progress_claim : std::false_type
{};

template<typename Lifecycle>
struct has_atomic_root_qc_progress_claim<
    Lifecycle,
    std::void_t<root_qc_progress_claim_member_t<Lifecycle>>>
    : std::true_type
{};

template<typename Lifecycle, typename = void>
struct has_accumulator_storage_stats : std::false_type
{};

template<typename Lifecycle>
struct has_accumulator_storage_stats<
    Lifecycle,
    std::void_t<
        decltype(std::declval<const Lifecycle &>()
                     .storage_stats()
                     .retained_accumulators),
        decltype(std::declval<const Lifecycle &>()
                     .storage_stats()
                     .retained_latency_entries)>> : std::true_type
{};

template<typename Lifecycle>
using forwarding_claim_result_t = decltype(
    std::declval<Lifecycle &>().claim_unforwarded_certificate(
        std::declval<const ProposalContextLease &>(),
        std::declval<quorum_cert_bt>()));

template<typename Lifecycle>
using forwarding_claim_value_t =
    typename forwarding_claim_result_t<Lifecycle>::value_type;

template<typename Lifecycle>
using forwarding_claim_member_t = decltype(static_cast<
    std::optional<forwarding_claim_value_t<Lifecycle>>
        (Lifecycle::*)(const ProposalContextLease &, quorum_cert_bt)>(
            &Lifecycle::claim_unforwarded_certificate));

template<typename Lifecycle, typename = void>
struct has_atomic_forwarding_claim : std::false_type
{};

template<typename Lifecycle>
struct has_atomic_forwarding_claim<
    Lifecycle,
    std::void_t<
        forwarding_claim_result_t<Lifecycle>,
        forwarding_claim_value_t<Lifecycle>,
        forwarding_claim_member_t<Lifecycle>,
        decltype(std::declval<
                     forwarding_claim_value_t<Lifecycle> &>()
                     .certificate),
        decltype(std::declval<
                     forwarding_claim_value_t<Lifecycle> &>()
                     .signers)>>
    : std::bool_constant<
          std::is_same_v<
              forwarding_claim_result_t<Lifecycle>,
              std::optional<forwarding_claim_value_t<Lifecycle>>> &&
          std::is_same_v<
              forwarding_claim_value_t<Lifecycle>,
              hotstuff::ProposalForwardingClaim> &&
          std::is_same_v<
              decltype(std::declval<
                           forwarding_claim_value_t<Lifecycle> &>()
                           .certificate),
              quorum_cert_bt> &&
          std::is_same_v<
              decltype(std::declval<
                           forwarding_claim_value_t<Lifecycle> &>()
                           .signers),
              std::set<ReplicaID>>>
{};

ProposalTreeSnapshot tree(ReplicaID local_replica,
                          ReplicaID root,
                          std::optional<ReplicaID> parent,
                          std::vector<ReplicaID> direct_children,
                          std::vector<ReplicaID> assigned_subtree,
                          ChildSubtrees child_subtrees)
{
    ProposalTreeSnapshot value;
    value.local_replica = local_replica;
    value.root = root;
    value.parent = parent;
    value.direct_children = std::move(direct_children);
    value.assigned_subtree = std::move(assigned_subtree);
    value.child_subtrees = std::move(child_subtrees);
    value.fanout = 2;
    value.pipeline_stretch = 2;
    return value;
}

ProposalTreeSnapshot root_tree()
{
    return tree(0, 0, std::nullopt, {1, 2},
                {0, 1, 2, 3, 4, 5, 6},
                {{1, {1, 3, 4}}, {2, {2, 5, 6}}});
}

ProposalTreeSnapshot six_member_root_tree()
{
    return tree(0, 0, std::nullopt, {1, 2},
                {0, 1, 2, 3, 4, 5},
                {{1, {1, 3, 4}}, {2, {2, 5}}});
}

ProposalTreeSnapshot non_root_tree()
{
    return tree(1, 0, 0, {3, 4}, {1, 3, 4},
                {{3, {3}}, {4, {4}}});
}

ProposalTreeSnapshot leaf_tree()
{
    return tree(3, 0, 1, {}, {3}, {});
}

template<typename Metadata>
Metadata metadata_as(
    const ProposalKey &proposal,
    ProposalTreeSnapshot snapshot = root_tree(),
    std::size_t global_quorum = 5)
{
    Metadata value{proposal, std::move(snapshot)};
    if constexpr (has_frozen_global_quorum<Metadata>::value)
        value.global_quorum = global_quorum;
    return value;
}

ProposalContextMetadata metadata(
    const ProposalKey &proposal,
    ProposalTreeSnapshot snapshot = root_tree(),
    std::size_t global_quorum = 5)
{
    return metadata_as<ProposalContextMetadata>(
        proposal, std::move(snapshot), global_quorum);
}

void check_set(const std::set<ReplicaID> &actual,
               std::initializer_list<ReplicaID> expected)
{
    CHECK(actual == std::set<ReplicaID>(expected));
}

void check_metadata_rejected_without_insertion(
    const ProposalTreeSnapshot &snapshot,
    const std::string &label)
{
    const auto config = configuration(14, 2, "invalid-tree-" + label);
    const auto proposal = key(config, "invalid-tree-block-" + label);
    const auto candidate = metadata(proposal, snapshot);

    ProposalContextLifecycle buffered;
    CHECK_FALSE(buffered.buffer_future(candidate));
    CHECK(buffered.proposal_entry_count(config) == 0);
    CHECK(buffered.context_status(proposal) ==
          ProposalContextStatus::unknown);

    ProposalContextLifecycle admitted;
    CHECK_FALSE(admitted.admit_remote(candidate).has_value());
    CHECK(admitted.proposal_entry_count(config) == 0);
    CHECK(admitted.context_status(proposal) ==
          ProposalContextStatus::unknown);
}

void check_root_quorum_rejected_without_insertion(
    std::size_t global_quorum,
    const std::string &label,
    ProposalTreeSnapshot snapshot = root_tree())
{
    const auto config = configuration(33, 1, "invalid-quorum-" + label);
    const auto proposal = key(config, "invalid-quorum-block-" + label);
    const auto candidate = metadata(
        proposal, std::move(snapshot), global_quorum);

    ProposalContextLifecycle buffered;
    CHECK_FALSE(buffered.buffer_future(candidate));
    CHECK(buffered.proposal_entry_count(config) == 0);
    CHECK(buffered.context_status(proposal) ==
          ProposalContextStatus::unknown);

    ProposalContextLifecycle admitted;
    CHECK_FALSE(admitted.admit_local(candidate).has_value());
    CHECK(admitted.proposal_entry_count(config) == 0);
    CHECK(admitted.context_status(proposal) ==
          ProposalContextStatus::unknown);
}

template<typename Metadata>
void check_frozen_root_quorum_contract()
{
    if constexpr (!has_frozen_global_quorum<Metadata>::value)
    {
        FAIL("ProposalContextMetadata must carry the immutable global_quorum "
             "required to authorize root QC publication");
    }
    else
    {
        ProposalContextLifecycle contexts;
        const auto config = configuration(15, 2, "frozen-root-quorum");
        const auto proposal = key(config, "frozen-root-quorum-block");
        Metadata candidate = metadata_as<Metadata>(
            proposal, root_tree(), 5);
        auto lease = contexts.admit_local(candidate);
        REQUIRE(lease.has_value());

        // The staging object is mutable, but the admitted threshold is not.
        candidate.global_quorum = 1;
        REQUIRE(contexts.record_local_signer(*lease));
        REQUIRE(contexts.record_verified_aggregate(
            *lease, 1, std::set<ReplicaID>{1, 3, 4}));

        CHECK(contexts.transition(
                  *lease, ProposalContextEvent::root_qc_published) ==
              ProposalTransitionResult::retained_open);
        CHECK(contexts.context_status(proposal) ==
              ProposalContextStatus::admitted_open);
        REQUIRE(contexts.revalidate(*lease));

        REQUIRE(contexts.record_verified_aggregate(
            *lease, 2, std::set<ReplicaID>{2}));
        CHECK(contexts.transition(
                  *lease, ProposalContextEvent::root_qc_published) ==
              ProposalTransitionResult::terminal_closed);
        CHECK(contexts.context_status(proposal) ==
              ProposalContextStatus::terminal_closed);
    }
}

struct ReentrantReleaseState
{
    ProposalContextLifecycle *contexts{nullptr};
    ProposalKey key;
    bool armed{false};
    bool released{false};
    std::size_t releases{0};
    bool close_result{false};
};

struct CloseContextOnRelease
{
    explicit CloseContextOnRelease(
        std::shared_ptr<ReentrantReleaseState> state)
        : state(std::move(state))
    {}

    ~CloseContextOnRelease()
    {
        if (!state || !state->armed || state->released)
            return;
        state->released = true;
        ++state->releases;
        if (state->contexts != nullptr)
            state->close_result = state->contexts->close(
                state->key, ProposalContextEvent::proposal_aborted);
    }

    void operator()() const noexcept
    {}

    std::shared_ptr<ReentrantReleaseState> state;
};

template<typename Lifecycle>
void check_terminal_storage_contract()
{
    Lifecycle contexts;
    const auto config = configuration(16, 2, "terminal-storage");
    const auto proposal = key(config, "terminal-storage-block");
    auto lease = contexts.admit_remote(metadata(proposal, root_tree()));
    REQUIRE(lease.has_value());

    REQUIRE(contexts.record_local_signer(*lease));
    REQUIRE(contexts.record_verified_aggregate(
        *lease, 1, std::set<ReplicaID>{1, 3, 4}));
    REQUIRE(contexts.mark_forwarded_signers(
        *lease, std::set<ReplicaID>{1, 3, 4}));
    REQUIRE(contexts.record_latency_start(*lease, 2));

    std::size_t timer_cancellations = 0;
    auto timer_owner = std::make_shared<int>(1);
    std::weak_ptr<int> timer_owner_lifetime = timer_owner;
    REQUIRE(contexts.arm_timer(
                *lease,
                [timer_owner, &timer_cancellations]() {
                    ++timer_cancellations;
                }) != 0);
    timer_owner.reset();

    if constexpr (has_storage_stats<Lifecycle>::value)
    {
        const auto open = contexts.storage_stats();
        CHECK(open.retained_tree_snapshots == 1);
        CHECK(open.retained_runtime_states == 1);
        CHECK(open.terminal_tombstones == 0);
        CHECK(open.retired_configuration_tombstones == 0);
    }

    // Only the lifecycle owns the tree/runtime now. Terminal close must reduce
    // that entry to an exact-key tombstone and release its timer owner.
    lease.reset();
    REQUIRE(contexts.close(proposal, ProposalContextEvent::committed));
    CHECK(timer_cancellations == 1);
    CHECK(timer_owner_lifetime.expired());
    CHECK(contexts.context_status(proposal) ==
          ProposalContextStatus::terminal_closed);
    CHECK(contexts.proposal_entry_count(config) == 1);
    CHECK_FALSE(contexts.acquire_open_context(proposal).has_value());
    CHECK_FALSE(contexts.snapshot(proposal).has_value());
    CHECK_FALSE(contexts.admit_remote(
        metadata(proposal, root_tree())).has_value());

    if constexpr (has_storage_stats<Lifecycle>::value)
    {
        const auto closed = contexts.storage_stats();
        CHECK(closed.retained_tree_snapshots == 0);
        CHECK(closed.retained_runtime_states == 0);
        CHECK(closed.terminal_tombstones == 1);
        CHECK(closed.retired_configuration_tombstones == 0);
    }
    else
    {
        FAIL("ProposalContextLifecycle::storage_stats() must expose retained "
             "tree/runtime ownership separately from minimal terminal and "
             "retired tombstones");
    }
}

template<typename Lifecycle>
void check_retirement_floor_contract()
{
    if constexpr (!(has_retirement_floor<Lifecycle>::value &&
                    has_storage_stats<Lifecycle>::value))
    {
        FAIL("ProposalContextLifecycle must expose a monotonic "
             "advance_retirement_floor(first_live_epoch) and bounded exact "
             "retirement-tombstone count");
    }
    else
    {
        Lifecycle contexts;
        const auto config_40 = configuration(40, 1, "retired-40");
        const auto config_41 = configuration(41, 1, "retired-41");
        const auto config_45 = configuration(45, 1, "retired-45");
        const auto key_40 = key(config_40, "retired-block-40");
        const auto key_41 = key(config_41, "retired-block-41");
        const auto key_45 = key(config_45, "retired-block-45");

        REQUIRE(contexts.admit_remote(metadata(key_40)).has_value());
        REQUIRE(contexts.retire_configuration(config_40) == 1);
        REQUIRE(contexts.admit_remote(metadata(key_41)).has_value());
        REQUIRE(contexts.retire_configuration(config_41) == 1);
        REQUIRE(contexts.admit_remote(metadata(key_45)).has_value());
        REQUIRE(contexts.retire_configuration(config_45) == 1);

        auto stats = contexts.storage_stats();
        CHECK(stats.retained_tree_snapshots == 0);
        CHECK(stats.retained_runtime_states == 0);
        CHECK(stats.terminal_tombstones == 0);
        CHECK(stats.retired_configuration_tombstones == 3);

        // first_live_epoch=42 replaces exact tombstones for 40 and 41 with a
        // monotonic epoch floor. No wall-clock or delayed callback is involved.
        CHECK(contexts.advance_retirement_floor(42) == 2);
        stats = contexts.storage_stats();
        CHECK(stats.retired_configuration_tombstones == 1);
        CHECK(contexts.context_status(key_40) ==
              ProposalContextStatus::retired);
        CHECK(contexts.context_status(key_41) ==
              ProposalContextStatus::retired);

        const auto unseen_old_config =
            configuration(40, 99, "unseen-retired-40");
        const auto unseen_old_key =
            key(unseen_old_config, "unseen-retired-block-40");
        CHECK(contexts.context_status(unseen_old_key) ==
              ProposalContextStatus::retired);
        CHECK_FALSE(contexts.buffer_future(metadata(unseen_old_key)));
        CHECK_FALSE(contexts.admit_remote(
            metadata(unseen_old_key)).has_value());
        CHECK(contexts.retire_configuration(unseen_old_config) == 0);

        // A stale attempt cannot move the floor backwards or resurrect ABA.
        CHECK(contexts.advance_retirement_floor(41) == 0);
        CHECK(contexts.storage_stats()
                  .retired_configuration_tombstones == 1);
        CHECK(contexts.context_status(unseen_old_key) ==
              ProposalContextStatus::retired);

        CHECK(contexts.advance_retirement_floor(46) == 1);
        CHECK(contexts.storage_stats()
                  .retired_configuration_tombstones == 0);
        CHECK(contexts.context_status(key_45) ==
              ProposalContextStatus::retired);
        CHECK_FALSE(contexts.admit_local(metadata(key_45)).has_value());

        // The floor is exclusive: its first live epoch remains admissible.
        Lifecycle boundary;
        CHECK(boundary.advance_retirement_floor(42) == 0);
        const auto floor_key = key(
            configuration(42, 1, "first-live-42"),
            "first-live-block-42");
        CHECK(boundary.context_status(floor_key) ==
              ProposalContextStatus::unknown);
        CHECK(boundary.admit_remote(metadata(floor_key)).has_value());
    }
}

template<typename Lifecycle>
void check_open_context_floor_guard()
{
    if constexpr (!has_open_context_floor_guard<Lifecycle>::value)
    {
        FAIL("ProposalContextLifecycle must report open exact contexts below "
             "a candidate retirement floor");
    }
    else
    {
        Lifecycle contexts;
        const auto old_configuration =
            configuration(8, 2, "open-draining-epoch");
        const auto new_configuration =
            configuration(9, 0, "active-successor-epoch");
        const auto old_key = key(
            old_configuration, "open-draining-proposal");
        const auto new_key = key(
            new_configuration, "active-successor-proposal");

        CHECK_FALSE(contexts.has_open_context_before_epoch(9));
        REQUIRE(contexts.admit_remote(metadata(old_key)).has_value());
        CHECK(contexts.has_open_context_before_epoch(9));

        contexts.activate_configuration(new_configuration);
        REQUIRE(contexts.admit_remote(metadata(new_key)).has_value());
        CHECK(contexts.has_open_context_before_epoch(9));

        REQUIRE(contexts.close(
            old_key, ProposalContextEvent::proposal_aborted));
        CHECK_FALSE(contexts.has_open_context_before_epoch(9));
        CHECK(contexts.context_status(old_key) ==
              ProposalContextStatus::terminal_closed);
    }
}

quorum_cert_bt empty_bls_accumulator(
    BlsTestCore &core,
    const ProposalKey &proposal)
{
    return new QuorumCertAggBLS(core.get_config(), proposal);
}

quorum_cert_bt verified_bls_aggregate(
    BlsTestCore &core,
    const ProposalKey &proposal,
    const std::vector<ReplicaID> &signers)
{
    auto certificate = empty_bls_accumulator(core, proposal);
    for (const auto signer : signers)
    {
        auto part = core.make_part(signer, proposal);
        certificate->add_part(core.get_config(), signer, *part);
    }
    certificate->compute();
    REQUIRE(certificate->verify(core.get_config()));
    return certificate;
}

std::set<ReplicaID> signer_set(const QuorumCert &certificate)
{
    const auto signers = certificate.get_signers();
    return {signers.begin(), signers.end()};
}

template<typename Lifecycle>
void check_atomic_frozen_root_qc_candidate()
{
    if constexpr (!has_frozen_root_qc_candidate<Lifecycle>::value)
    {
        FAIL("ProposalContextLifecycle must atomically clone a publishable "
             "root QC using the admitted context's frozen global_quorum");
    }
    else
    {
        Lifecycle contexts;
        BlsTestCore core(7);
        const auto proposal = key(
            configuration(53, 1, "frozen-publishable-root-qc"),
            "frozen-publishable-root-qc-block");
        auto lease = contexts.admit_local(
            metadata(proposal, root_tree(), 5));
        REQUIRE(lease.has_value());
        REQUIRE(contexts.initialize_accumulator(
            *lease, empty_bls_accumulator(core, proposal)));

        auto local = core.make_part(0, proposal);
        auto first_subtree = verified_bls_aggregate(
            core, proposal, {1, 3});
        REQUIRE(contexts.record_local_part(
            *lease, core.get_config(), 0, *local));
        REQUIRE(contexts.record_verified_aggregate_certificate(
            *lease, 1, *first_subtree));

        std::size_t mutable_live_threshold =
            core.get_config().nmajority;
        REQUIRE(mutable_live_threshold == 5);
        mutable_live_threshold = 3;
        auto partial = contexts.clone_accumulator(*lease);
        REQUIRE(partial != nullptr);
        REQUIRE(partial->has_n(mutable_live_threshold));

        INFO("a lowered live threshold cannot make three signers publishable "
             "for a context admitted with global_quorum five");
        CHECK(contexts.clone_publishable_root_qc(*lease) == nullptr);
        CHECK(contexts.context_status(proposal) ==
              ProposalContextStatus::admitted_open);

        auto second_subtree = verified_bls_aggregate(
            core, proposal, {2, 5});
        REQUIRE(contexts.record_verified_aggregate_certificate(
            *lease, 2, *second_subtree));

        auto candidate = contexts.clone_publishable_root_qc(*lease);
        auto independent_clone =
            contexts.clone_publishable_root_qc(*lease);
        REQUIRE(candidate != nullptr);
        REQUIRE(independent_clone != nullptr);
        CHECK(candidate.get() != independent_clone.get());
        CHECK(candidate->get_proposal_key() == proposal);
        CHECK(candidate->get_sigs_n() == 5);
        CHECK(signer_set(*candidate) ==
              std::set<ReplicaID>{0, 1, 2, 3, 5});
        candidate->compute();
        CHECK(candidate->verify(core.get_config()));
        CHECK(contexts.context_status(proposal) ==
              ProposalContextStatus::admitted_open);
    }
}

void check_storage_stats_equal(
    const hotstuff::ProposalContextStorageStats &actual,
    const hotstuff::ProposalContextStorageStats &expected)
{
    CHECK(actual.retained_tree_snapshots ==
          expected.retained_tree_snapshots);
    CHECK(actual.retained_runtime_states ==
          expected.retained_runtime_states);
    CHECK(actual.retained_accumulators ==
          expected.retained_accumulators);
    CHECK(actual.retained_latency_entries ==
          expected.retained_latency_entries);
    CHECK(actual.terminal_tombstones == expected.terminal_tombstones);
    CHECK(actual.retired_configuration_tombstones ==
          expected.retired_configuration_tombstones);
}

template<typename Lifecycle>
void check_root_qc_progress_claim_once()
{
    if constexpr (!has_atomic_root_qc_progress_claim<Lifecycle>::value)
        return;
    else
    {
        Lifecycle contexts;
        const auto proposal = key(
            configuration(70, 1, "root-qc-progress-once"),
            "root-qc-progress-once-block");
        auto lease = contexts.admit_local(
            metadata(proposal, root_tree()));
        REQUIRE(lease.has_value());

        const auto before = contexts.storage_stats();
        CHECK(contexts.claim_root_qc_progress(*lease));
        CHECK_FALSE(contexts.claim_root_qc_progress(*lease));
        check_storage_stats_equal(contexts.storage_stats(), before);
        CHECK(contexts.context_status(proposal) ==
              ProposalContextStatus::admitted_open);
        CHECK(contexts.revalidate(*lease));
    }
}

template<typename Lifecycle>
void check_root_qc_progress_claim_rejections()
{
    if constexpr (!has_atomic_root_qc_progress_claim<Lifecycle>::value)
        return;
    else
    {
        SECTION("a non-root context cannot claim root QC progress")
        {
            Lifecycle contexts;
            const auto proposal = key(
                configuration(71, 1, "root-qc-progress-non-root"),
                "root-qc-progress-non-root-block");
            auto lease = contexts.admit_remote(
                metadata(proposal, non_root_tree()));
            REQUIRE(lease.has_value());
            CHECK_FALSE(contexts.claim_root_qc_progress(*lease));
            CHECK(contexts.revalidate(*lease));
        }

        SECTION("a lease owned by another exact key is rejected")
        {
            Lifecycle contexts;
            Lifecycle other_contexts;
            const auto target = key(
                configuration(71, 2, "root-qc-progress-target"),
                "root-qc-progress-target-block");
            const auto wrong = key(
                configuration(71, 2, "root-qc-progress-wrong"),
                "root-qc-progress-wrong-block");
            auto target_lease = contexts.admit_local(
                metadata(target, root_tree()));
            auto wrong_lease = other_contexts.admit_local(
                metadata(wrong, root_tree()));
            REQUIRE(target_lease.has_value());
            REQUIRE(wrong_lease.has_value());

            CHECK_FALSE(contexts.claim_root_qc_progress(*wrong_lease));
            CHECK(contexts.claim_root_qc_progress(*target_lease));
        }

        SECTION("a terminal context rejects its stale lease")
        {
            Lifecycle contexts;
            const auto proposal = key(
                configuration(71, 3, "root-qc-progress-terminal"),
                "root-qc-progress-terminal-block");
            auto lease = contexts.admit_local(
                metadata(proposal, root_tree()));
            REQUIRE(lease.has_value());
            REQUIRE(contexts.close(
                proposal, ProposalContextEvent::committed));

            CHECK_FALSE(contexts.claim_root_qc_progress(*lease));
            CHECK_FALSE(contexts.revalidate(*lease));
            CHECK(contexts.context_status(proposal) ==
                  ProposalContextStatus::terminal_closed);
        }

        SECTION("a retired context rejects its stale lease")
        {
            Lifecycle contexts;
            const auto config = configuration(
                71, 4, "root-qc-progress-retired");
            const auto proposal = key(
                config, "root-qc-progress-retired-block");
            auto lease = contexts.admit_local(
                metadata(proposal, root_tree()));
            REQUIRE(lease.has_value());
            REQUIRE(contexts.retire_configuration(config) == 1);

            CHECK_FALSE(contexts.claim_root_qc_progress(*lease));
            CHECK_FALSE(contexts.revalidate(*lease));
            CHECK(contexts.context_status(proposal) ==
                  ProposalContextStatus::retired);
        }
    }
}

template<typename Lifecycle>
void check_root_qc_progress_exact_configuration_isolation()
{
    if constexpr (!has_atomic_root_qc_progress_claim<Lifecycle>::value)
        return;
    else
    {
        const auto shared_hash = digest(
            "root-qc-progress-shared-block");
        const ProposalKey key_a{
            configuration(72, 1, "root-qc-progress-config-a"),
            shared_hash};
        const ProposalKey key_b{
            configuration(72, 1, "root-qc-progress-config-b"),
            shared_hash};

        Lifecycle contexts;
        auto lease_a = contexts.admit_local(
            metadata(key_a, root_tree()));
        REQUIRE(lease_a.has_value());

        // A same-block lease for another configuration is not a lease for
        // key_a and cannot consume key_a's one-shot marker.
        Lifecycle other_contexts;
        auto foreign_b = other_contexts.admit_local(
            metadata(key_b, root_tree()));
        REQUIRE(foreign_b.has_value());
        CHECK_FALSE(contexts.claim_root_qc_progress(*foreign_b));
        CHECK(contexts.claim_root_qc_progress(*lease_a));
        CHECK_FALSE(contexts.claim_root_qc_progress(*lease_a));

        // When both exact contexts are legitimately admitted, each owns an
        // independent marker; a bare block hash never aliases them.
        auto lease_b = contexts.admit_local(
            metadata(key_b, root_tree()));
        REQUIRE(lease_b.has_value());
        CHECK(contexts.claim_root_qc_progress(*lease_b));
        CHECK_FALSE(contexts.claim_root_qc_progress(*lease_b));
    }
}

template<typename Lifecycle>
void check_root_qc_progress_claim_cleanup()
{
    if constexpr (!has_atomic_root_qc_progress_claim<Lifecycle>::value)
        return;
    else
    {
        Lifecycle contexts;
        const auto config = configuration(
            73, 1, "root-qc-progress-bounded-storage");
        constexpr std::size_t proposal_count = 32;

        for (std::size_t index = 0; index < proposal_count; ++index)
        {
            const auto proposal = key(
                config,
                "root-qc-progress-bounded-block-" +
                    std::to_string(index));
            auto lease = contexts.admit_local(
                metadata(proposal, root_tree()));
            REQUIRE(lease.has_value());
            const auto before_claim = contexts.storage_stats();
            REQUIRE(contexts.claim_root_qc_progress(*lease));
            CHECK_FALSE(contexts.claim_root_qc_progress(*lease));
            check_storage_stats_equal(
                contexts.storage_stats(), before_claim);
            REQUIRE(contexts.close(
                proposal, ProposalContextEvent::committed));
        }

        auto stats = contexts.storage_stats();
        CHECK(stats.retained_tree_snapshots == 0);
        CHECK(stats.retained_runtime_states == 0);
        CHECK(stats.retained_accumulators == 0);
        CHECK(stats.retained_latency_entries == 0);
        CHECK(stats.terminal_tombstones == proposal_count);
        CHECK(stats.retired_configuration_tombstones == 0);

        CHECK(contexts.retire_configuration(config) == proposal_count);
        stats = contexts.storage_stats();
        CHECK(stats.retained_tree_snapshots == 0);
        CHECK(stats.retained_runtime_states == 0);
        CHECK(stats.retained_accumulators == 0);
        CHECK(stats.retained_latency_entries == 0);
        CHECK(stats.terminal_tombstones == 0);
        CHECK(stats.retired_configuration_tombstones == 1);

        CHECK(contexts.advance_retirement_floor(
                  config.epoch_number + 1) == 1);
        CHECK(contexts.storage_stats()
                  .retired_configuration_tombstones == 0);
    }
}

class InconsistentAccumulatorCertificate final
    : public QuorumCertAggBLS
{
public:
    InconsistentAccumulatorCertificate(
        const ReplicaConfig &config,
        const ProposalKey &proposal)
        : QuorumCertAggBLS(config, proposal)
    {}

    std::vector<ReplicaID> get_signers() const override
    {
        return {3, 3};
    }

    std::size_t get_sigs_n() override
    {
        return 2;
    }

    InconsistentAccumulatorCertificate *clone() override
    {
        return new InconsistentAccumulatorCertificate(*this);
    }
};

template<typename Lifecycle>
void initialize_verified_direct_signers(
    Lifecycle &contexts,
    const ProposalContextLease &lease,
    BlsTestCore &core,
    std::initializer_list<ReplicaID> signers)
{
    REQUIRE(contexts.initialize_accumulator(
        lease, empty_bls_accumulator(core, lease.key())));
    for (const auto signer : signers)
    {
        auto part = core.make_part(signer, lease.key());
        REQUIRE(contexts.record_verified_direct_part(
            lease, core.get_config(), signer, signer, *part));
    }
}

template<typename Lifecycle>
void check_owned_one_shot_forwarding_claim()
{
    if constexpr (!has_atomic_forwarding_claim<Lifecycle>::value)
        return;
    else
    {
        Lifecycle contexts;
        BlsTestCore core(7, 1);
        const auto config = configuration(
            59, 1, "forwarding-claim-once");
        const auto proposal = key(
            config, "forwarding-claim-once-block");
        auto lease = contexts.admit_remote(
            metadata(proposal, non_root_tree()));
        REQUIRE(lease.has_value());
        initialize_verified_direct_signers(
            contexts, *lease, core, {3, 4});

        const auto before = contexts.snapshot(proposal);
        REQUIRE(before.has_value());
        CHECK(before->verified_signers ==
              std::set<ReplicaID>{3, 4});
        CHECK(before->forwarded_signers.empty());

        auto candidate = verified_bls_aggregate(
            core, proposal, {3, 4});
        auto *candidate_identity = candidate.get();
        auto claim = contexts.claim_unforwarded_certificate(
            *lease, std::move(candidate));
        CHECK(candidate == nullptr);
        REQUIRE(claim.has_value());
        REQUIRE(claim->certificate != nullptr);
        CHECK(claim->certificate.get() == candidate_identity);
        CHECK(claim->signers == std::set<ReplicaID>{3, 4});
        CHECK(signer_set(*claim->certificate) == claim->signers);
        CHECK(claim->certificate->get_proposal_key() == proposal);
        CHECK(claim->certificate->verify(core.get_config()));

        const auto after = contexts.snapshot(proposal);
        REQUIRE(after.has_value());
        CHECK(after->verified_signers == before->verified_signers);
        CHECK(after->forwarded_signers == claim->signers);

        auto duplicate = verified_bls_aggregate(
            core, proposal, {3, 4});
        CHECK_FALSE(contexts.claim_unforwarded_certificate(
            *lease, std::move(duplicate)).has_value());
        CHECK(duplicate == nullptr);
        const auto after_duplicate = contexts.snapshot(proposal);
        REQUIRE(after_duplicate.has_value());
        CHECK(after_duplicate->forwarded_signers ==
              after->forwarded_signers);

        // The returned certificate is independent of exact-context storage.
        // Retiring the configuration removes runtime ownership but cannot
        // invalidate the certificate already transferred to the caller.
        REQUIRE(contexts.retire_configuration(config) == 1);
        CHECK(contexts.context_status(proposal) ==
              ProposalContextStatus::retired);
        CHECK_FALSE(contexts.snapshot(proposal).has_value());
        CHECK(claim->certificate != nullptr);
        CHECK(claim->certificate->get_proposal_key() == proposal);
        CHECK(signer_set(*claim->certificate) == claim->signers);
        CHECK(claim->certificate->verify(core.get_config()));
    }
}

template<typename Lifecycle>
void check_forwarding_claim_rejections_are_atomic()
{
    if constexpr (!has_atomic_forwarding_claim<Lifecycle>::value)
        return;
    else
    {
        SECTION("a different block key is rejected")
        {
            Lifecycle contexts;
            BlsTestCore core(7, 1);
            const auto config = configuration(
                60, 1, "forwarding-claim-wrong-key");
            const auto proposal = key(
                config, "forwarding-claim-right-block");
            const auto wrong = key(
                config, "forwarding-claim-wrong-block");
            auto lease = contexts.admit_remote(
                metadata(proposal, non_root_tree()));
            REQUIRE(lease.has_value());
            initialize_verified_direct_signers(
                contexts, *lease, core, {3});

            auto wrong_candidate = verified_bls_aggregate(
                core, wrong, {3});
            CHECK_FALSE(contexts.claim_unforwarded_certificate(
                *lease, std::move(wrong_candidate)).has_value());
            const auto snapshot = contexts.snapshot(proposal);
            REQUIRE(snapshot.has_value());
            CHECK(snapshot->verified_signers ==
                  std::set<ReplicaID>{3});
            CHECK(snapshot->forwarded_signers.empty());
        }

        SECTION("a cryptographically valid but unrecorded signer is rejected")
        {
            Lifecycle contexts;
            BlsTestCore core(7, 1);
            const auto proposal = key(
                configuration(60, 2, "forwarding-claim-unverified"),
                "forwarding-claim-unverified-block");
            auto lease = contexts.admit_remote(
                metadata(proposal, non_root_tree()));
            REQUIRE(lease.has_value());
            initialize_verified_direct_signers(
                contexts, *lease, core, {3});

            auto unrecorded_candidate = verified_bls_aggregate(
                core, proposal, {4});
            CHECK_FALSE(contexts.claim_unforwarded_certificate(
                *lease, std::move(unrecorded_candidate)).has_value());
            const auto snapshot = contexts.snapshot(proposal);
            REQUIRE(snapshot.has_value());
            CHECK(snapshot->verified_signers ==
                  std::set<ReplicaID>{3});
            CHECK(snapshot->forwarded_signers.empty());
        }

        SECTION("overlap rejects the whole candidate without partial marking")
        {
            Lifecycle contexts;
            BlsTestCore core(7, 1);
            const auto proposal = key(
                configuration(60, 3, "forwarding-claim-overlap"),
                "forwarding-claim-overlap-block");
            auto lease = contexts.admit_remote(
                metadata(proposal, non_root_tree()));
            REQUIRE(lease.has_value());
            initialize_verified_direct_signers(
                contexts, *lease, core, {3, 4});
            REQUIRE(contexts.mark_forwarded_signers(
                *lease, std::set<ReplicaID>{3}));

            auto overlap = verified_bls_aggregate(
                core, proposal, {3, 4});
            CHECK_FALSE(contexts.claim_unforwarded_certificate(
                *lease, std::move(overlap)).has_value());
            const auto snapshot = contexts.snapshot(proposal);
            REQUIRE(snapshot.has_value());
            CHECK(snapshot->verified_signers ==
                  std::set<ReplicaID>{3, 4});
            CHECK(snapshot->forwarded_signers ==
                  std::set<ReplicaID>{3});
        }

        SECTION("duplicate signer enumeration is rejected")
        {
            Lifecycle contexts;
            BlsTestCore core(7, 1);
            const auto proposal = key(
                configuration(60, 4, "forwarding-claim-duplicates"),
                "forwarding-claim-duplicates-block");
            auto lease = contexts.admit_remote(
                metadata(proposal, non_root_tree()));
            REQUIRE(lease.has_value());
            initialize_verified_direct_signers(
                contexts, *lease, core, {3});

            quorum_cert_bt duplicate_candidate(
                new InconsistentAccumulatorCertificate(
                    core.get_config(), proposal));
            CHECK_FALSE(contexts.claim_unforwarded_certificate(
                *lease, std::move(duplicate_candidate)).has_value());
            const auto snapshot = contexts.snapshot(proposal);
            REQUIRE(snapshot.has_value());
            CHECK(snapshot->verified_signers ==
                  std::set<ReplicaID>{3});
            CHECK(snapshot->forwarded_signers.empty());
        }
    }
}

template<typename Lifecycle>
void check_same_hash_forwarding_claim_isolation()
{
    if constexpr (!has_atomic_forwarding_claim<Lifecycle>::value)
        return;
    else
    {
        Lifecycle contexts;
        BlsTestCore core(7, 1);
        const auto shared_hash = digest(
            "forwarding-claim-shared-hash-block");
        const ProposalKey key_a{
            configuration(61, 1, "forwarding-claim-config-a"),
            shared_hash};
        const ProposalKey key_b{
            configuration(61, 1, "forwarding-claim-config-b"),
            shared_hash};
        auto lease_a = contexts.admit_remote(
            metadata(key_a, non_root_tree()));
        auto lease_b = contexts.admit_remote(
            metadata(key_b, non_root_tree()));
        REQUIRE(lease_a.has_value());
        REQUIRE(lease_b.has_value());
        initialize_verified_direct_signers(
            contexts, *lease_a, core, {3});
        initialize_verified_direct_signers(
            contexts, *lease_b, core, {3});

        auto candidate_b = verified_bls_aggregate(
            core, key_b, {3});
        CHECK_FALSE(contexts.claim_unforwarded_certificate(
            *lease_a, std::move(candidate_b)).has_value());
        auto snapshot_a = contexts.snapshot(key_a);
        auto snapshot_b = contexts.snapshot(key_b);
        REQUIRE(snapshot_a.has_value());
        REQUIRE(snapshot_b.has_value());
        CHECK(snapshot_a->forwarded_signers.empty());
        CHECK(snapshot_b->forwarded_signers.empty());

        auto candidate_a = verified_bls_aggregate(
            core, key_a, {3});
        auto claim_a = contexts.claim_unforwarded_certificate(
            *lease_a, std::move(candidate_a));
        REQUIRE(claim_a.has_value());
        CHECK(claim_a->signers == std::set<ReplicaID>{3});
        snapshot_a = contexts.snapshot(key_a);
        snapshot_b = contexts.snapshot(key_b);
        REQUIRE(snapshot_a.has_value());
        REQUIRE(snapshot_b.has_value());
        CHECK(snapshot_a->forwarded_signers ==
              std::set<ReplicaID>{3});
        CHECK(snapshot_b->forwarded_signers.empty());
    }
}

template<typename Lifecycle>
void check_zero_partial_and_stale_forwarding_claims()
{
    if constexpr (!has_atomic_forwarding_claim<Lifecycle>::value)
        return;
    else
    {
        SECTION("zero signer candidates reject and a partial accumulator claims")
        {
            Lifecycle contexts;
            BlsTestCore core(7, 1);
            const auto proposal = key(
                configuration(62, 1, "forwarding-claim-partial"),
                "forwarding-claim-partial-block");
            auto lease = contexts.admit_remote(
                metadata(proposal, non_root_tree()));
            REQUIRE(lease.has_value());
            REQUIRE(contexts.initialize_accumulator(
                *lease, empty_bls_accumulator(core, proposal)));

            auto empty = contexts.clone_accumulator(*lease);
            REQUIRE(empty != nullptr);
            REQUIRE(empty->get_sigs_n() == 0);
            CHECK_FALSE(contexts.claim_unforwarded_certificate(
                *lease, std::move(empty)).has_value());
            auto snapshot = contexts.snapshot(proposal);
            REQUIRE(snapshot.has_value());
            CHECK(snapshot->verified_signers.empty());
            CHECK(snapshot->forwarded_signers.empty());

            auto direct = core.make_part(3, proposal);
            REQUIRE(contexts.record_verified_direct_part(
                *lease, core.get_config(), 3, 3, *direct));
            CHECK_FALSE(contexts.assigned_subtree_complete(*lease));
            auto partial = contexts.clone_accumulator(*lease);
            REQUIRE(partial != nullptr);
            REQUIRE(signer_set(*partial) ==
                    std::set<ReplicaID>{3});
            partial->compute();
            REQUIRE(partial->verify(core.get_config()));
            auto claim = contexts.claim_unforwarded_certificate(
                *lease, std::move(partial));
            REQUIRE(claim.has_value());
            CHECK(claim->signers == std::set<ReplicaID>{3});
            CHECK(claim->certificate->verify(core.get_config()));
            snapshot = contexts.snapshot(proposal);
            REQUIRE(snapshot.has_value());
            CHECK(snapshot->verified_signers ==
                  std::set<ReplicaID>{3});
            CHECK(snapshot->forwarded_signers ==
                  std::set<ReplicaID>{3});
        }

        SECTION("a terminal context rejects its stale lease")
        {
            Lifecycle contexts;
            BlsTestCore core(7, 1);
            const auto proposal = key(
                configuration(62, 2, "forwarding-claim-terminal"),
                "forwarding-claim-terminal-block");
            auto lease = contexts.admit_remote(
                metadata(proposal, non_root_tree()));
            REQUIRE(lease.has_value());
            initialize_verified_direct_signers(
                contexts, *lease, core, {3});
            auto candidate = verified_bls_aggregate(
                core, proposal, {3});

            REQUIRE(contexts.close(
                proposal, ProposalContextEvent::proposal_aborted));
            CHECK_FALSE(contexts.revalidate(*lease));
            CHECK(contexts.context_status(proposal) ==
                  ProposalContextStatus::terminal_closed);
            CHECK_FALSE(contexts.claim_unforwarded_certificate(
                *lease, std::move(candidate)).has_value());
            CHECK_FALSE(contexts.snapshot(proposal).has_value());
        }

        SECTION("configuration retirement purges claimable runtime")
        {
            Lifecycle contexts;
            BlsTestCore core(7, 1);
            const auto config = configuration(
                62, 3, "forwarding-claim-retirement");
            const auto proposal = key(
                config, "forwarding-claim-retirement-block");
            auto lease = contexts.admit_remote(
                metadata(proposal, non_root_tree()));
            REQUIRE(lease.has_value());
            initialize_verified_direct_signers(
                contexts, *lease, core, {3});
            auto candidate = verified_bls_aggregate(
                core, proposal, {3});

            REQUIRE(contexts.retire_configuration(config) == 1);
            CHECK_FALSE(contexts.revalidate(*lease));
            CHECK(contexts.context_status(proposal) ==
                  ProposalContextStatus::retired);
            CHECK_FALSE(contexts.claim_unforwarded_certificate(
                *lease, std::move(candidate)).has_value());
            CHECK_FALSE(contexts.snapshot(proposal).has_value());
            CHECK(contexts.storage_stats().retained_runtime_states == 0);
            CHECK(contexts.storage_stats().retained_accumulators == 0);
        }
    }
}

struct ContextAccumulatorState
{
    ProposalContextSnapshot runtime;
    std::set<ReplicaID> signers;
    std::string serialized;
};

template<typename Lifecycle>
ContextAccumulatorState capture_accumulator_state(
    const Lifecycle &contexts,
    const ProposalContextLease &lease,
    const ReplicaConfig &config)
{
    const auto runtime = contexts.snapshot(lease.key());
    REQUIRE(runtime.has_value());
    auto accumulator = contexts.clone_accumulator(lease);
    REQUIRE(accumulator != nullptr);
    const auto signers = signer_set(*accumulator);
    CHECK(signers.size() == accumulator->get_sigs_n());
    if (!signers.empty())
    {
        accumulator->compute();
        REQUIRE(accumulator->verify(config));
    }
    DataStream stream;
    stream << *accumulator;
    return ContextAccumulatorState{
        *runtime, signers, stream.get_hex()};
}

void check_same_state(const ContextAccumulatorState &actual,
                      const ContextAccumulatorState &expected)
{
    CHECK(actual.runtime.pending_children ==
          expected.runtime.pending_children);
    CHECK(actual.runtime.latency_started ==
          expected.runtime.latency_started);
    CHECK(actual.runtime.verified_signers ==
          expected.runtime.verified_signers);
    CHECK(actual.runtime.forwarded_signers ==
          expected.runtime.forwarded_signers);
    CHECK(actual.runtime.pass_through == expected.runtime.pass_through);
    CHECK(actual.runtime.timer_generation ==
          expected.runtime.timer_generation);
    CHECK(actual.signers == expected.signers);
    CHECK(actual.serialized == expected.serialized);
}

template<typename Lifecycle>
void check_accumulator_initialization_contract()
{
    if constexpr (!has_accumulator_runtime_api<Lifecycle>::value)
        return;
    else
    {
        Lifecycle contexts;
        BlsTestCore core(7);
        const auto exact_key = key(
            configuration(50, 1, "accumulator-init"),
            "accumulator-init-block");
        const auto wrong_key = key(
            configuration(50, 1, "accumulator-wrong"),
            "accumulator-init-block");
        auto lease = contexts.admit_local(
            metadata(exact_key, root_tree()));
        REQUIRE(lease.has_value());

        CHECK_FALSE(contexts.initialize_accumulator(
            *lease, quorum_cert_bt()));
        CHECK_FALSE(contexts.initialize_accumulator(
            *lease, empty_bls_accumulator(core, wrong_key)));
        CHECK_FALSE(contexts.initialize_accumulator(
            *lease,
            verified_bls_aggregate(core, exact_key, {3})));
        CHECK(contexts.clone_accumulator(*lease) == nullptr);

        REQUIRE(contexts.initialize_accumulator(
            *lease, empty_bls_accumulator(core, exact_key)));
        auto initial = contexts.clone_accumulator(*lease);
        REQUIRE(initial != nullptr);
        CHECK(initial->get_proposal_key() == exact_key);
        CHECK(initial->get_signers().empty());
        CHECK(initial->get_sigs_n() == 0);

        auto local_part = core.make_part(0, exact_key);
        REQUIRE(contexts.record_local_part(
            *lease, core.get_config(), 0, *local_part));
        const auto owned = capture_accumulator_state(
            contexts, *lease, core.get_config());
        CHECK(owned.signers == std::set<ReplicaID>{0});

        CHECK_FALSE(contexts.initialize_accumulator(
            *lease, verified_bls_aggregate(core, exact_key, {3})));
        REQUIRE(contexts.initialize_accumulator(
            *lease, empty_bls_accumulator(core, exact_key)));
        check_same_state(
            capture_accumulator_state(
                contexts, *lease, core.get_config()),
            owned);
    }
}

template<typename Lifecycle>
void check_same_hash_accumulator_isolation()
{
    if constexpr (!has_accumulator_runtime_api<Lifecycle>::value)
        return;
    else
    {
        Lifecycle contexts;
        BlsTestCore core(7);
        const auto shared_hash = digest("accumulator-shared-block");
        const ProposalKey first_key{
            configuration(51, 1, "accumulator-config-a"), shared_hash};
        const ProposalKey second_key{
            configuration(51, 1, "accumulator-config-b"), shared_hash};
        auto first = contexts.admit_local(
            metadata(first_key, root_tree()));
        auto second = contexts.admit_local(
            metadata(second_key, root_tree()));
        REQUIRE(first.has_value());
        REQUIRE(second.has_value());
        REQUIRE(contexts.initialize_accumulator(
            *first, empty_bls_accumulator(core, first_key)));
        REQUIRE(contexts.initialize_accumulator(
            *second, empty_bls_accumulator(core, second_key)));

        auto local = core.make_part(0, first_key);
        auto direct = core.make_part(1, first_key);
        auto relay = verified_bls_aggregate(
            core, first_key, {2, 5});
        REQUIRE(contexts.record_local_part(
            *first, core.get_config(), 0, *local));
        REQUIRE(contexts.record_verified_direct_part(
            *first, core.get_config(), 1, 1, *direct));
        REQUIRE(contexts.record_verified_aggregate_certificate(
            *first, 2, *relay));

        const auto first_state = capture_accumulator_state(
            contexts, *first, core.get_config());
        const auto second_empty = capture_accumulator_state(
            contexts, *second, core.get_config());
        CHECK(first_state.signers ==
              std::set<ReplicaID>{0, 1, 2, 5});
        CHECK(first_state.runtime.verified_signers ==
              first_state.signers);
        CHECK(second_empty.signers.empty());
        CHECK(second_empty.runtime.verified_signers.empty());

        auto second_local = core.make_part(0, second_key);
        REQUIRE(contexts.record_local_part(
            *second, core.get_config(), 0, *second_local));
        check_same_state(
            capture_accumulator_state(
                contexts, *first, core.get_config()),
            first_state);
        CHECK(capture_accumulator_state(
                  contexts, *second, core.get_config()).signers ==
              std::set<ReplicaID>{0});
    }
}

template<typename Lifecycle>
void check_atomic_accumulator_mutation()
{
    if constexpr (!has_accumulator_runtime_api<Lifecycle>::value)
        return;
    else
    {
        Lifecycle contexts;
        BlsTestCore core(7);
        const auto exact_key = key(
            configuration(52, 1, "accumulator-atomic"),
            "accumulator-atomic-block");
        const auto wrong_key = key(
            configuration(52, 1, "accumulator-atomic-wrong"),
            "accumulator-atomic-block");
        auto lease = contexts.admit_local(
            metadata(exact_key, root_tree()));
        REQUIRE(lease.has_value());
        REQUIRE(contexts.initialize_accumulator(
            *lease, empty_bls_accumulator(core, exact_key)));

        const auto empty = capture_accumulator_state(
            contexts, *lease, core.get_config());
        auto wrong_local = core.make_part(0, wrong_key);
        CHECK_FALSE(contexts.record_local_part(
            *lease, core.get_config(), 0, *wrong_local));
        check_same_state(
            capture_accumulator_state(
                contexts, *lease, core.get_config()),
            empty);

        auto local = core.make_part(0, exact_key);
        REQUIRE(contexts.record_local_part(
            *lease, core.get_config(), 0, *local));
        auto after_local = capture_accumulator_state(
            contexts, *lease, core.get_config());
        CHECK(after_local.signers == std::set<ReplicaID>{0});
        CHECK(after_local.runtime.verified_signers == after_local.signers);
        auto duplicate_local = core.make_part(0, exact_key);
        CHECK_FALSE(contexts.record_local_part(
            *lease, core.get_config(), 0, *duplicate_local));
        check_same_state(
            capture_accumulator_state(
                contexts, *lease, core.get_config()),
            after_local);

        auto direct = core.make_part(1, exact_key);
        REQUIRE(contexts.record_verified_direct_part(
            *lease, core.get_config(), 1, 1, *direct));
        const auto accepted = capture_accumulator_state(
            contexts, *lease, core.get_config());
        CHECK(accepted.signers == std::set<ReplicaID>{0, 1});
        CHECK(accepted.runtime.verified_signers == accepted.signers);
        CHECK(accepted.runtime.pending_children ==
              std::set<ReplicaID>{2});

        const auto unchanged_after = [&](bool rejected) {
            CHECK_FALSE(rejected);
            check_same_state(
                capture_accumulator_state(
                    contexts, *lease, core.get_config()),
                accepted);
        };

        auto wrong_part = core.make_part(3, wrong_key);
        unchanged_after(contexts.record_verified_direct_part(
            *lease, core.get_config(), 3, 3, *wrong_part));

        auto non_child_part = core.make_part(6, exact_key);
        unchanged_after(contexts.record_verified_direct_part(
            *lease, core.get_config(), 6, 6, *non_child_part));

        auto duplicate = core.make_part(1, exact_key);
        unchanged_after(contexts.record_verified_direct_part(
            *lease, core.get_config(), 1, 1, *duplicate));

        auto wrong_subtree = verified_bls_aggregate(
            core, exact_key, {5});
        unchanged_after(
            contexts.record_verified_aggregate_certificate(
                *lease, 1, *wrong_subtree));

        auto overlapping = verified_bls_aggregate(
            core, exact_key, {1, 3});
        unchanged_after(
            contexts.record_verified_aggregate_certificate(
                *lease, 1, *overlapping));

        auto wrong_aggregate = verified_bls_aggregate(
            core, wrong_key, {3});
        unchanged_after(
            contexts.record_verified_aggregate_certificate(
                *lease, 1, *wrong_aggregate));

        InconsistentAccumulatorCertificate malformed(
            core.get_config(), exact_key);
        unchanged_after(
            contexts.record_verified_aggregate_certificate(
                *lease, 1, malformed));

        auto relay = verified_bls_aggregate(
            core, exact_key, {2, 5, 6});
        REQUIRE(contexts.record_verified_aggregate_certificate(
            *lease, 2, *relay));
        const auto complete = capture_accumulator_state(
            contexts, *lease, core.get_config());
        CHECK(complete.signers ==
              std::set<ReplicaID>{0, 1, 2, 5, 6});
        CHECK(complete.runtime.verified_signers == complete.signers);
        CHECK(complete.runtime.pending_children.empty());
    }
}

template<typename Lifecycle>
void check_owned_accumulator_clone()
{
    if constexpr (!has_accumulator_runtime_api<Lifecycle>::value)
        return;
    else
    {
        Lifecycle contexts;
        BlsTestCore core(7);
        const auto proposal = key(
            configuration(53, 1, "accumulator-clone"),
            "accumulator-clone-block");
        auto lease = contexts.admit_local(
            metadata(proposal, root_tree()));
        REQUIRE(lease.has_value());
        REQUIRE(contexts.initialize_accumulator(
            *lease, empty_bls_accumulator(core, proposal)));
        auto local = core.make_part(0, proposal);
        REQUIRE(contexts.record_local_part(
            *lease, core.get_config(), 0, *local));
        const auto before = capture_accumulator_state(
            contexts, *lease, core.get_config());

        auto clone = contexts.clone_accumulator(*lease);
        REQUIRE(clone != nullptr);
        auto descendant = core.make_part(3, proposal);
        clone->add_part(core.get_config(), 3, *descendant);
        clone->compute();
        REQUIRE(clone->verify(core.get_config()));
        CHECK(signer_set(*clone) == std::set<ReplicaID>{0, 3});

        check_same_state(
            capture_accumulator_state(
                contexts, *lease, core.get_config()),
            before);
    }
}

template<typename Lifecycle>
void check_non_voting_internal_accumulator()
{
    if constexpr (!has_accumulator_runtime_api<Lifecycle>::value)
        return;
    else
    {
        Lifecycle contexts;
        BlsTestCore core(7, 1);
        const auto proposal = key(
            configuration(54, 1, "accumulator-non-voting"),
            "accumulator-non-voting-block");
        auto lease = contexts.admit_remote(
            metadata(proposal, non_root_tree()));
        REQUIRE(lease.has_value());
        REQUIRE(contexts.initialize_accumulator(
            *lease, empty_bls_accumulator(core, proposal)));

        auto child_aggregate = verified_bls_aggregate(
            core, proposal, {3});
        REQUIRE(contexts.record_verified_aggregate_certificate(
            *lease, 3, *child_aggregate));
        auto child_direct = core.make_part(4, proposal);
        REQUIRE(contexts.record_verified_direct_part(
            *lease, core.get_config(), 4, 4, *child_direct));

        auto descendants = capture_accumulator_state(
            contexts, *lease, core.get_config());
        CHECK(descendants.signers == std::set<ReplicaID>{3, 4});
        CHECK(descendants.runtime.verified_signers == descendants.signers);
        CHECK(descendants.signers.count(1) == 0);
        CHECK_FALSE(contexts.assigned_subtree_complete(*lease));

        auto local = core.make_part(1, proposal);
        REQUIRE(contexts.record_local_part(
            *lease, core.get_config(), 1, *local));
        CHECK(contexts.assigned_subtree_complete(*lease));
        CHECK(capture_accumulator_state(
                  contexts, *lease, core.get_config()).signers ==
              std::set<ReplicaID>{1, 3, 4});
    }
}

template<typename Lifecycle>
void check_terminal_accumulator_release()
{
    if constexpr (!has_accumulator_runtime_api<Lifecycle>::value)
        return;
    else
    {
        SECTION("terminal close")
        {
            Lifecycle contexts;
            BlsTestCore core(7);
            const auto config = configuration(55, 1, "accumulator-close");
            const auto proposal = key(config, "accumulator-close-block");
            auto lease = contexts.admit_local(
                metadata(proposal, root_tree()));
            REQUIRE(lease.has_value());
            REQUIRE(contexts.initialize_accumulator(
                *lease, empty_bls_accumulator(core, proposal)));
            REQUIRE(contexts.record_latency_start(*lease, 2));
            REQUIRE(contexts.close(
                proposal, ProposalContextEvent::committed));
            CHECK(contexts.clone_accumulator(*lease) == nullptr);
            CHECK_FALSE(contexts.take_latency_us(*lease, 2).has_value());
            CHECK(contexts.context_status(proposal) ==
                  ProposalContextStatus::terminal_closed);
            const auto stats = contexts.storage_stats();
            CHECK(stats.terminal_tombstones == 1);
            if constexpr (has_accumulator_storage_stats<Lifecycle>::value)
            {
                CHECK(stats.retained_accumulators == 0);
                CHECK(stats.retained_latency_entries == 0);
            }
        }

        SECTION("shutdown")
        {
            Lifecycle contexts;
            BlsTestCore core(7);
            const auto proposal = key(
                configuration(56, 1, "accumulator-shutdown"),
                "accumulator-shutdown-block");
            auto lease = contexts.admit_local(
                metadata(proposal, root_tree()));
            REQUIRE(lease.has_value());
            REQUIRE(contexts.initialize_accumulator(
                *lease, empty_bls_accumulator(core, proposal)));
            REQUIRE(contexts.record_latency_start(*lease, 2));
            contexts.shutdown();
            CHECK(contexts.clone_accumulator(*lease) == nullptr);
            CHECK_FALSE(contexts.take_latency_us(*lease, 2).has_value());
            CHECK(contexts.context_status(proposal) ==
                  ProposalContextStatus::terminal_closed);
            const auto stats = contexts.storage_stats();
            CHECK(stats.terminal_tombstones == 1);
            if constexpr (has_accumulator_storage_stats<Lifecycle>::value)
            {
                CHECK(stats.retained_accumulators == 0);
                CHECK(stats.retained_latency_entries == 0);
            }
        }

        SECTION("configuration retirement")
        {
            Lifecycle contexts;
            BlsTestCore core(7);
            const auto config = configuration(
                57, 1, "accumulator-retirement");
            const auto proposal = key(
                config, "accumulator-retirement-block");
            auto lease = contexts.admit_local(
                metadata(proposal, root_tree()));
            REQUIRE(lease.has_value());
            REQUIRE(contexts.initialize_accumulator(
                *lease, empty_bls_accumulator(core, proposal)));
            REQUIRE(contexts.record_latency_start(*lease, 2));
            REQUIRE(contexts.retire_configuration(config) == 1);
            CHECK(contexts.clone_accumulator(*lease) == nullptr);
            CHECK_FALSE(contexts.take_latency_us(*lease, 2).has_value());
            CHECK(contexts.context_status(proposal) ==
                  ProposalContextStatus::retired);
            const auto stats = contexts.storage_stats();
            CHECK(stats.terminal_tombstones == 0);
            CHECK(stats.retired_configuration_tombstones == 1);
            if constexpr (has_accumulator_storage_stats<Lifecycle>::value)
            {
                CHECK(stats.retained_accumulators == 0);
                CHECK(stats.retained_latency_entries == 0);
            }
        }
    }
}

template<typename Lifecycle>
void check_accumulator_storage_observability()
{
    if constexpr (!has_accumulator_runtime_api<Lifecycle>::value)
        return;
    else if constexpr (!has_accumulator_storage_stats<Lifecycle>::value)
    {
        FAIL("storage_stats must expose retained_accumulators and "
             "retained_latency_entries so terminal cleanup is observable");
    }
    else
    {
        Lifecycle contexts;
        BlsTestCore core(7);
        const auto proposal = key(
            configuration(58, 1, "accumulator-storage"),
            "accumulator-storage-block");
        auto lease = contexts.admit_local(
            metadata(proposal, root_tree()));
        REQUIRE(lease.has_value());
        REQUIRE(contexts.initialize_accumulator(
            *lease, empty_bls_accumulator(core, proposal)));
        REQUIRE(contexts.record_latency_start(*lease, 2));
        const auto open = contexts.storage_stats();
        CHECK(open.retained_accumulators == 1);
        CHECK(open.retained_latency_entries == 1);
    }
}

} // namespace

TEST_CASE("proposal contexts expose all exact lifecycle states",
          "[rem-a06-02][proposal-context][status][admission]")
{
    ProposalContextLifecycle contexts;
    const auto config_a = configuration(4, 7, "epoch-a");
    const auto remote_key = key(config_a, "remote-block");
    auto remote_metadata = metadata(remote_key, non_root_tree());

    CHECK(contexts.context_status(remote_key) ==
          ProposalContextStatus::unknown);
    CHECK_FALSE(contexts.acquire_open_context(remote_key).has_value());

    REQUIRE(contexts.buffer_future(remote_metadata));
    CHECK(contexts.context_status(remote_key) ==
          ProposalContextStatus::buffered_future);
    CHECK_FALSE(contexts.acquire_open_context(remote_key).has_value());
    CHECK_FALSE(contexts.buffer_future(remote_metadata));

    auto remote_lease = contexts.admit_remote(remote_metadata);
    REQUIRE(remote_lease.has_value());
    CHECK(contexts.context_status(remote_key) ==
          ProposalContextStatus::admitted_open);
    CHECK(remote_lease->origin() == ProposalContextOrigin::remote);
    CHECK(remote_lease->key() == remote_key);
    CHECK(contexts.revalidate(*remote_lease));

    const auto local_key = key(config_a, "leader-local-block");
    auto local_lease = contexts.admit_local(metadata(local_key, root_tree()));
    REQUIRE(local_lease.has_value());
    CHECK(local_lease->origin() == ProposalContextOrigin::leader_local);
    CHECK(contexts.context_status(local_key) ==
          ProposalContextStatus::admitted_open);

    REQUIRE(contexts.close(
        remote_key, ProposalContextEvent::proposal_aborted));
    CHECK(contexts.context_status(remote_key) ==
          ProposalContextStatus::terminal_closed);
    CHECK_FALSE(contexts.acquire_open_context(remote_key).has_value());

    REQUIRE(contexts.retire_configuration(config_a) == 2);
    CHECK(contexts.context_status(remote_key) ==
          ProposalContextStatus::retired);
    CHECK(contexts.context_status(local_key) ==
          ProposalContextStatus::retired);
}

TEST_CASE("admission freezes the exact tree and leases carry a generation",
          "[rem-a06-02][proposal-context][tree][lease]")
{
    ProposalContextLifecycle contexts;
    const auto config = configuration(5, 3, "immutable-epoch");
    const auto proposal = key(config, "immutable-block");
    auto candidate = metadata(proposal, non_root_tree());

    auto lease = contexts.admit_remote(candidate);
    REQUIRE(lease.has_value());
    const auto generation = lease->generation();
    REQUIRE(generation != 0);

    // The caller's staging objects remain mutable.  The admitted context must
    // retain its own immutable value snapshot.
    candidate.tree.local_replica = 99;
    candidate.tree.root = 99;
    candidate.tree.parent = std::nullopt;
    candidate.tree.direct_children.clear();
    candidate.tree.assigned_subtree = {99};
    candidate.tree.child_subtrees.clear();

    auto acquired = contexts.acquire_open_context(proposal);
    REQUIRE(acquired.has_value());
    CHECK(acquired->generation() == generation);
    CHECK(acquired->tree().local_replica == 1);
    CHECK(acquired->tree().root == 0);
    CHECK(acquired->tree().parent == std::optional<ReplicaID>{0});
    CHECK((acquired->tree().direct_children ==
           std::vector<ReplicaID>{3, 4}));
    CHECK((acquired->tree().assigned_subtree ==
           std::vector<ReplicaID>{1, 3, 4}));
    CHECK((acquired->tree().child_subtrees ==
           ChildSubtrees{{3, {3}}, {4, {4}}}));

    REQUIRE(contexts.close(
        proposal, ProposalContextEvent::proposal_aborted));
    CHECK_FALSE(contexts.revalidate(*lease));
    CHECK_FALSE(contexts.revalidate(*acquired));
}

TEST_CASE("cyclic or role-invalid tree snapshots never insert contexts",
          "[rem-a06-02][proposal-context][tree][cycle][regression]")
{
    SECTION("the local replica cannot be its own parent")
    {
        check_metadata_rejected_without_insertion(
            tree(1, 0, 1, {3, 4}, {1, 3, 4},
                 {{3, {3}}, {4, {4}}}),
            "parent-is-local");
    }

    SECTION("an upstream parent cannot appear in a descendant subtree")
    {
        check_metadata_rejected_without_insertion(
            tree(1, 0, 2, {3, 4}, {1, 2, 3, 4},
                 {{3, {2, 3}}, {4, {4}}}),
            "parent-is-descendant");
    }

    SECTION("the root cannot appear in a descendant subtree")
    {
        check_metadata_rejected_without_insertion(
            tree(1, 0, 2, {3, 4}, {0, 1, 3, 4},
                 {{3, {0, 3}}, {4, {4}}}),
            "root-is-descendant");
    }

    SECTION("an upstream parent cannot also be a direct child")
    {
        check_metadata_rejected_without_insertion(
            tree(1, 0, 2, {2, 4}, {1, 2, 4},
                 {{2, {2}}, {4, {4}}}),
            "parent-is-direct-child");
    }

    SECTION("valid root non-root and leaf roles remain admissible")
    {
        ProposalContextLifecycle contexts;
        const auto config = configuration(14, 3, "valid-role-controls");
        CHECK(contexts.admit_local(metadata(
                  key(config, "valid-root"), root_tree())).has_value());
        CHECK(contexts.admit_remote(metadata(
                  key(config, "valid-non-root"),
                  non_root_tree())).has_value());
        CHECK(contexts.admit_remote(metadata(
                  key(config, "valid-leaf"), leaf_tree())).has_value());
        CHECK(contexts.proposal_entry_count(config) == 3);
    }
}

TEST_CASE("retirement purges proposals but preserves a configuration tombstone",
          "[rem-a06-02][proposal-context][tombstone][retirement]")
{
    ProposalContextLifecycle contexts;
    const auto config_a = configuration(6, 1, "retire-a");
    const auto config_b = configuration(7, 1, "retire-b");
    const auto key_a1 = key(config_a, "a-one");
    const auto key_a2 = key(config_a, "a-two");
    const auto key_b = key(config_b, "b-one");

    auto lease_a1 = contexts.admit_remote(metadata(key_a1));
    auto lease_a2 = contexts.admit_local(metadata(key_a2));
    auto lease_b = contexts.admit_remote(metadata(key_b));
    REQUIRE(lease_a1.has_value());
    REQUIRE(lease_a2.has_value());
    REQUIRE(lease_b.has_value());

    REQUIRE(contexts.close(
        key_a1, ProposalContextEvent::committed));
    CHECK_FALSE(contexts.close(
        key_a1, ProposalContextEvent::committed));
    CHECK_FALSE(contexts.admit_remote(metadata(key_a1)).has_value());
    CHECK_FALSE(contexts.admit_local(metadata(key_a1)).has_value());
    CHECK(contexts.context_status(key_a1) ==
          ProposalContextStatus::terminal_closed);
    CHECK(contexts.proposal_entry_count(config_a) == 2);
    CHECK_FALSE(contexts.is_configuration_retired(config_a));

    CHECK(contexts.retire_configuration(config_a) == 2);
    CHECK(contexts.retire_configuration(config_a) == 0);
    CHECK(contexts.proposal_entry_count(config_a) == 0);
    CHECK(contexts.is_configuration_retired(config_a));
    CHECK_FALSE(contexts.snapshot(key_a1).has_value());
    CHECK_FALSE(contexts.snapshot(key_a2).has_value());
    CHECK(contexts.context_status(key_a1) ==
          ProposalContextStatus::retired);
    CHECK(contexts.context_status(key_a2) ==
          ProposalContextStatus::retired);
    CHECK(contexts.context_status(key_b) ==
          ProposalContextStatus::admitted_open);
    CHECK_FALSE(contexts.revalidate(*lease_a1));
    CHECK_FALSE(contexts.revalidate(*lease_a2));
    CHECK(contexts.revalidate(*lease_b));

    // Retired status is derived from a compact configuration tombstone, not a
    // retained proposal entry. It prevents ABA reopening of both an old exact
    // key and a previously unseen block in the same retired configuration.
    CHECK_FALSE(contexts.admit_remote(metadata(key_a1)).has_value());
    const auto unseen_key = key(config_a, "unseen-after-retirement");
    CHECK(contexts.context_status(unseen_key) ==
          ProposalContextStatus::retired);
    CHECK_FALSE(contexts.buffer_future(metadata(unseen_key)));
    CHECK_FALSE(contexts.admit_local(metadata(unseen_key)).has_value());
}

TEST_CASE("retirement floor bounds exact tombstones without allowing old ABA",
          "[rem-a06-02][proposal-context][retirement][floor][regression]")
{
    check_retirement_floor_contract<ProposalContextLifecycle>();
}

TEST_CASE("retirement readiness preserves an open draining proposal",
          "[rem-d11][proposal-context][retirement][draining][regression]")
{
    check_open_context_floor_guard<ProposalContextLifecycle>();
}

TEST_CASE("terminal close compacts heavy ownership to an exact-key tombstone",
          "[rem-a06-02][proposal-context][terminal][storage][regression]")
{
    check_terminal_storage_contract<ProposalContextLifecycle>();
}

TEST_CASE("activation preserves old open contexts and does not admit new ones",
          "[rem-a06-02][proposal-context][activation][drain]")
{
    ProposalContextLifecycle contexts;
    const auto old_configuration = configuration(8, 2, "old-epoch");
    const auto new_configuration = configuration(9, 2, "new-epoch");
    const auto old_key = key(old_configuration, "old-draining-block");
    const auto new_key = key(new_configuration, "new-active-block");

    auto old_lease = contexts.admit_remote(metadata(old_key));
    REQUIRE(old_lease.has_value());

    contexts.activate_configuration(new_configuration);
    REQUIRE(contexts.active_configuration().has_value());
    CHECK(*contexts.active_configuration() == new_configuration);
    CHECK(contexts.context_status(old_key) ==
          ProposalContextStatus::admitted_open);
    CHECK(contexts.revalidate(*old_lease));
    CHECK(contexts.context_status(new_key) ==
          ProposalContextStatus::unknown);
    CHECK_FALSE(contexts.acquire_open_context(new_key).has_value());

    auto new_lease = contexts.admit_remote(metadata(new_key));
    REQUIRE(new_lease.has_value());
    CHECK(contexts.revalidate(*old_lease));
    CHECK(contexts.revalidate(*new_lease));
}

TEST_CASE("same-hash proposal runtimes are isolated by exact configuration",
          "[rem-a06-02][proposal-context][same-hash][runtime]")
{
    ProposalContextLifecycle contexts;
    const auto shared_hash = digest("shared-block-hash");
    const auto config_a = configuration(10, 4, "same-hash-a");
    const auto config_b = configuration(10, 4, "same-hash-b");
    const ProposalKey key_a{config_a, shared_hash};
    const ProposalKey key_b{config_b, shared_hash};

    auto lease_a = contexts.admit_remote(metadata(key_a, root_tree()));
    auto lease_b = contexts.admit_remote(metadata(
        key_b,
        tree(0, 0, std::nullopt, {5, 6}, {0, 5, 6},
             {{5, {5}}, {6, {6}}}),
        1));
    REQUIRE(lease_a.has_value());
    REQUIRE(lease_b.has_value());

    REQUIRE(contexts.mark_child_responded(*lease_a, 1));
    REQUIRE(contexts.record_latency_start(*lease_a, 2));
    REQUIRE(contexts.record_verified_aggregate(
        *lease_a, 1, std::set<ReplicaID>{1, 3, 4}));
    REQUIRE(contexts.mark_forwarded_signers(
        *lease_a, std::set<ReplicaID>{1, 3, 4}));
    CHECK_FALSE(contexts.record_verified_aggregate(
        *lease_a, 1, std::set<ReplicaID>{1, 3, 4}));
    CHECK_FALSE(contexts.mark_forwarded_signers(
        *lease_a, std::set<ReplicaID>{1, 3, 4}));
    const auto timer_a = contexts.arm_timer(*lease_a, []() {});
    REQUIRE(timer_a != 0);

    auto snapshot_a = contexts.snapshot(key_a);
    auto snapshot_b = contexts.snapshot(key_b);
    REQUIRE(snapshot_a.has_value());
    REQUIRE(snapshot_b.has_value());
    check_set(snapshot_a->pending_children, {2});
    check_set(snapshot_a->latency_started, {2});
    check_set(snapshot_a->verified_signers, {1, 3, 4});
    check_set(snapshot_a->forwarded_signers, {1, 3, 4});
    CHECK(snapshot_a->timer_generation == timer_a);

    check_set(snapshot_b->pending_children, {5, 6});
    CHECK(snapshot_b->latency_started.empty());
    CHECK(snapshot_b->verified_signers.empty());
    CHECK(snapshot_b->forwarded_signers.empty());
    CHECK(snapshot_b->timer_generation == 0);

    REQUIRE(contexts.close(key_a, ProposalContextEvent::committed));
    CHECK(contexts.context_status(key_a) ==
          ProposalContextStatus::terminal_closed);
    CHECK(contexts.context_status(key_b) ==
          ProposalContextStatus::admitted_open);
    CHECK(contexts.revalidate(*lease_b));

    snapshot_b = contexts.snapshot(key_b);
    REQUIRE(snapshot_b.has_value());
    check_set(snapshot_b->pending_children, {5, 6});
    CHECK(snapshot_b->verified_signers.empty());
}

TEST_CASE("certified signers are child-bound and aggregate dedup is atomic",
          "[rem-a06-02][proposal-context][dedup][signers][child-subtree]")
{
    ProposalContextLifecycle contexts;
    const auto config = configuration(12, 8, "dedup-epoch");

    SECTION("a direct vote must name its authenticated direct child")
    {
        const auto proposal = key(config, "direct-dedup");
        auto lease = contexts.admit_remote(metadata(proposal, root_tree()));
        REQUIRE(lease.has_value());

        CHECK_FALSE(contexts.record_verified_direct(*lease, 1, 2));
        CHECK_FALSE(contexts.record_verified_direct(*lease, 6, 6));
        auto snapshot = contexts.snapshot(proposal);
        REQUIRE(snapshot.has_value());
        CHECK(snapshot->verified_signers.empty());

        REQUIRE(contexts.record_verified_direct(*lease, 1, 1));
        REQUIRE(contexts.mark_forwarded_signers(
            *lease, std::set<ReplicaID>{1}));
        CHECK_FALSE(contexts.record_verified_direct(*lease, 1, 1));
        CHECK_FALSE(contexts.mark_forwarded_signers(
            *lease, std::set<ReplicaID>{1}));

        snapshot = contexts.snapshot(proposal);
        REQUIRE(snapshot.has_value());
        check_set(snapshot->verified_signers, {1});
        check_set(snapshot->forwarded_signers, {1});
    }

    SECTION("invalid or overlapping aggregate relays mutate nothing")
    {
        const auto proposal = key(config, "relay-dedup");
        auto lease = contexts.admit_remote(metadata(proposal, root_tree()));
        REQUIRE(lease.has_value());

        REQUIRE(contexts.record_verified_aggregate(
            *lease, 1, std::set<ReplicaID>{1, 3}));
        REQUIRE(contexts.mark_forwarded_signers(
            *lease, std::set<ReplicaID>{1, 3}));

        const auto before = contexts.snapshot(proposal);
        REQUIRE(before.has_value());

        // Replica 5 belongs to child 2's immutable subtree. A relay from child
        // 1 that claims it must be rejected as a whole; replica 4 must not be
        // partially inserted before the invalid signer is noticed.
        CHECK_FALSE(contexts.record_verified_aggregate(
            *lease, 1, std::set<ReplicaID>{4, 5}));
        auto after = contexts.snapshot(proposal);
        REQUIRE(after.has_value());
        CHECK(after->verified_signers == before->verified_signers);

        // Overlap is also an immutable rejection. A cumulative relay cannot
        // smuggle signer 4 in alongside already-accounted signer 3.
        CHECK_FALSE(contexts.record_verified_aggregate(
            *lease, 1, std::set<ReplicaID>{3, 4}));
        after = contexts.snapshot(proposal);
        REQUIRE(after.has_value());
        CHECK(after->verified_signers == before->verified_signers);

        CHECK_FALSE(contexts.record_verified_aggregate(
            *lease, 1, std::set<ReplicaID>{1, 3}));
        CHECK_FALSE(contexts.mark_forwarded_signers(
            *lease, std::set<ReplicaID>{1, 3}));

        after = contexts.snapshot(proposal);
        REQUIRE(after.has_value());
        check_set(after->verified_signers, {1, 3});
        check_set(after->forwarded_signers, {1, 3});
    }
}

TEST_CASE("non-root terminal closure requires the immutable assigned subtree",
          "[rem-a06-02][proposal-context][non-root][assigned-subtree]")
{
    ProposalContextLifecycle contexts;
    const auto config = configuration(13, 2, "fixed-subtree-epoch");
    const auto proposal = key(config, "fixed-subtree-block");
    auto staged_tree = non_root_tree();
    auto candidate = metadata(proposal, staged_tree);
    auto lease = contexts.admit_remote(candidate);
    REQUIRE(lease.has_value());

    // Mutating the staging value cannot shrink the admitted completion set.
    staged_tree.assigned_subtree = {1, 3};
    staged_tree.child_subtrees.erase(4);
    candidate.tree.assigned_subtree = {1, 3};
    candidate.tree.child_subtrees.erase(4);

    REQUIRE(contexts.record_local_signer(*lease));
    REQUIRE(contexts.record_verified_direct(*lease, 3, 3));
    CHECK(contexts.transition(
              *lease,
              ProposalContextEvent::non_root_aggregate_enqueued) ==
          ProposalTransitionResult::retained_open);
    CHECK(contexts.context_status(proposal) ==
          ProposalContextStatus::admitted_open);
    CHECK(contexts.revalidate(*lease));

    // There is intentionally no reputation input: replica 4 remains required
    // because it belongs to the immutable assigned-subtree snapshot.
    REQUIRE(contexts.record_verified_direct(*lease, 4, 4));
    CHECK(contexts.transition(
              *lease,
              ProposalContextEvent::non_root_aggregate_enqueued) ==
          ProposalTransitionResult::terminal_closed);
    CHECK(contexts.context_status(proposal) ==
          ProposalContextStatus::terminal_closed);
}

TEST_CASE("root QC publication requires its frozen global quorum",
          "[rem-a06-02][proposal-context][root][quorum][regression]")
{
    check_frozen_root_quorum_contract<ProposalContextMetadata>();
}

TEST_CASE("root QC eligibility atomically uses the frozen global quorum",
          "[rem-a06-02][proposal-context][root][quorum][candidate]"
          "[intentional-red]")
{
    check_atomic_frozen_root_qc_candidate<ProposalContextLifecycle>();
}

TEST_CASE("proposal contexts expose one atomic root QC progress claim",
          "[l07][proposal-context][root-qc-progress][api]"
          "[intentional-red]")
{
    if constexpr (has_atomic_root_qc_progress_claim<
                      ProposalContextLifecycle>::value)
        SUCCEED("the exact root QC progress claim API is available");
    else
        FAIL("ProposalContextLifecycle must expose bool "
             "claim_root_qc_progress(const ProposalContextLease &) as a "
             "lifecycle-owned marker after caller-side QC verification");
}

TEST_CASE("an open exact root context claims QC progress once",
          "[l07][proposal-context][root-qc-progress][atomic]"
          "[one-shot]")
{
    check_root_qc_progress_claim_once<ProposalContextLifecycle>();
}

TEST_CASE("invalid contexts cannot claim root QC progress",
          "[l07][proposal-context][root-qc-progress][rejection]"
          "[stale][terminal][retired]")
{
    check_root_qc_progress_claim_rejections<
        ProposalContextLifecycle>();
}

TEST_CASE("root QC progress claims isolate same-block configurations",
          "[l07][proposal-context][root-qc-progress][same-hash]"
          "[exact-key]")
{
    check_root_qc_progress_exact_configuration_isolation<
        ProposalContextLifecycle>();
}

TEST_CASE("root QC progress claim ownership is reclaimed with its context",
          "[l07][proposal-context][root-qc-progress][storage]"
          "[bounded][cleanup]")
{
    check_root_qc_progress_claim_cleanup<ProposalContextLifecycle>();
}

TEST_CASE("root admission accepts only the authoritative Kauri quorum",
          "[rem-a06-02][proposal-context][root][quorum][validation]"
          "[regression]")
{
    constexpr std::size_t membership_size = 7;
    constexpr std::size_t authoritative_quorum =
        2 * ((membership_size - 1) / 3) + 1;
    static_assert(authoritative_quorum == 5,
                  "seven-member Kauri quorum must be five");

    SECTION("a nonzero quorum below the threshold is rejected")
    {
        check_root_quorum_rejected_without_insertion(
            authoritative_quorum - 1, "below-authoritative");
    }

    SECTION("an above-threshold quorum within membership is rejected")
    {
        static_assert(authoritative_quorum + 1 <= membership_size,
                      "fixture must remain within membership");
        check_root_quorum_rejected_without_insertion(
            authoritative_quorum + 1, "above-authoritative");
    }

    SECTION("a quorum larger than membership is rejected")
    {
        check_root_quorum_rejected_without_insertion(
            membership_size + 1, "above-membership");
    }

    SECTION("the authoritative quorum remains admissible")
    {
        ProposalContextLifecycle contexts;
        const auto config = configuration(33, 1, "authoritative-quorum");
        const auto proposal = key(config, "authoritative-quorum-block");
        auto lease = contexts.admit_local(metadata(
            proposal, root_tree(), authoritative_quorum));

        REQUIRE(lease.has_value());
        CHECK(contexts.proposal_entry_count(config) == 1);
        CHECK(contexts.context_status(proposal) ==
              ProposalContextStatus::admitted_open);
    }
}

TEST_CASE("root quorum matches Kauri config for divisible membership",
          "[rem-a06-02][proposal-context][root][quorum][validation]"
          "[regression]")
{
    constexpr std::size_t membership_size = 6;
    constexpr std::size_t authoritative_quorum =
        2 * ((membership_size - 1) / 3) + 1;
    constexpr std::size_t floor_n_over_three_quorum =
        2 * (membership_size / 3) + 1;
    static_assert(authoritative_quorum == 3,
                  "six-member Kauri quorum must be three");
    static_assert(floor_n_over_three_quorum == 5,
                  "regression fixture must distinguish the two formulas");

    SECTION("the configured Kauri quorum is admissible")
    {
        ProposalContextLifecycle contexts;
        const auto config = configuration(34, 1, "six-member-quorum");
        const auto proposal = key(config, "six-member-quorum-block");
        const auto candidate = metadata(
            proposal, six_member_root_tree(), authoritative_quorum);

        CHECK(contexts.buffer_future(candidate));
        CHECK(contexts.proposal_entry_count(config) == 1);
        CHECK(contexts.context_status(proposal) ==
              ProposalContextStatus::buffered_future);
        auto lease = contexts.admit_local(candidate);
        REQUIRE(lease.has_value());
        CHECK(contexts.context_status(proposal) ==
              ProposalContextStatus::admitted_open);
    }

    SECTION("floor N over three is not the configured Kauri quorum")
    {
        check_root_quorum_rejected_without_insertion(
            floor_n_over_three_quorum,
            "six-member-floor-n-over-three",
            six_member_root_tree());
    }
}

TEST_CASE("timer replacement cancels releases and generation-gates callbacks",
          "[rem-a06-02][proposal-context][timer][generation][cancellation]")
{
    ProposalContextLifecycle contexts;
    const auto config = configuration(11, 6, "timer-epoch");
    const auto proposal = key(config, "timer-block");
    auto lease = contexts.admit_remote(metadata(proposal));
    REQUIRE(lease.has_value());

    std::size_t old_cancellations = 0;
    auto old_owner = std::make_shared<int>(1);
    std::weak_ptr<int> old_handle = old_owner;
    const auto old_timer = contexts.arm_timer(
        *lease, [old_owner, &old_cancellations]() {
            ++old_cancellations;
        });
    old_owner.reset();
    CHECK_FALSE(old_handle.expired());

    std::size_t current_cancellations = 0;
    auto current_owner = std::make_shared<int>(2);
    std::weak_ptr<int> current_handle = current_owner;
    const auto current_timer = contexts.arm_timer(
        *lease, [current_owner, &current_cancellations]() {
            ++current_cancellations;
        });
    current_owner.reset();
    REQUIRE(old_timer != 0);
    REQUIRE(current_timer != 0);
    REQUIRE(old_timer != current_timer);
    CHECK(old_cancellations == 1);
    CHECK(old_handle.expired());
    CHECK_FALSE(current_handle.expired());

    std::size_t callbacks = 0;
    CHECK_FALSE(contexts.dispatch_timer(
        proposal, old_timer, [&callbacks](const ProposalContextLease &) {
            ++callbacks;
        }));
    CHECK(callbacks == 0);
    CHECK(old_cancellations == 1);

    REQUIRE(contexts.dispatch_timer(
        proposal,
        current_timer,
        [&contexts, &callbacks](const ProposalContextLease &timer_lease) {
            REQUIRE(contexts.revalidate(timer_lease));
            ++callbacks;
        }));
    CHECK(callbacks == 1);
    CHECK(current_cancellations == 0);
    CHECK(current_handle.expired());

    std::size_t closing_cancellations = 0;
    auto closing_owner = std::make_shared<int>(3);
    std::weak_ptr<int> closing_handle = closing_owner;
    const auto closing_timer = contexts.arm_timer(
        *lease, [closing_owner, &closing_cancellations]() {
            ++closing_cancellations;
        });
    closing_owner.reset();
    REQUIRE(contexts.close(
        proposal, ProposalContextEvent::proposal_aborted));
    CHECK(closing_cancellations == 1);
    CHECK(closing_handle.expired());
    CHECK_FALSE(contexts.dispatch_timer(
        proposal,
        closing_timer,
        [&callbacks](const ProposalContextLease &) { ++callbacks; }));
    CHECK(callbacks == 1);
}

TEST_CASE("timer rearm never reports a generation invalidated reentrantly",
          "[rem-a06-02][proposal-context][timer][reentrant][regression]")
{
    SECTION("the prior cancellation hook closes the context")
    {
        ProposalContextLifecycle contexts;
        const auto proposal = key(
            configuration(17, 1, "timer-reentrant-close"),
            "timer-reentrant-close-block");
        auto lease = contexts.admit_remote(metadata(proposal));
        REQUIRE(lease.has_value());

        std::size_t prior_cancellations = 0;
        bool close_result = false;
        const auto prior_generation = contexts.arm_timer(
            *lease, [&]() {
                ++prior_cancellations;
                close_result = contexts.close(
                    proposal, ProposalContextEvent::proposal_aborted);
            });
        REQUIRE(prior_generation != 0);

        std::size_t replacement_cancellations = 0;
        const auto replacement_generation = contexts.arm_timer(
            *lease, [&replacement_cancellations]() {
                ++replacement_cancellations;
            });

        CHECK(prior_cancellations == 1);
        CHECK(close_result);
        CHECK(replacement_cancellations == 1);
        CHECK(replacement_generation == 0);
        CHECK(contexts.context_status(proposal) ==
              ProposalContextStatus::terminal_closed);
        CHECK_FALSE(contexts.dispatch_timer(
            proposal,
            replacement_generation,
            [](const ProposalContextLease &) {}));
    }

    SECTION("the prior cancellation hook installs a newer timer")
    {
        ProposalContextLifecycle contexts;
        const auto proposal = key(
            configuration(18, 1, "timer-reentrant-rearm"),
            "timer-reentrant-rearm-block");
        auto lease = contexts.admit_remote(metadata(proposal));
        REQUIRE(lease.has_value());

        std::size_t prior_cancellations = 0;
        std::size_t outer_cancellations = 0;
        std::size_t reentrant_cancellations = 0;
        std::uint64_t reentrant_generation = 0;
        const auto prior_generation = contexts.arm_timer(
            *lease, [&]() {
                ++prior_cancellations;
                reentrant_generation = contexts.arm_timer(
                    *lease, [&reentrant_cancellations]() {
                        ++reentrant_cancellations;
                    });
            });
        REQUIRE(prior_generation != 0);

        const auto outer_generation = contexts.arm_timer(
            *lease, [&outer_cancellations]() {
                ++outer_cancellations;
            });

        CHECK(prior_cancellations == 1);
        REQUIRE(reentrant_generation != 0);
        CHECK(outer_cancellations == 1);
        CHECK(reentrant_cancellations == 0);
        CHECK(outer_generation == 0);
        const auto current = contexts.snapshot(proposal);
        REQUIRE(current.has_value());
        CHECK(current->timer_generation == reentrant_generation);
        CHECK_FALSE(contexts.dispatch_timer(
            proposal,
            outer_generation,
            [](const ProposalContextLease &) {}));

        std::size_t callbacks = 0;
        CHECK(contexts.dispatch_timer(
            proposal,
            reentrant_generation,
            [&callbacks](const ProposalContextLease &) { ++callbacks; }));
        CHECK(callbacks == 1);
        CHECK(reentrant_cancellations == 0);
    }
}

TEST_CASE("timer dispatch revalidates after releasing its owner",
          "[rem-a06-02][proposal-context][timer][dispatch][reentrant]"
          "[regression]")
{
    ProposalContextLifecycle contexts;
    const auto proposal = key(
        configuration(19, 1, "timer-dispatch-release"),
        "timer-dispatch-release-block");
    auto lease = contexts.admit_remote(metadata(proposal));
    REQUIRE(lease.has_value());

    auto release_state = std::make_shared<ReentrantReleaseState>();
    release_state->contexts = &contexts;
    release_state->key = proposal;
    const auto timer_generation = contexts.arm_timer(
        *lease, CloseContextOnRelease(release_state));
    REQUIRE(timer_generation != 0);
    release_state->armed = true;

    std::size_t callbacks = 0;
    bool callback_saw_valid_lease = false;
    const auto dispatched = contexts.dispatch_timer(
        proposal,
        timer_generation,
        [&](const ProposalContextLease &timer_lease) {
            ++callbacks;
            callback_saw_valid_lease = contexts.revalidate(timer_lease);
        });

    CHECK(release_state->releases == 1);
    CHECK(release_state->close_result);
    CHECK(contexts.context_status(proposal) ==
          ProposalContextStatus::terminal_closed);
    CHECK_FALSE(dispatched);
    CHECK(callbacks == 0);
    CHECK_FALSE(callback_saw_valid_lease);

    // Keep teardown safe even if a broken implementation retains the owner.
    release_state->contexts = nullptr;
}

TEST_CASE("role-valid terminal paths close once",
          "[rem-a06-02][proposal-context][terminal-matrix][roles]")
{
    SECTION("leaf closes only after its local vote is enqueued")
    {
        ProposalContextLifecycle contexts;
        const auto proposal = key(
            configuration(20, 1, "terminal-leaf"), "leaf-block");
        auto lease = contexts.admit_remote(metadata(proposal, leaf_tree()));
        REQUIRE(lease.has_value());
        REQUIRE(contexts.record_local_signer(*lease));

        CHECK(contexts.transition(
                  *lease, ProposalContextEvent::leaf_vote_enqueued) ==
              ProposalTransitionResult::terminal_closed);
        CHECK_FALSE(contexts.revalidate(*lease));
        CHECK(contexts.transition(
                  *lease, ProposalContextEvent::leaf_vote_enqueued) ==
              ProposalTransitionResult::stale_lease);
    }

    SECTION("non-root closes after its immutable subtree is enqueued")
    {
        ProposalContextLifecycle contexts;
        const auto proposal = key(
            configuration(21, 1, "terminal-non-root"), "non-root-block");
        auto lease = contexts.admit_remote(
            metadata(proposal, non_root_tree()));
        REQUIRE(lease.has_value());
        REQUIRE(contexts.record_local_signer(*lease));
        REQUIRE(contexts.record_verified_direct(*lease, 3, 3));
        REQUIRE(contexts.record_verified_direct(*lease, 4, 4));
        std::size_t timer_cancellations = 0;
        REQUIRE(contexts.arm_timer(
                    *lease, [&timer_cancellations]() {
                        ++timer_cancellations;
                    }) != 0);

        CHECK(contexts.transition(
                  *lease,
                  ProposalContextEvent::non_root_aggregate_enqueued) ==
              ProposalTransitionResult::terminal_closed);
        CHECK(timer_cancellations == 1);
        CHECK_FALSE(contexts.revalidate(*lease));
    }

    SECTION("root closes after the final global QC is published")
    {
        ProposalContextLifecycle contexts;
        const auto proposal = key(
            configuration(22, 1, "terminal-root"), "root-block");
        auto lease = contexts.admit_local(metadata(proposal, root_tree()));
        REQUIRE(lease.has_value());
        REQUIRE(contexts.record_local_signer(*lease));
        REQUIRE(contexts.record_verified_aggregate(
            *lease, 1, std::set<ReplicaID>{1, 3, 4}));
        REQUIRE(contexts.record_verified_aggregate(
            *lease, 2, std::set<ReplicaID>{2}));

        CHECK(contexts.transition(
                  *lease, ProposalContextEvent::root_qc_published) ==
              ProposalTransitionResult::terminal_closed);
        CHECK_FALSE(contexts.revalidate(*lease));
    }

    for (const auto event :
         {ProposalContextEvent::proposal_aborted,
          ProposalContextEvent::committed,
          ProposalContextEvent::shutdown})
    {
        INFO("deterministic terminal event " << static_cast<int>(event));
        ProposalContextLifecycle contexts;
        const auto proposal = key(
            configuration(23 + static_cast<std::uint32_t>(event), 1,
                          "deterministic-terminal"),
            "deterministic-terminal-block");
        auto lease = contexts.admit_remote(metadata(proposal, root_tree()));
        REQUIRE(lease.has_value());
        CHECK(contexts.transition(*lease, event) ==
              ProposalTransitionResult::terminal_closed);
        CHECK_FALSE(contexts.revalidate(*lease));
        CHECK(contexts.transition(*lease, event) ==
              ProposalTransitionResult::stale_lease);
    }
}

TEST_CASE("timeout is lifecycle pass-through without specifying A06 send policy",
          "[rem-a06-02][proposal-context][timeout][late-forwarding]")
{
    ProposalContextLifecycle contexts;
    const auto config = configuration(30, 1, "pass-through-epoch");
    const auto proposal = key(config, "pass-through-block");
    auto lease = contexts.admit_remote(metadata(proposal, non_root_tree()));
    REQUIRE(lease.has_value());

    // REM-A06-02 owns only the open/pass-through lifecycle fact. Which
    // certificate is signed or sent at timeout remains explicitly in A06.
    CHECK(contexts.transition(
              *lease, ProposalContextEvent::aggregation_timeout) ==
          ProposalTransitionResult::retained_open);
    REQUIRE(contexts.revalidate(*lease));
    auto snapshot = contexts.snapshot(proposal);
    REQUIRE(snapshot.has_value());
    CHECK(snapshot->pass_through);

    REQUIRE(contexts.record_verified_direct(*lease, 3, 3));
    REQUIRE(contexts.mark_forwarded_signers(
        *lease, std::set<ReplicaID>{3}));
    CHECK(contexts.transition(
              *lease, ProposalContextEvent::late_contribution_forwarded) ==
          ProposalTransitionResult::retained_open);

    contexts.activate_configuration(
        configuration(31, 1, "next-active-epoch"));
    CHECK(contexts.context_status(proposal) ==
          ProposalContextStatus::admitted_open);
    CHECK(contexts.revalidate(*lease));

    CHECK(contexts.transition(*lease, ProposalContextEvent::committed) ==
          ProposalTransitionResult::terminal_closed);
}

TEST_CASE("commit closes only the exact key certified by the final QC",
          "[rem-a06-02][proposal-context][commit][same-hash]")
{
    ProposalContextLifecycle contexts;
    const auto shared_hash = digest("commit-shared-block");
    const ProposalKey final_qc_key{
        configuration(32, 1, "commit-configuration-a"), shared_hash};
    const ProposalKey same_hash_other_configuration{
        configuration(32, 1, "commit-configuration-b"), shared_hash};
    auto final_lease = contexts.admit_remote(
        metadata(final_qc_key, root_tree()));
    auto other_lease = contexts.admit_remote(
        metadata(same_hash_other_configuration, root_tree()));
    REQUIRE(final_lease.has_value());
    REQUIRE(other_lease.has_value());

    // The caller supplies the complete ProposalKey retained by the published
    // final QC. A bare block hash is never enough to select closure state.
    REQUIRE(contexts.close(
        final_qc_key, ProposalContextEvent::committed));
    CHECK(contexts.context_status(final_qc_key) ==
          ProposalContextStatus::terminal_closed);
    CHECK(contexts.context_status(same_hash_other_configuration) ==
          ProposalContextStatus::admitted_open);
    CHECK_FALSE(contexts.revalidate(*final_lease));
    CHECK(contexts.revalidate(*other_lease));
}

TEST_CASE("proposal contexts expose exact accumulator runtime ownership",
          "[rem-a06-02][proposal-context][accumulator][api]"
          "[intentional-red]")
{
    if constexpr (has_accumulator_runtime_api<
                      ProposalContextLifecycle>::value)
        SUCCEED("exact accumulator runtime API is available");
    else
        FAIL("ProposalContextLifecycle must own an exact-key accumulator, "
             "atomic mutation APIs, owned cloning, latency release, and "
             "shutdown cleanup");
}

TEST_CASE("accumulator initialization preserves one exact owner",
          "[rem-a06-02][proposal-context][accumulator][initialization]")
{
    check_accumulator_initialization_contract<ProposalContextLifecycle>();
}

TEST_CASE("same-block configurations isolate cryptographic accumulators",
          "[rem-a06-02][proposal-context][accumulator][same-hash]")
{
    check_same_hash_accumulator_isolation<ProposalContextLifecycle>();
}

TEST_CASE("accepted certificate mutations are atomic with signer state",
          "[rem-a06-02][proposal-context][accumulator][atomic]"
          "[child-subtree]")
{
    check_atomic_accumulator_mutation<ProposalContextLifecycle>();
}

TEST_CASE("cloned accumulators are owned immutable context snapshots",
          "[rem-a06-02][proposal-context][accumulator][clone]")
{
    check_owned_accumulator_clone<ProposalContextLifecycle>();
}

TEST_CASE("non-voting internal nodes aggregate only verified descendants",
          "[rem-a06-02][proposal-context][accumulator][non-voting]"
          "[assigned-subtree]")
{
    check_non_voting_internal_accumulator<ProposalContextLifecycle>();
}

TEST_CASE("proposal contexts expose one atomic forwarding claim",
          "[a06][proposal-context][forwarding-claim][api]"
          "[intentional-red]")
{
    if constexpr (has_atomic_forwarding_claim<
                      ProposalContextLifecycle>::value)
        SUCCEED("the owned exact forwarding-claim API is available");
    else
        FAIL("ProposalContextLifecycle must return optional<"
             "ProposalForwardingClaim> from claim_unforwarded_certificate("
             "lease, owned_candidate), with owned certificate and exact "
             "signer fields");
}

TEST_CASE("an exact verified forwarding candidate is claimed once",
          "[a06][proposal-context][forwarding-claim][ownership]"
          "[atomic]")
{
    check_owned_one_shot_forwarding_claim<ProposalContextLifecycle>();
}

TEST_CASE("WE06-C05 forwarding reservations preserve disjoint late ownership",
          "[we06][c05][proposal-context][reservation][delta-open]")
{
    ProposalContextLifecycle contexts;
    BlsTestCore core(7, 1);
    const auto proposal = key(
        configuration(64, 1, "wait-exempt-forwarding"),
        "wait-exempt-forwarding-block");
    EpochTreeDefinition definition{
        1, 2, 2, {0, 1, 2, 3, 4, 5, 6}, {4}};
    const auto frozen = hotstuff::make_exact_proposal_context_metadata(
        proposal, 1, definition, 5);
    REQUIRE(frozen.has_value());
    CHECK(frozen->tree.required_subtree ==
          std::set<ReplicaID>{1, 3});
    CHECK(frozen->tree.optional_subtree ==
          std::set<ReplicaID>{4});
    auto lease = contexts.admit_remote(*frozen);
    REQUIRE(lease.has_value());
    definition.wait_exempt_leaves.clear();
    CHECK(lease->tree().required_subtree ==
          std::set<ReplicaID>{1, 3});
    CHECK(lease->tree().optional_subtree ==
          std::set<ReplicaID>{4});
    REQUIRE(contexts.initialize_accumulator(
        *lease, empty_bls_accumulator(core, proposal)));

    auto local = core.make_part(1, proposal);
    REQUIRE(contexts.record_local_part(
        *lease, core.get_config(), 1, *local));
    auto required = core.make_part(3, proposal);
    REQUIRE(contexts.record_verified_direct_part(
        *lease, core.get_config(), 3, 3, *required));
    REQUIRE(contexts.required_subtree_complete(*lease));
    CHECK_FALSE(contexts.assigned_subtree_complete(*lease));

    auto initial = contexts.clone_accumulator(*lease);
    REQUIRE(initial != nullptr);
    initial->compute();
    REQUIRE(initial->verify(core.get_config()));
    auto first = contexts.claim_initial_certificate_reservation(
        *lease, std::move(initial));
    REQUIRE(first.has_value());
    CHECK(first->signers == std::set<ReplicaID>{1, 3});
    REQUIRE(contexts.release_forwarding_claim(
        *lease, first->reservation_id));
    auto after_release = contexts.snapshot(proposal);
    REQUIRE(after_release.has_value());
    CHECK(after_release->reserved_signers.empty());
    CHECK(after_release->forwarded_signers.empty());

    auto retried = contexts.claim_initial_forwarding_reservation(*lease);
    REQUIRE(retried.has_value());
    CHECK(retried->signers == std::set<ReplicaID>{1, 3});
    REQUIRE(contexts.commit_forwarding_claim(
        *lease, retried->reservation_id));
    REQUIRE(contexts.transition(
                *lease,
                ProposalContextEvent::non_root_aggregate_enqueued) ==
            ProposalTransitionResult::retained_open);
    REQUIRE(contexts.delta_open_enabled(*lease));

    auto optional = core.make_part(4, proposal);
    auto optional_candidate = verified_bls_aggregate(
        core, proposal, {4});
    REQUIRE(contexts.record_verified_direct_part(
        *lease,
        core.get_config(),
        4,
        4,
        *optional,
        std::move(optional_candidate)));
    const auto pending = contexts.pending_forwarding_candidate_ids(*lease);
    REQUIRE(pending.size() == 1);

    auto overlapping = verified_bls_aggregate(
        core, proposal, {3, 4});
    const auto before_overlap = contexts.snapshot(proposal);
    REQUIRE(before_overlap.has_value());
    CHECK_FALSE(contexts.claim_unforwarded_certificate_reservation(
        *lease, std::move(overlapping)).has_value());
    const auto after_overlap = contexts.snapshot(proposal);
    REQUIRE(after_overlap.has_value());
    CHECK(after_overlap->reserved_signers ==
          before_overlap->reserved_signers);
    CHECK(after_overlap->forwarded_signers ==
          before_overlap->forwarded_signers);

    auto late = contexts.claim_pending_certificate_reservation(
        *lease, pending.front());
    REQUIRE(late.has_value());
    CHECK(late->signers == std::set<ReplicaID>{4});
    REQUIRE(contexts.release_forwarding_claim(
        *lease, late->reservation_id));
    late = contexts.claim_pending_certificate_reservation(
        *lease, pending.front());
    REQUIRE(late.has_value());
    REQUIRE(contexts.commit_forwarding_claim(
        *lease, late->reservation_id));
    CHECK_FALSE(contexts.claim_pending_certificate_reservation(
        *lease, pending.front()).has_value());
    CHECK(contexts.transition(
              *lease,
              ProposalContextEvent::late_contribution_forwarded) ==
          ProposalTransitionResult::terminal_closed);
}

TEST_CASE("invalid forwarding candidates never mark signer state",
          "[a06][proposal-context][forwarding-claim][rejection]"
          "[atomic]")
{
    check_forwarding_claim_rejections_are_atomic<
        ProposalContextLifecycle>();
}

TEST_CASE("forwarding claims isolate same-hash configurations",
          "[a06][proposal-context][forwarding-claim][same-hash]"
          "[exact-key]")
{
    check_same_hash_forwarding_claim_isolation<
        ProposalContextLifecycle>();
}

TEST_CASE("forwarding claims handle partial and stale context state",
          "[a06][proposal-context][forwarding-claim][partial]"
          "[terminal][retirement]")
{
    check_zero_partial_and_stale_forwarding_claims<
        ProposalContextLifecycle>();
}

TEST_CASE("non-voting internal timeout forwards verified descendants only",
          "[a06][proposal-context][aggregation-timeout][non-voting]"
          "[control]")
{
    ProposalContextLifecycle contexts;
    BlsTestCore core(7, 1);
    const auto proposal = key(
        configuration(58, 1, "non-voting-timeout"),
        "non-voting-timeout-block");
    auto lease = contexts.admit_remote(
        metadata(proposal, non_root_tree()));
    REQUIRE(lease.has_value());
    REQUIRE(contexts.initialize_accumulator(
        *lease, empty_bls_accumulator(core, proposal)));

    const auto initial_children = contexts.pending_children(*lease);
    REQUIRE(initial_children.has_value());
    CHECK(*initial_children == std::set<ReplicaID>{3, 4});
    REQUIRE(contexts.record_latency_start(*lease, 3));
    REQUIRE(contexts.record_latency_start(*lease, 4));
    const auto timer_generation = contexts.arm_timer(*lease);
    REQUIRE(timer_generation != 0);

    auto child_aggregate = verified_bls_aggregate(
        core, proposal, {3});
    REQUIRE(contexts.record_verified_aggregate_certificate(
        *lease, 3, *child_aggregate));
    auto child_direct = core.make_part(4, proposal);
    REQUIRE(contexts.record_verified_direct_part(
        *lease, core.get_config(), 4, 4, *child_direct));
    CHECK(contexts.take_latency_us(*lease, 3).has_value());
    CHECK(contexts.take_latency_us(*lease, 4).has_value());
    CHECK(contexts.pending_children(*lease) ==
          std::optional<std::set<ReplicaID>>(std::set<ReplicaID>{}));
    CHECK_FALSE(contexts.assigned_subtree_complete(*lease));

    std::set<ReplicaID> forwarded_payload;
    bool timeout_ran = false;
    REQUIRE(contexts.dispatch_timer(
        proposal,
        timer_generation,
        [&](const ProposalContextLease &active) {
            timeout_ran = true;
            auto partial = contexts.clone_accumulator(active);
            REQUIRE(partial != nullptr);
            REQUIRE(partial->get_sigs_n() > 0);
            partial->compute();
            REQUIRE(partial->verify(core.get_config()));
            const auto enumerated = partial->get_signers();
            forwarded_payload = std::set<ReplicaID>(
                enumerated.begin(), enumerated.end());
            REQUIRE(forwarded_payload.size() == enumerated.size());
            REQUIRE(forwarded_payload.size() == partial->get_sigs_n());
            REQUIRE(contexts.mark_forwarded_signers(
                active, forwarded_payload));
            CHECK(contexts.transition(
                      active,
                      ProposalContextEvent::aggregation_timeout) ==
                  ProposalTransitionResult::retained_open);
        }));

    CHECK(timeout_ran);
    CHECK(forwarded_payload == std::set<ReplicaID>{3, 4});
    CHECK(forwarded_payload.count(1) == 0);
    REQUIRE(contexts.revalidate(*lease));
    const auto snapshot = contexts.snapshot(proposal);
    REQUIRE(snapshot.has_value());
    CHECK(snapshot->pass_through);
    CHECK(snapshot->forwarded_signers == forwarded_payload);
    CHECK(snapshot->verified_signers == forwarded_payload);
    auto retained = contexts.clone_accumulator(*lease);
    REQUIRE(retained != nullptr);
    CHECK(signer_set(*retained) == forwarded_payload);
    CHECK(signer_set(*retained).count(1) == 0);
}

TEST_CASE("terminal paths release accumulator and latency ownership",
          "[rem-a06-02][proposal-context][accumulator][terminal]"
          "[shutdown][retirement]")
{
    check_terminal_accumulator_release<ProposalContextLifecycle>();
}

TEST_CASE("storage stats expose heavy exact-context ownership",
          "[rem-a06-02][proposal-context][accumulator][storage]"
          "[intentional-red]")
{
    check_accumulator_storage_observability<ProposalContextLifecycle>();
}

#endif
