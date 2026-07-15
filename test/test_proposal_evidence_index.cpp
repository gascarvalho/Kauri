#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <iterator>
#include <limits>
#include <memory>
#include <new>
#include <string>
#include <type_traits>
#include <utility>

#include "catch.hpp"
#include "hotstuff/evidence.h"

namespace proposal_evidence_index_allocation_failure
{

constexpr std::size_t disabled = std::numeric_limits<std::size_t>::max();
thread_local std::size_t allocations_before_failure = disabled;

bool consume_failure() noexcept
{
    if (allocations_before_failure == disabled)
        return false;
    if (allocations_before_failure == 0)
    {
        allocations_before_failure = disabled;
        return true;
    }
    --allocations_before_failure;
    return false;
}

void disable() noexcept
{
    allocations_before_failure = disabled;
}

class OneShot final
{
public:
    explicit OneShot(std::size_t successful_allocations) noexcept
    {
        allocations_before_failure = successful_allocations;
    }

    ~OneShot()
    {
        disable();
    }

    OneShot(const OneShot &) = delete;
    OneShot &operator=(const OneShot &) = delete;
};

} // namespace proposal_evidence_index_allocation_failure

void *operator new(std::size_t size)
{
    if (proposal_evidence_index_allocation_failure::consume_failure())
        throw std::bad_alloc();
    if (size == 0)
        size = 1;
    if (auto *const allocation = std::malloc(size))
        return allocation;
    throw std::bad_alloc();
}

void operator delete(void *allocation) noexcept
{
    std::free(allocation);
}

void operator delete(void *allocation, std::size_t) noexcept
{
    std::free(allocation);
}

/*
 * E08 trusted proposal-evidence window contract
 * ------------------------------------------------
 * This index is the manager-owned, bounded mirror of immutable protocol
 * admission lifecycle facts. Activation is deliberately not an index event:
 * a buffered future proposal remains unknown until normal protocol admission
 * opens its exact ProposalKey. Commit and deterministic retirement make an
 * exact key stale without permitting ABA resurrection.
 *
 * The fallback declares the complete public seam while production is absent.
 * Methods remain deliberately undefined so this target object-compiles and
 * then fails to link with a precise missing-production signal.
 */
#if __has_include("hotstuff/proposal_evidence_index.h")
#include "hotstuff/proposal_evidence_index.h"
#define KAURI_HAS_PROPOSAL_EVIDENCE_INDEX_API 1
#else
#define KAURI_HAS_PROPOSAL_EVIDENCE_INDEX_API 0

namespace hotstuff
{

struct ProposalEvidenceIndexLimits
{
    std::size_t maximum_exact_proposals{4096};
    std::size_t maximum_retired_configurations{64};
};

struct ProposalEvidenceIndexStats
{
    std::size_t admissible_proposals{0};
    std::size_t stale_proposals{0};
    std::size_t retired_configurations{0};
    std::uint32_t first_live_epoch{0};
    std::uint64_t capacity_failures{0};
    bool healthy{true};
    bool stopped{false};
};

class ProposalEvidenceIndex final : public ProposalEvidenceWindow
{
public:
    explicit ProposalEvidenceIndex(ProposalEvidenceIndexLimits limits = {});
    ~ProposalEvidenceIndex();

    ProposalEvidenceIndex(const ProposalEvidenceIndex &) = delete;
    ProposalEvidenceIndex &operator=(const ProposalEvidenceIndex &) = delete;
    ProposalEvidenceIndex(ProposalEvidenceIndex &&) = delete;
    ProposalEvidenceIndex &operator=(ProposalEvidenceIndex &&) = delete;

    bool admit(const ProposalKey &proposal) noexcept;
    bool stale_if_admitted(const ProposalKey &proposal) noexcept;
    bool mark_stale(const ProposalKey &proposal) noexcept;
    std::size_t retire_configuration(
        const ConfigurationId &configuration) noexcept;
    std::size_t advance_retirement_floor(
        std::uint32_t first_live_epoch) noexcept;

    ProposalEvidenceStatus classify(
        const ProposalKey &proposal) const noexcept override;

    ProposalEvidenceIndexStats stats() const noexcept;
    bool healthy() const noexcept;
    void shutdown() noexcept;

private:
    struct State;
    std::unique_ptr<State> state_;
};

} // namespace hotstuff
#endif

namespace
{

using hotstuff::ConfigurationId;
using hotstuff::DataStream;
using hotstuff::ProposalEvidenceIndex;
using hotstuff::ProposalEvidenceIndexLimits;
using hotstuff::ProposalEvidenceIndexStats;
using hotstuff::ProposalEvidenceStatus;
using hotstuff::ProposalKey;
using hotstuff::uint256_t;

uint256_t digest(const std::string &label)
{
    return DataStream(label).get_hash();
}

ConfigurationId configuration(
    std::uint32_t epoch,
    std::uint32_t tree,
    const std::string &definition)
{
    return ConfigurationId{epoch, tree, digest(definition)};
}

ProposalKey proposal(
    const ConfigurationId &configuration_id,
    const std::string &block)
{
    return ProposalKey{configuration_id, digest(block)};
}

ProposalEvidenceIndexLimits limits(
    std::size_t exact_proposals = 32,
    std::size_t retired_configurations = 8)
{
    return {exact_proposals, retired_configurations};
}

void check_stats(
    const ProposalEvidenceIndex &index,
    std::size_t admissible,
    std::size_t stale,
    std::size_t retired_configurations,
    std::uint32_t first_live_epoch,
    std::uint64_t capacity_failures,
    bool healthy,
    bool stopped = false)
{
    const auto stats = index.stats();
    CHECK(stats.admissible_proposals == admissible);
    CHECK(stats.stale_proposals == stale);
    CHECK(stats.retired_configurations == retired_configurations);
    CHECK(stats.first_live_epoch == first_live_epoch);
    CHECK(stats.capacity_failures == capacity_failures);
    CHECK(stats.healthy == healthy);
    CHECK(stats.stopped == stopped);
    CHECK(index.healthy() == healthy);
}

template<typename Index, typename = void>
struct has_activation_mutator : std::false_type
{};

template<typename Index>
struct has_activation_mutator<
    Index,
    std::void_t<decltype(std::declval<Index &>().activate_configuration(
        std::declval<const ConfigurationId &>()))>> : std::true_type
{};

std::string read_source(const std::string &relative_path)
{
#ifdef KAURI_PROJECT_SOURCE_DIR
    const std::string root = KAURI_PROJECT_SOURCE_DIR;
#else
    const std::string root = ".";
#endif
    std::ifstream input(root + "/" + relative_path);
    REQUIRE(input.good());
    return std::string(
        std::istreambuf_iterator<char>(input),
        std::istreambuf_iterator<char>());
}

} // namespace

TEST_CASE("E08 proposal evidence index exposes one bounded fail-closed window",
          "[e08][proposal-evidence-index][contract][intentional-red]")
{
    CHECK(KAURI_HAS_PROPOSAL_EVIDENCE_INDEX_API == 1);
    CHECK(ProposalEvidenceIndexLimits{}.maximum_exact_proposals == 4096);
    CHECK(ProposalEvidenceIndexLimits{}.maximum_retired_configurations == 64);

    static_assert(
        std::is_final<ProposalEvidenceIndex>::value,
        "the manager window has one concrete lifecycle owner");
    static_assert(
        std::is_base_of<
            hotstuff::ProposalEvidenceWindow,
            ProposalEvidenceIndex>::value,
        "the index must be injected directly into EvidenceLedger");
    static_assert(
        !std::is_copy_constructible<ProposalEvidenceIndex>::value,
        "copying would fork trusted lifecycle state");
    static_assert(
        !std::is_move_constructible<ProposalEvidenceIndex>::value,
        "moving would invalidate the ledger's borrowed window");
    static_assert(
        !has_activation_mutator<ProposalEvidenceIndex>::value,
        "configuration activation alone must not admit proposals");

    using KeyMutation = bool (ProposalEvidenceIndex::*)(
        const ProposalKey &) noexcept;
    using ConfigurationRetirement = std::size_t (
        ProposalEvidenceIndex::*)(const ConfigurationId &) noexcept;
    using FloorRetirement = std::size_t (
        ProposalEvidenceIndex::*)(std::uint32_t) noexcept;
    using Classification = ProposalEvidenceStatus (
        ProposalEvidenceIndex::*)(const ProposalKey &) const noexcept;
    using Stats = ProposalEvidenceIndexStats (
        ProposalEvidenceIndex::*)() const noexcept;

    static_assert(
        std::is_same<decltype(&ProposalEvidenceIndex::admit), KeyMutation>::value,
        "admission must not throw into protocol code");
    static_assert(
        std::is_same<
            decltype(&ProposalEvidenceIndex::stale_if_admitted),
            KeyMutation>::value,
        "abort cleanup must not throw into protocol code");
    static_assert(
        std::is_same<
            decltype(&ProposalEvidenceIndex::mark_stale),
            KeyMutation>::value,
        "commit cleanup must not throw into protocol code");
    static_assert(
        std::is_same<
            decltype(&ProposalEvidenceIndex::retire_configuration),
            ConfigurationRetirement>::value,
        "exact configuration retirement must be nonthrowing");
    static_assert(
        std::is_same<
            decltype(&ProposalEvidenceIndex::advance_retirement_floor),
            FloorRetirement>::value,
        "the monotonic floor must be nonthrowing");
    static_assert(
        std::is_same<
            decltype(&ProposalEvidenceIndex::classify),
            Classification>::value,
        "ledger classification must remain noexcept");
    static_assert(
        std::is_same<decltype(&ProposalEvidenceIndex::stats), Stats>::value,
        "bounded-state diagnostics must be an immutable snapshot");
}

TEST_CASE("only exact admitted proposal keys become admissible",
          "[e08][proposal-evidence-index][admission][exact]"
          "[intentional-red]")
{
    const auto config = configuration(4, 7, "epoch-four");
    const auto key = proposal(config, "block-a");
    ProposalEvidenceIndex index(limits());

    CHECK(index.classify(key) == ProposalEvidenceStatus::unknown);
    check_stats(index, 0, 0, 0, 0, 0, true);

    CHECK(index.admit(key));
    CHECK(index.classify(key) == ProposalEvidenceStatus::admissible);
    check_stats(index, 1, 0, 0, 0, 0, true);

    INFO("duplicate protocol admission is idempotent");
    CHECK(index.admit(key));
    CHECK(index.classify(key) == ProposalEvidenceStatus::admissible);
    check_stats(index, 1, 0, 0, 0, 0, true);

    INFO("the same block under another exact tree or digest never aliases");
    const ProposalKey other_tree{
        configuration(4, 11, "epoch-four"), key.block_hash};
    const ProposalKey other_digest{
        configuration(4, 7, "divergent-epoch-four"), key.block_hash};
    CHECK(index.classify(other_tree) == ProposalEvidenceStatus::unknown);
    CHECK(index.classify(other_digest) == ProposalEvidenceStatus::unknown);
    CHECK(index.admit(other_tree));
    CHECK(index.classify(other_tree) == ProposalEvidenceStatus::admissible);
    CHECK(index.classify(other_digest) == ProposalEvidenceStatus::unknown);
    check_stats(index, 2, 0, 0, 0, 0, true);
}

TEST_CASE("future configuration activation is not proposal admission",
          "[e08][proposal-evidence-index][future][activation]"
          "[intentional-red]")
{
    ProposalEvidenceIndex index(limits());
    const auto active = proposal(
        configuration(8, 7, "active-eight"), "active-block");
    const auto buffered_future = proposal(
        configuration(9, 7, "future-nine"), "future-block");

    REQUIRE(index.admit(active));
    CHECK(index.classify(active) == ProposalEvidenceStatus::admissible);
    CHECK(index.classify(buffered_future) == ProposalEvidenceStatus::unknown);
    CHECK_FALSE(has_activation_mutator<ProposalEvidenceIndex>::value);

    INFO("only a later normal-protocol admission opens the future exact key");
    REQUIRE(index.admit(buffered_future));
    CHECK(index.classify(buffered_future) ==
          ProposalEvidenceStatus::admissible);
}

TEST_CASE("stale transitions are monotonic and abort cleanup is conditional",
          "[e08][proposal-evidence-index][stale][aba][intentional-red]")
{
    ProposalEvidenceIndex index(limits());
    const auto config = configuration(12, 3, "epoch-twelve");
    const auto admitted = proposal(config, "admitted");
    const auto never_admitted = proposal(config, "never-admitted");
    const auto committed_before_feed = proposal(config, "commit-first");

    INFO("an abort before admission does not manufacture a stale tombstone");
    CHECK_FALSE(index.stale_if_admitted(never_admitted));
    CHECK(index.classify(never_admitted) == ProposalEvidenceStatus::unknown);
    check_stats(index, 0, 0, 0, 0, 0, true);

    REQUIRE(index.admit(admitted));
    CHECK(index.stale_if_admitted(admitted));
    CHECK(index.classify(admitted) == ProposalEvidenceStatus::stale);
    CHECK_FALSE(index.stale_if_admitted(admitted));
    CHECK_FALSE(index.admit(admitted));
    CHECK(index.classify(admitted) == ProposalEvidenceStatus::stale);

    INFO("commit may arrive before admission and must block later resurrection");
    CHECK(index.mark_stale(committed_before_feed));
    CHECK(index.classify(committed_before_feed) ==
          ProposalEvidenceStatus::stale);
    CHECK_FALSE(index.mark_stale(committed_before_feed));
    CHECK_FALSE(index.admit(committed_before_feed));
    CHECK(index.classify(committed_before_feed) ==
          ProposalEvidenceStatus::stale);
    check_stats(index, 0, 2, 0, 0, 0, true);
}

TEST_CASE("exact configuration retirement stales seen and unseen keys",
          "[e08][proposal-evidence-index][configuration-retirement]"
          "[intentional-red]")
{
    ProposalEvidenceIndex index(limits());
    const auto retired = configuration(20, 7, "epoch-twenty");
    const auto retained_tree = configuration(20, 11, "epoch-twenty");
    const auto retained_digest = configuration(20, 7, "epoch-twenty-fork");
    const auto retired_a = proposal(retired, "retired-a");
    const auto retired_b = proposal(retired, "retired-b");
    const auto unseen = proposal(retired, "retired-unseen");
    const auto other_tree = proposal(retained_tree, "other-tree");
    const auto other_digest = proposal(retained_digest, "other-digest");

    REQUIRE(index.admit(retired_a));
    REQUIRE(index.admit(retired_b));
    REQUIRE(index.admit(other_tree));
    REQUIRE(index.admit(other_digest));

    CHECK(index.retire_configuration(retired) == 2);
    CHECK(index.classify(retired_a) == ProposalEvidenceStatus::stale);
    CHECK(index.classify(retired_b) == ProposalEvidenceStatus::stale);
    CHECK(index.classify(unseen) == ProposalEvidenceStatus::stale);
    CHECK_FALSE(index.admit(unseen));
    CHECK(index.classify(other_tree) ==
          ProposalEvidenceStatus::admissible);
    CHECK(index.classify(other_digest) ==
          ProposalEvidenceStatus::admissible);
    check_stats(index, 2, 0, 1, 0, 0, true);

    INFO("retirement is idempotent and its configuration tombstone is bounded");
    CHECK(index.retire_configuration(retired) == 0);
    check_stats(index, 2, 0, 1, 0, 0, true);
}

TEST_CASE("monotonic retirement floor is exclusive and compacts deterministically",
          "[e08][proposal-evidence-index][retirement-floor][compaction]"
          "[intentional-red]")
{
    ProposalEvidenceIndex index(limits());
    const auto config_40 = configuration(40, 1, "epoch-forty");
    const auto config_41 = configuration(41, 1, "epoch-forty-one");
    const auto config_42 = configuration(42, 1, "epoch-forty-two");
    const auto key_40_open = proposal(config_40, "forty-open");
    const auto key_40_stale = proposal(config_40, "forty-stale");
    const auto key_41 = proposal(config_41, "forty-one");
    const auto key_42 = proposal(config_42, "forty-two");

    REQUIRE(index.admit(key_40_open));
    REQUIRE(index.mark_stale(key_40_stale));
    REQUIRE(index.admit(key_41));
    REQUIRE(index.admit(key_42));
    REQUIRE(index.retire_configuration(config_41) == 1);
    check_stats(index, 2, 1, 1, 0, 0, true);

    INFO("two exact entries and one configuration tombstone are compacted");
    CHECK(index.advance_retirement_floor(42) == 3);
    CHECK(index.classify(key_40_open) == ProposalEvidenceStatus::stale);
    CHECK(index.classify(key_40_stale) == ProposalEvidenceStatus::stale);
    CHECK(index.classify(key_41) == ProposalEvidenceStatus::stale);
    CHECK(index.classify(key_42) == ProposalEvidenceStatus::admissible);
    check_stats(index, 1, 0, 0, 42, 0, true);

    INFO("the first-live epoch is admissible and the floor never moves back");
    CHECK(index.advance_retirement_floor(41) == 0);
    CHECK(index.classify(key_42) == ProposalEvidenceStatus::admissible);
    check_stats(index, 1, 0, 0, 42, 0, true);

    CHECK(index.advance_retirement_floor(43) == 1);
    CHECK(index.classify(key_42) == ProposalEvidenceStatus::stale);
    check_stats(index, 0, 0, 0, 43, 0, true);

    const auto unseen_old = proposal(
        configuration(39, 99, "unseen-old"), "unseen-old-block");
    const auto first_live = proposal(
        configuration(43, 99, "first-live"), "first-live-block");
    CHECK(index.classify(unseen_old) == ProposalEvidenceStatus::stale);
    CHECK(index.classify(first_live) == ProposalEvidenceStatus::unknown);
}

TEST_CASE("exact proposal capacity fails closed without silent eviction",
          "[e08][proposal-evidence-index][capacity][exact]"
          "[intentional-red]")
{
    const auto config = configuration(50, 1, "capacity-exact");
    const auto stale = proposal(config, "stale");
    const auto admissible = proposal(config, "admissible");
    const auto overflow = proposal(config, "overflow");

    SECTION("admission cannot exceed the shared exact-entry bound")
    {
        ProposalEvidenceIndex index(limits(2, 2));
        REQUIRE(index.mark_stale(stale));
        REQUIRE(index.admit(admissible));
        check_stats(index, 1, 1, 0, 0, 0, true);

        CHECK_FALSE(index.admit(overflow));
        CHECK_FALSE(index.healthy());
        CHECK(index.classify(stale) == ProposalEvidenceStatus::stale);
        CHECK(index.classify(admissible) == ProposalEvidenceStatus::unknown);
        CHECK(index.classify(overflow) == ProposalEvidenceStatus::unknown);
        check_stats(index, 1, 1, 0, 0, 1, false);

        INFO("health is sticky and no later admission can revive the subsystem");
        CHECK_FALSE(index.admit(overflow));
        CHECK_FALSE(index.admit(admissible));
        CHECK(index.classify(admissible) == ProposalEvidenceStatus::unknown);
        check_stats(index, 1, 1, 0, 0, 1, false);

        INFO("nonallocating stale/floor knowledge remains fail-closed and precise");
        CHECK(index.stale_if_admitted(admissible));
        CHECK(index.classify(admissible) == ProposalEvidenceStatus::stale);
        CHECK(index.advance_retirement_floor(51) == 2);
        CHECK(index.classify(overflow) == ProposalEvidenceStatus::stale);
        check_stats(index, 0, 0, 0, 51, 1, false);
    }

    SECTION("an unseen stale tombstone uses the same exact-entry bound")
    {
        ProposalEvidenceIndex index(limits(1, 2));
        REQUIRE(index.mark_stale(stale));

        CHECK_FALSE(index.mark_stale(overflow));
        CHECK_FALSE(index.healthy());
        CHECK(index.classify(stale) == ProposalEvidenceStatus::stale);
        CHECK(index.classify(overflow) == ProposalEvidenceStatus::unknown);
        check_stats(index, 0, 1, 0, 0, 1, false);
    }
}

TEST_CASE("retired configuration capacity is fixed and fail closed",
          "[e08][proposal-evidence-index][capacity][configuration]"
          "[intentional-red]")
{
    ProposalEvidenceIndex index(limits(8, 1));
    const auto retired = configuration(60, 1, "retired-capacity-a");
    const auto overflow = configuration(60, 2, "retired-capacity-b");
    const auto retired_unseen = proposal(retired, "retired-unseen");
    const auto overflow_admitted = proposal(overflow, "overflow-admitted");
    const auto overflow_unseen = proposal(overflow, "overflow-unseen");

    CHECK(index.retire_configuration(retired) == 0);
    CHECK(index.classify(retired_unseen) == ProposalEvidenceStatus::stale);
    check_stats(index, 0, 0, 1, 0, 0, true);

    REQUIRE(index.admit(overflow_admitted));
    check_stats(index, 1, 0, 1, 0, 0, true);

    CHECK(index.retire_configuration(overflow) == 0);
    CHECK_FALSE(index.healthy());
    CHECK(index.classify(retired_unseen) == ProposalEvidenceStatus::stale);
    CHECK(index.classify(overflow_admitted) == ProposalEvidenceStatus::unknown);
    CHECK(index.classify(overflow_unseen) == ProposalEvidenceStatus::unknown);
    INFO("the exact entry survives a failed tombstone insertion");
    check_stats(index, 1, 0, 1, 0, 1, false);
}

TEST_CASE("zero bounds construct an unhealthy inert index",
          "[e08][proposal-evidence-index][limits][fail-closed]"
          "[intentional-red]")
{
    const auto key = proposal(
        configuration(70, 1, "invalid-limits"), "invalid-limits-block");

    SECTION("exact proposal bound")
    {
        ProposalEvidenceIndex index(limits(0, 1));
        CHECK_FALSE(index.healthy());
        CHECK_FALSE(index.admit(key));
        CHECK(index.classify(key) == ProposalEvidenceStatus::unknown);
        check_stats(index, 0, 0, 0, 0, 0, false);
    }

    SECTION("retired configuration bound")
    {
        ProposalEvidenceIndex index(limits(1, 0));
        CHECK_FALSE(index.healthy());
        CHECK_FALSE(index.admit(key));
        CHECK(index.classify(key) == ProposalEvidenceStatus::unknown);
        check_stats(index, 0, 0, 0, 0, 0, false);
    }
}

TEST_CASE("allocation failure is atomic sticky and preserves stale knowledge",
          "[e08][proposal-evidence-index][allocation][atomic]"
          "[intentional-red]")
{
    const auto config = configuration(80, 1, "allocation");
    const auto stale = proposal(config, "stale-before-failure");
    const auto admissible = proposal(config, "open-before-failure");
    const auto failed = proposal(config, "allocation-failed");

    SECTION("admission insertion")
    {
        ProposalEvidenceIndex index(limits(8, 4));
        REQUIRE(index.mark_stale(stale));
        REQUIRE(index.admit(admissible));

        bool returned = true;
        {
            proposal_evidence_index_allocation_failure::OneShot fault(0);
            returned = index.admit(failed);
        }

        CHECK_FALSE(returned);
        CHECK_FALSE(index.healthy());
        CHECK(index.classify(stale) == ProposalEvidenceStatus::stale);
        CHECK(index.classify(admissible) == ProposalEvidenceStatus::unknown);
        CHECK(index.classify(failed) == ProposalEvidenceStatus::unknown);
        check_stats(index, 1, 1, 0, 0, 0, false);

        INFO("a failed insertion leaves no partial entry and health never recovers");
        CHECK_FALSE(index.admit(failed));
        CHECK(index.classify(failed) == ProposalEvidenceStatus::unknown);
        CHECK(index.stale_if_admitted(admissible));
        CHECK(index.classify(admissible) == ProposalEvidenceStatus::stale);
        CHECK_FALSE(index.healthy());
    }

    SECTION("unseen stale tombstone insertion")
    {
        ProposalEvidenceIndex index(limits(8, 4));
        REQUIRE(index.admit(admissible));

        bool returned = true;
        {
            proposal_evidence_index_allocation_failure::OneShot fault(0);
            returned = index.mark_stale(failed);
        }

        CHECK_FALSE(returned);
        CHECK_FALSE(index.healthy());
        CHECK(index.classify(admissible) == ProposalEvidenceStatus::unknown);
        CHECK(index.classify(failed) == ProposalEvidenceStatus::unknown);
        check_stats(index, 1, 0, 0, 0, 0, false);

        CHECK(index.stale_if_admitted(admissible));
        CHECK(index.classify(admissible) == ProposalEvidenceStatus::stale);
    }

    SECTION("retired configuration tombstone insertion")
    {
        ProposalEvidenceIndex index(limits(8, 4));
        REQUIRE(index.admit(admissible));

        std::size_t retired = 1;
        {
            proposal_evidence_index_allocation_failure::OneShot fault(0);
            retired = index.retire_configuration(config);
        }

        CHECK(retired == 0);
        CHECK_FALSE(index.healthy());
        CHECK(index.classify(admissible) == ProposalEvidenceStatus::unknown);
        CHECK(index.classify(failed) == ProposalEvidenceStatus::unknown);
        INFO("the exact entry is not erased before the tombstone is durable");
        check_stats(index, 1, 0, 0, 0, 0, false);
    }
}

TEST_CASE("shutdown is inert and retains only explicit stale classifications",
          "[e08][proposal-evidence-index][shutdown][intentional-red]")
{
    const auto config = configuration(90, 1, "shutdown");
    const auto open = proposal(config, "open");
    const auto stale = proposal(config, "stale");
    const auto unknown = proposal(config, "unknown");
    const auto later_config = configuration(91, 2, "shutdown-later");
    const auto later = proposal(later_config, "shutdown-later");

    SECTION("a healthy shutdown rejects every mutator")
    {
        ProposalEvidenceIndex index(limits());
        REQUIRE(index.admit(open));
        REQUIRE(index.mark_stale(stale));
        index.shutdown();

        CHECK(index.classify(open) == ProposalEvidenceStatus::unknown);
        CHECK(index.classify(stale) == ProposalEvidenceStatus::stale);
        CHECK(index.classify(unknown) == ProposalEvidenceStatus::unknown);
        CHECK_FALSE(index.admit(unknown));
        CHECK_FALSE(index.mark_stale(unknown));
        CHECK_FALSE(index.stale_if_admitted(open));
        CHECK(index.retire_configuration(later_config) == 0);
        CHECK(index.advance_retirement_floor(91) == 0);
        CHECK(index.classify(later) == ProposalEvidenceStatus::unknown);
        check_stats(index, 1, 1, 0, 0, 0, true, true);

        index.shutdown();
        check_stats(index, 1, 1, 0, 0, 0, true, true);
    }

    SECTION("shutdown cannot clear a sticky unhealthy state")
    {
        ProposalEvidenceIndex index(limits(1, 1));
        REQUIRE(index.mark_stale(stale));
        REQUIRE_FALSE(index.admit(open));
        REQUIRE_FALSE(index.healthy());

        index.shutdown();

        CHECK(index.classify(stale) == ProposalEvidenceStatus::stale);
        CHECK(index.classify(open) == ProposalEvidenceStatus::unknown);
        CHECK(index.retire_configuration(later_config) == 0);
        CHECK(index.advance_retirement_floor(91) == 0);
        CHECK(index.classify(later) == ProposalEvidenceStatus::unknown);
        check_stats(index, 0, 1, 0, 0, 1, false, true);
    }
}

TEST_CASE("proposal evidence index source is a pure bounded manager component",
          "[e08][proposal-evidence-index][source-audit][pure]"
          "[intentional-red]")
{
    const auto header = read_source(
        "include/hotstuff/proposal_evidence_index.h");
    const auto source = read_source("src/proposal_evidence_index.cpp");
    const auto implementation = header + "\n" + source;

    CHECK(header.find("ProposalEvidenceIndexLimits") != std::string::npos);
    CHECK(header.find("ProposalEvidenceIndexStats") != std::string::npos);
    CHECK(header.find("ProposalEvidenceIndex") != std::string::npos);
    CHECK(header.find("ProposalEvidenceWindow") != std::string::npos);
    CHECK(implementation.find("maximum_exact_proposals") !=
          std::string::npos);
    CHECK(implementation.find("maximum_retired_configurations") !=
          std::string::npos);

    INFO("the index owns no clocks timers transport or protocol machinery");
    for (const auto *forbidden :
         {"system_clock",
          "steady_clock",
          "high_resolution_clock",
          "gettimeofday",
          "CLOCK_REALTIME",
          "std::time(",
          "TimerEvent",
          "EventContext",
          "PeerNetwork",
          "MsgNetwork",
          "NetAddr",
          "PeerId",
          "send_msg(",
          "HotStuffBase",
          "HotStuffCore",
          "QuorumCert",
          "on_receive_vote",
          "add_verified_part",
          "LeaderProgressMonitor",
          "rotate_active_view",
          "nmajority"})
    {
        CHECK(implementation.find(forbidden) == std::string::npos);
    }
}
