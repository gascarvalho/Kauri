#include <string>

#include "catch.hpp"

/*
 * C04 public contract
 * -------------------
 * The production headers intentionally do not exist in the red commit.  Keep
 * this target runnable while the API is absent, then compile the complete
 * behavior matrix as soon as both public seams are introduced.
 *
 * include/hotstuff/configuration.h owns:
 *   - kEpochDefinitionSchemaVersion
 *   - ConfigurationId, ProposalKey, LeaderViewId and std::hash support
 *   - EpochTreeDefinition and EpochDefinitionInput request values
 *   - canonical_membership_digest, canonical_serialize_epoch and
 *     compute_epoch_digest
 *   - immutable EpochDefinition getters
 *   - parse_legacy_epoch_zero for the existing fan:/pipe: grammar
 *
 * include/hotstuff/epoch_store.h owns:
 *   - EpochValidationContext
 *   - EpochStore::stage with a strong no-mutation-on-rejection guarantee
 *   - const find_epoch/find_tree lookups keyed by external IDs
 *
 * EpochDefinitionInput::epoch_digest is optional.  An absent value asks the
 * trusted generator/store seam to compute it; a present value is an untrusted
 * claim and must match compute_epoch_digest(input).  The claimed digest itself
 * is not part of canonical serialization.
 */
#if __has_include("hotstuff/configuration.h") && \
    __has_include("hotstuff/epoch_store.h")
#define KAURI_HAS_C04_CONFIGURATION_API 1
#include <algorithm>
#include <cstdint>
#include <type_traits>
#include <typeindex>
#include <unordered_set>
#include <utility>
#include <vector>

#include "hotstuff/configuration.h"
#include "hotstuff/epoch_store.h"
#else
#define KAURI_HAS_C04_CONFIGURATION_API 0
#endif

#if !KAURI_HAS_C04_CONFIGURATION_API

TEST_CASE("C04 exact configuration API is available",
          "[c04][configuration][contract][red]")
{
    INFO("Missing include/hotstuff/configuration.h and/or "
         "include/hotstuff/epoch_store.h. C04 requires exact configuration, "
         "proposal, leader-view, epoch-definition, and epoch-store identities.");
    REQUIRE(KAURI_HAS_C04_CONFIGURATION_API == 1);
}

#else

namespace
{

using hotstuff::ConfigurationId;
using hotstuff::EpochDefinition;
using hotstuff::EpochDefinitionInput;
using hotstuff::EpochStore;
using hotstuff::EpochTreeDefinition;
using hotstuff::EpochValidationContext;
using hotstuff::LeaderViewId;
using hotstuff::ProposalKey;
using hotstuff::ReplicaID;
using hotstuff::uint256_t;

constexpr std::uint64_t kCurrentHeight = 10;
constexpr std::uint64_t kMinimumStagingGrace = 5;

uint256_t fixture_digest(const std::string &label)
{
    hotstuff::DataStream stream(label);
    return stream.get_hash();
}

std::vector<ReplicaID> membership7()
{
    return {0, 1, 2, 3, 4, 5, 6};
}

EpochTreeDefinition tree(
    std::uint32_t tree_id,
    std::vector<ReplicaID> members = membership7(),
    std::uint32_t fanout = 2,
    std::uint32_t pipeline_stretch = 2)
{
    return EpochTreeDefinition{
        tree_id, fanout, pipeline_stretch, std::move(members)};
}

EpochDefinitionInput epoch_zero_input(
    const std::vector<ReplicaID> &membership = membership7())
{
    EpochDefinitionInput input;
    input.schema_version = hotstuff::kEpochDefinitionSchemaVersion;
    input.epoch_number = 0;
    input.previous_epoch_digest = uint256_t{};
    input.membership_digest =
        hotstuff::canonical_membership_digest(membership);
    input.trees = {
        tree(42, {0, 1, 2, 3, 4, 5, 6}),
        tree(7, {1, 2, 3, 4, 5, 6, 0})};
    input.activation_height = 15;
    input.generation_seed = 0xCA04;
    input.policy_version = "c04-test-policy-v1";
    input.evidence_snapshot_id = "baseline-window-0001";
    input.evidence_cutoff = 100;
    input.epoch_digest.reset();
    return input;
}

EpochDefinitionInput successor_input(const EpochDefinition &predecessor)
{
    auto input = epoch_zero_input();
    input.epoch_number = predecessor.epoch_number() + 1;
    input.previous_epoch_digest = predecessor.epoch_digest();
    input.activation_height = predecessor.activation_height() + 10;
    input.generation_seed += input.epoch_number;
    input.evidence_snapshot_id = "successor-window-0002";
    input.evidence_cutoff += 100;
    input.trees = {
        tree(42, {2, 1, 0, 3, 4, 5, 6}),
        tree(7, {3, 2, 1, 0, 4, 5, 6})};
    input.epoch_digest.reset();
    return input;
}

EpochValidationContext validation_context(
    std::vector<ReplicaID> ineligible_members = {})
{
    EpochValidationContext context;
    context.current_height = kCurrentHeight;
    context.minimum_activation_grace = kMinimumStagingGrace;
    context.ineligible_members = std::move(ineligible_members);
    return context;
}

const EpochDefinition &stage_epoch_zero(EpochStore &store)
{
    return store.stage(epoch_zero_input(), validation_context());
}

template <typename Mutator>
uint256_t digest_after(Mutator mutator)
{
    auto input = epoch_zero_input();
    mutator(input);
    return hotstuff::compute_epoch_digest(input);
}

template <typename Mutator>
void require_rejected_without_store_mutation(
    EpochStore &store,
    EpochDefinitionInput input,
    Mutator mutator,
    const EpochValidationContext &context = validation_context())
{
    const auto size_before = store.size();
    mutator(input);
    REQUIRE_THROWS(store.stage(input, context));
    CHECK(store.size() == size_before);
}

struct StagingFailure
{
    std::type_index type;
    std::string message;
};

StagingFailure capture_staging_failure(
    EpochStore &store,
    const EpochDefinitionInput &input,
    const EpochValidationContext &context)
{
    try
    {
        store.stage(input, context);
        return {typeid(void), "no exception"};
    }
    catch (const std::exception &error)
    {
        return {typeid(error), error.what()};
    }
    catch (...)
    {
        return {typeid(void), "non-standard exception"};
    }
}

} // namespace

TEST_CASE("configuration identity is exact ordered and hashable",
          "[c04][configuration][identity]")
{
    const auto digest_a = fixture_digest("epoch-a");
    const auto digest_b = fixture_digest("epoch-b");
    const ConfigurationId epoch1_tree7{1, 7, digest_a};
    const ConfigurationId equal{1, 7, digest_a};
    const ConfigurationId epoch2_tree7{2, 7, digest_a};
    const ConfigurationId epoch1_tree8{1, 8, digest_a};
    const ConfigurationId divergent_epoch1_tree7{1, 7, digest_b};

    CHECK(epoch1_tree7 == equal);
    CHECK_FALSE(epoch1_tree7 != equal);
    CHECK(epoch1_tree7 != epoch2_tree7);
    CHECK(epoch1_tree7 != epoch1_tree8);
    CHECK(epoch1_tree7 != divergent_epoch1_tree7);

    CHECK(epoch1_tree7 < epoch2_tree7);
    CHECK(epoch1_tree7 < epoch1_tree8);
    CHECK((epoch1_tree7 < divergent_epoch1_tree7) !=
          (divergent_epoch1_tree7 < epoch1_tree7));

    std::unordered_set<ConfigurationId> identities;
    identities.insert(epoch1_tree7);
    identities.insert(equal);
    identities.insert(epoch2_tree7);
    identities.insert(epoch1_tree8);
    identities.insert(divergent_epoch1_tree7);
    CHECK(identities.size() == 4);
    CHECK(std::hash<ConfigurationId>{}(epoch1_tree7) ==
          std::hash<ConfigurationId>{}(equal));
}

TEST_CASE("proposal key scopes equal block hashes to exact configurations",
          "[c04][configuration][proposal-key]")
{
    const auto block_hash = fixture_digest("same-block");
    const auto other_block_hash = fixture_digest("other-block");
    const ConfigurationId epoch1{1, 4, fixture_digest("epoch-one")};
    const ConfigurationId epoch2{2, 4, fixture_digest("epoch-two")};
    const ProposalKey first{epoch1, block_hash};
    const ProposalKey equal{epoch1, block_hash};
    const ProposalKey same_block_new_configuration{epoch2, block_hash};
    const ProposalKey same_configuration_new_block{epoch1, other_block_hash};

    CHECK(first == equal);
    CHECK(first != same_block_new_configuration);
    CHECK(first != same_configuration_new_block);
    CHECK(first < same_block_new_configuration);

    std::unordered_set<ProposalKey> keys{
        first,
        equal,
        same_block_new_configuration,
        same_configuration_new_block};
    CHECK(keys.size() == 3);
    CHECK(std::hash<ProposalKey>{}(first) ==
          std::hash<ProposalKey>{}(equal));
}

TEST_CASE("leader view identity includes configuration generation and leader",
          "[c04][configuration][leader-view]")
{
    const ConfigurationId configuration{
        3, 9, fixture_digest("leader-view-epoch")};
    const ConfigurationId other_configuration{
        4, 9, fixture_digest("leader-view-next-epoch")};
    const LeaderViewId first{configuration, 11, 2};
    const LeaderViewId equal{configuration, 11, 2};
    const LeaderViewId new_generation{configuration, 12, 2};
    const LeaderViewId new_leader{configuration, 11, 3};
    const LeaderViewId new_configuration{other_configuration, 11, 2};

    CHECK(first == equal);
    CHECK(first != new_generation);
    CHECK(first != new_leader);
    CHECK(first != new_configuration);
    CHECK(first < new_generation);

    std::unordered_set<LeaderViewId> views{
        first, equal, new_generation, new_leader, new_configuration};
    CHECK(views.size() == 4);
    CHECK(std::hash<LeaderViewId>{}(first) ==
          std::hash<LeaderViewId>{}(equal));
}

TEST_CASE("canonical epoch serialization is deterministic and explicit",
          "[c04][configuration][canonical]")
{
    auto first = epoch_zero_input();
    auto same_logical_epoch = epoch_zero_input();
    std::reverse(
        same_logical_epoch.trees.begin(), same_logical_epoch.trees.end());

    const std::vector<ReplicaID> scrambled_membership{6, 2, 4, 0, 5, 1, 3};
    same_logical_epoch.membership_digest =
        hotstuff::canonical_membership_digest(scrambled_membership);

    CHECK(hotstuff::canonical_membership_digest(membership7()) ==
          hotstuff::canonical_membership_digest(scrambled_membership));
    CHECK(hotstuff::canonical_serialize_epoch(first) ==
          hotstuff::canonical_serialize_epoch(same_logical_epoch));
    CHECK(hotstuff::compute_epoch_digest(first) ==
          hotstuff::compute_epoch_digest(same_logical_epoch));

    EpochStore store(scrambled_membership);
    const auto &definition = store.stage(
        same_logical_epoch, validation_context());
    REQUIRE(definition.trees().size() == 2);
    CHECK(definition.trees()[0].tree_id == 7);
    CHECK(definition.trees()[1].tree_id == 42);
    CHECK(definition.canonical_serialization() ==
          hotstuff::canonical_serialize_epoch(first));
    CHECK(definition.epoch_digest() ==
          hotstuff::compute_epoch_digest(first));
    CHECK(definition.schema_version() ==
          hotstuff::kEpochDefinitionSchemaVersion);
}

TEST_CASE("adaptive v2 derives the fixed N7 Byzantine quorum",
          "[c01][configuration][adaptive-v2][quorum]")
{
    const auto quorum = hotstuff::derive_byzantine_quorum(7);
    REQUIRE(quorum.has_value());
    CHECK((hotstuff::kEpochDefinitionSchemaVersionV2 == 2 &&
           quorum->replica_count == 7 &&
           quorum->fault_threshold == 2 &&
           quorum->quorum == 5));
}

TEST_CASE("adaptive v2 epoch zero helper pins replica bootstrap metadata",
          "[c04][configuration][adaptive-v2][bootstrap]")
{
    const std::vector<EpochTreeDefinition> trees{
        tree(42, {0, 1, 2, 3, 4, 5, 6}),
        tree(7, {1, 2, 3, 4, 5, 6, 0})};
    const auto input = hotstuff::adaptive_v2_epoch_zero_input(
        membership7(), trees);

    CHECK(input.schema_version ==
          hotstuff::kEpochDefinitionSchemaVersionV2);
    CHECK(input.epoch_number == 0);
    CHECK(input.previous_epoch_digest == uint256_t{});
    CHECK(input.membership_digest ==
          hotstuff::canonical_membership_digest(membership7()));
    REQUIRE(input.trees.size() == trees.size());
    CHECK(input.trees[0].tree_id == trees[0].tree_id);
    CHECK(input.trees[0].members_breadth_first ==
          trees[0].members_breadth_first);
    CHECK(input.trees[1].tree_id == trees[1].tree_id);
    CHECK(input.trees[1].members_breadth_first ==
          trees[1].members_breadth_first);
    CHECK(input.activation_height == 0);
    CHECK(input.generation_seed == 0);
    CHECK(input.policy_version == "adaptive-v2-bootstrap");
    CHECK(input.evidence_snapshot_id ==
          "adaptive-v2-bootstrap-epoch-zero");
    CHECK(input.evidence_cutoff == 0);
    CHECK_FALSE(input.epoch_digest.has_value());
}

TEST_CASE("every protocol-relevant epoch field is digest-bound",
          "[c04][configuration][digest]")
{
    const auto baseline =
        hotstuff::compute_epoch_digest(epoch_zero_input());

    const std::vector<std::pair<std::string, uint256_t>> changed{
        {"schema version", digest_after([](auto &input) {
             ++input.schema_version;
         })},
        {"epoch number", digest_after([](auto &input) {
             ++input.epoch_number;
         })},
        {"activation height", digest_after([](auto &input) {
             ++input.activation_height;
         })},
        {"member placement", digest_after([](auto &input) {
             std::swap(input.trees[0].members_breadth_first[1],
                       input.trees[0].members_breadth_first[2]);
         })},
        {"predecessor digest", digest_after([](auto &input) {
             input.previous_epoch_digest = fixture_digest("other-predecessor");
         })},
        {"membership digest", digest_after([](auto &input) {
             input.membership_digest = fixture_digest("other-membership");
         })},
        {"generation seed", digest_after([](auto &input) {
             ++input.generation_seed;
         })},
        {"policy version", digest_after([](auto &input) {
             input.policy_version += "-changed";
         })},
        {"evidence snapshot", digest_after([](auto &input) {
             input.evidence_snapshot_id += "-changed";
         })},
        {"evidence cutoff", digest_after([](auto &input) {
             ++input.evidence_cutoff;
         })},
        {"tree id", digest_after([](auto &input) {
             ++input.trees[0].tree_id;
         })},
        {"tree fanout", digest_after([](auto &input) {
             ++input.trees[0].fanout;
         })},
        {"pipeline stretch", digest_after([](auto &input) {
             ++input.trees[0].pipeline_stretch;
         })}};

    for (const auto &[field, digest] : changed)
    {
        INFO("field must change the epoch digest: " << field);
        CHECK(digest != baseline);
    }
}

TEST_CASE("store rejects invalid epoch sequencing without mutation",
          "[c04][configuration][store][validation]")
{
    EpochStore store(membership7());
    const auto &epoch0 = stage_epoch_zero(store);
    auto epoch1 = successor_input(epoch0);
    const auto epoch0_digest = epoch0.epoch_digest();

    SECTION("duplicate epoch")
    {
        require_rejected_without_store_mutation(
            store, epoch_zero_input(), [](auto &) {});
    }

    SECTION("non-successor sparse epoch")
    {
        require_rejected_without_store_mutation(
            store, epoch1, [](auto &input) { ++input.epoch_number; });
    }

    SECTION("predecessor mismatch")
    {
        require_rejected_without_store_mutation(
            store, epoch1, [](auto &input) {
                input.previous_epoch_digest = fixture_digest("wrong-predecessor");
            });
    }

    SECTION("membership digest mismatch")
    {
        require_rejected_without_store_mutation(
            store, epoch1, [](auto &input) {
                input.membership_digest = fixture_digest("wrong-membership");
            });
    }

    SECTION("claimed epoch digest mismatch")
    {
        require_rejected_without_store_mutation(
            store, epoch1, [](auto &input) {
                input.epoch_digest = fixture_digest("forged-epoch-digest");
            });
    }

    SECTION("unsupported schema version")
    {
        require_rejected_without_store_mutation(
            store, epoch1, [](auto &input) { input.schema_version = 0; });
    }

    SECTION("activation height lacks staging grace")
    {
        require_rejected_without_store_mutation(
            store, epoch1, [](auto &input) {
                input.activation_height =
                    kCurrentHeight + kMinimumStagingGrace - 1;
            });
    }

    CHECK(store.size() == 1);
    REQUIRE(store.find_epoch(0) != nullptr);
    CHECK(store.find_epoch(0)->epoch_digest() == epoch0_digest);
    CHECK(store.find_epoch(1) == nullptr);
}

TEST_CASE("store rejects malformed tree membership without mutation",
          "[c04][configuration][tree][validation]")
{
    EpochStore store(membership7());

    SECTION("no trees")
    {
        require_rejected_without_store_mutation(
            store, epoch_zero_input(), [](auto &input) {
                input.trees.clear();
            });
    }

    SECTION("empty tree")
    {
        require_rejected_without_store_mutation(
            store, epoch_zero_input(), [](auto &input) {
                input.trees[0].members_breadth_first.clear();
            });
    }

    SECTION("duplicate member")
    {
        require_rejected_without_store_mutation(
            store, epoch_zero_input(), [](auto &input) {
                input.trees[0].members_breadth_first[6] = 5;
            });
    }

    SECTION("missing member")
    {
        require_rejected_without_store_mutation(
            store, epoch_zero_input(), [](auto &input) {
                input.trees[0].members_breadth_first.pop_back();
            });
    }

    SECTION("unknown member")
    {
        require_rejected_without_store_mutation(
            store, epoch_zero_input(), [](auto &input) {
                input.trees[0].members_breadth_first[6] = 99;
            });
    }

    SECTION("duplicate tree id")
    {
        require_rejected_without_store_mutation(
            store, epoch_zero_input(), [](auto &input) {
                input.trees[1].tree_id = input.trees[0].tree_id;
            });
    }

    SECTION("zero fanout")
    {
        require_rejected_without_store_mutation(
            store, epoch_zero_input(), [](auto &input) {
                input.trees[0].fanout = 0;
            });
    }

    CHECK(store.size() == 0);
}

TEST_CASE("legacy zero pipeline stretch is preserved exactly",
          "[c04][configuration][legacy][pipeline]")
{
    const std::string disabled_pipeline =
        "fan:2 pipe:0 0 1 2 3 4 5 6\n";
    const std::string enabled_pipeline =
        "fan:2 pipe:1 0 1 2 3 4 5 6\n";

    auto input = hotstuff::parse_legacy_epoch_zero(
        disabled_pipeline, membership7());
    const auto enabled_input = hotstuff::parse_legacy_epoch_zero(
        enabled_pipeline, membership7());

    REQUIRE(input.trees.size() == 1);
    CHECK(input.trees[0].pipeline_stretch == 0);
    CHECK(hotstuff::compute_epoch_digest(input) !=
          hotstuff::compute_epoch_digest(enabled_input));

    EpochStore store(membership7());
    const auto &definition = store.stage(
        input, EpochValidationContext{});
    REQUIRE(definition.trees().size() == 1);
    CHECK(definition.trees()[0].pipeline_stretch == 0);
    CHECK(definition.epoch_digest() ==
          hotstuff::compute_epoch_digest(input));
}

TEST_CASE("duplicate tree validation is deterministic before tree contents",
          "[c04][rem-c04-01][configuration][tree][validation]")
{
    const auto valid = tree(7, {0, 1, 2, 3, 4, 5, 6});
    const auto malformed = tree(7, {0, 1, 2, 3, 4, 5, 99});
    EpochStore valid_first_store(membership7());
    EpochStore malformed_first_store(membership7());
    const auto &valid_first_predecessor =
        stage_epoch_zero(valid_first_store);
    const auto &malformed_first_predecessor =
        stage_epoch_zero(malformed_first_store);

    auto valid_first = successor_input(valid_first_predecessor);
    valid_first.trees = {valid, malformed};
    valid_first.epoch_digest.reset();

    auto malformed_first = successor_input(malformed_first_predecessor);
    malformed_first.trees = {malformed, valid};
    malformed_first.epoch_digest.reset();

    const auto predecessor_bytes =
        valid_first_predecessor.canonical_serialization();
    const auto predecessor_digest = valid_first_predecessor.epoch_digest();
    const auto valid_first_bytes =
        hotstuff::canonical_serialize_epoch(valid_first);
    const auto malformed_first_bytes =
        hotstuff::canonical_serialize_epoch(malformed_first);
    const auto first_context = validation_context();
    const auto second_context = validation_context();

    const auto valid_first_failure = capture_staging_failure(
        valid_first_store, valid_first, first_context);
    const auto malformed_first_failure = capture_staging_failure(
        malformed_first_store, malformed_first, second_context);

    const std::string expected =
        "epoch validation: duplicate tree ID 7";
    CHECK(valid_first_failure.type == typeid(std::invalid_argument));
    CHECK(malformed_first_failure.type == typeid(std::invalid_argument));
    CHECK(valid_first_failure.type == malformed_first_failure.type);
    CHECK(valid_first_failure.message == expected);
    CHECK(malformed_first_failure.message == expected);
    CHECK(valid_first_failure.message == malformed_first_failure.message);

    CHECK(valid_first_store.size() == 1);
    CHECK(malformed_first_store.size() == 1);
    REQUIRE(valid_first_store.find_epoch(0) != nullptr);
    REQUIRE(malformed_first_store.find_epoch(0) != nullptr);
    CHECK(valid_first_store.find_epoch(1) == nullptr);
    CHECK(malformed_first_store.find_epoch(1) == nullptr);
    CHECK(valid_first_store.find_epoch(0)->canonical_serialization() ==
          predecessor_bytes);
    CHECK(malformed_first_store.find_epoch(0)->canonical_serialization() ==
          predecessor_bytes);
    CHECK(valid_first_store.find_epoch(0)->epoch_digest() ==
          predecessor_digest);
    CHECK(malformed_first_store.find_epoch(0)->epoch_digest() ==
          predecessor_digest);
    CHECK(valid_first_store.find_tree(1, 7) == nullptr);
    CHECK(malformed_first_store.find_tree(1, 7) == nullptr);
    CHECK(hotstuff::canonical_serialize_epoch(valid_first) ==
          valid_first_bytes);
    CHECK(hotstuff::canonical_serialize_epoch(malformed_first) ==
          malformed_first_bytes);
}

TEST_CASE("root eligibility and leaf capacity are validated externally",
          "[c04][configuration][tree][leaf-capacity]")
{
    SECTION("an ineligible root is rejected")
    {
        EpochStore store(membership7());
        auto context = validation_context({0});
        REQUIRE_THROWS(store.stage(epoch_zero_input(), context));
        CHECK(store.size() == 0);
    }

    SECTION("more ineligible replicas than leaf slots is rejected")
    {
        EpochStore store(membership7());
        auto context = validation_context({0, 1, 2, 3, 4});
        REQUIRE_THROWS(store.stage(epoch_zero_input(), context));
        CHECK(store.size() == 0);
    }

    SECTION("ineligible replicas placed only in leaf slots are accepted")
    {
        EpochStore store(membership7());
        auto input = epoch_zero_input();
        input.trees = {
            tree(7, {0, 1, 2, 3, 4, 5, 6}),
            tree(42, {1, 2, 0, 3, 4, 5, 6})};
        const auto &definition = store.stage(
            input, validation_context({5, 6}));
        CHECK(definition.epoch_number() == 0);
        CHECK(store.size() == 1);
    }
}

TEST_CASE("epoch and tree lookup uses external IDs without insertion",
          "[c04][configuration][store][lookup]")
{
    EpochStore store(membership7());
    const auto &epoch0 = stage_epoch_zero(store);
    const auto &epoch1 = store.stage(
        successor_input(epoch0), validation_context());
    const EpochStore &const_store = store;

    static_assert(std::is_same_v<
                  decltype(std::declval<EpochStore &>().stage(
                      std::declval<const EpochDefinitionInput &>(),
                      std::declval<const EpochValidationContext &>())),
                  const EpochDefinition &>);
    static_assert(std::is_same_v<
                  decltype(const_store.find_epoch(0)),
                  const EpochDefinition *>);
    static_assert(std::is_same_v<
                  decltype(const_store.find_tree(0, 7)),
                  const EpochTreeDefinition *>);

    REQUIRE(const_store.find_epoch(0) != nullptr);
    REQUIRE(const_store.find_epoch(1) != nullptr);
    CHECK(const_store.find_epoch(0)->epoch_digest() !=
          const_store.find_epoch(1)->epoch_digest());

    REQUIRE(const_store.find_tree(0, 7) != nullptr);
    REQUIRE(const_store.find_tree(0, 42) != nullptr);
    REQUIRE(const_store.find_tree(1, 7) != nullptr);
    CHECK(const_store.find_tree(0, 7)->tree_id == 7);
    CHECK(const_store.find_tree(0, 42)->tree_id == 42);
    CHECK(const_store.find_tree(1, 7)->members_breadth_first !=
          const_store.find_tree(0, 7)->members_breadth_first);

    const auto size_before = const_store.size();
    CHECK(const_store.find_epoch(2) == nullptr);
    CHECK(const_store.find_epoch(99) == nullptr);
    CHECK(const_store.find_tree(0, 0) == nullptr);
    CHECK(const_store.find_tree(0, 999) == nullptr);
    CHECK(const_store.find_tree(999, 7) == nullptr);
    CHECK(const_store.size() == size_before);

    const ConfigurationId epoch0_tree7{
        0, 7, const_store.find_epoch(0)->epoch_digest()};
    const ConfigurationId epoch1_tree7{
        1, 7, epoch1.epoch_digest()};
    CHECK(epoch0_tree7 != epoch1_tree7);
}

TEST_CASE("legacy static epoch zero grammar adapts into a validated definition",
          "[c04][configuration][legacy][control]")
{
    const std::string fixture =
        "fan:2 pipe:2 0 1 2 3 4 5 6\n"
        "fan:2 pipe:3 1 2 3 4 5 6 0\n";
    auto input = hotstuff::parse_legacy_epoch_zero(fixture, membership7());

    CHECK(input.schema_version == hotstuff::kEpochDefinitionSchemaVersion);
    CHECK(input.epoch_number == 0);
    REQUIRE(input.trees.size() == 2);
    CHECK(input.trees[0].tree_id == 0);
    CHECK(input.trees[1].tree_id == 1);
    CHECK(input.trees[0].fanout == 2);
    CHECK(input.trees[1].pipeline_stretch == 3);

    EpochStore store(membership7());
    const auto &definition = store.stage(
        input, EpochValidationContext{});
    CHECK(definition.epoch_number() == 0);
    CHECK(definition.schema_version() ==
          hotstuff::kEpochDefinitionSchemaVersion);
    CHECK(definition.epoch_digest() ==
          hotstuff::compute_epoch_digest(input));
    CHECK(store.find_tree(0, 0) != nullptr);
    CHECK(store.find_tree(0, 1) != nullptr);
}

#endif
