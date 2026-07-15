#include <string>

#include "catch.hpp"

/*
 * P05 FutureProposalBuffer public contract
 * ----------------------------------------
 * Keep this target runnable while the production seam is absent. Once the
 * header appears, the complete exact-key behavior matrix below is compiled.
 *
 * include/hotstuff/future_proposal_buffer.h must expose:
 *
 *   ProposalMetadata {
 *       ConfigurationId configuration;
 *       uint256_t block_hash;
 *       ReplicaID proposer;
 *       ProposalKey key() const;
 *       void serialize(DataStream &) const;
 *       void unserialize(DataStream &);
 *   };
 *
 *   BufferedProposal {
 *       ProposalMetadata metadata;
 *       bytearray_t wire_payload;
 *   };
 *
 *   FutureProposalBuffer {
 *       bool insert(BufferedProposal);
 *       bool contains(const ProposalKey &) const;
 *       std::size_t size() const;
 *       std::vector<BufferedProposal> drain(const ConfigurationId &);
 *       std::size_t purge(const ConfigurationId &);
 *   };
 *
 * insert returns false for an exact-key duplicate and retains the first
 * payload. drain selects the exact configuration (including digest), retains
 * per-configuration insertion order, removes returned items, and leaves all
 * nonmatching configurations untouched. purge is exact-configuration scoped.
 * This is a production-owned typed buffer, not a test-only classifier.
 */
#if __has_include("hotstuff/future_proposal_buffer.h")
#define KAURI_HAS_P05_FUTURE_PROPOSAL_BUFFER 1
#include <cstdint>
#include <type_traits>
#include <utility>
#include <vector>

#include "hotstuff/future_proposal_buffer.h"
#else
#define KAURI_HAS_P05_FUTURE_PROPOSAL_BUFFER 0
#endif

#if !KAURI_HAS_P05_FUTURE_PROPOSAL_BUFFER

TEST_CASE("P05 exact future proposal buffer is available",
          "[p05][future-proposals][contract][red]")
{
    INFO("Missing include/hotstuff/future_proposal_buffer.h. P05 requires "
         "a typed exact-ProposalKey buffer with configuration-selective "
         "drain, deduplication, and purge semantics.");
    REQUIRE(KAURI_HAS_P05_FUTURE_PROPOSAL_BUFFER == 1);
}

#else

namespace
{

using hotstuff::BufferedProposal;
using hotstuff::ConfigurationId;
using hotstuff::DataStream;
using hotstuff::FutureProposalBuffer;
using hotstuff::ProposalMetadata;
using hotstuff::ReplicaID;
using hotstuff::bytearray_t;
using hotstuff::uint256_t;

uint256_t digest(const std::string &label)
{
    return DataStream(label).get_hash();
}

ConfigurationId configuration(std::uint32_t epoch,
                              std::uint32_t tree,
                              const std::string &digest_label)
{
    return ConfigurationId{epoch, tree, digest(digest_label)};
}

BufferedProposal proposal(const ConfigurationId &configuration_id,
                          const std::string &block_label,
                          ReplicaID proposer = 0,
                          bytearray_t payload = {})
{
    return BufferedProposal{
        ProposalMetadata{
            configuration_id, digest(block_label), proposer},
        std::move(payload)};
}

template<typename Buffer, typename = void>
struct has_epoch_floor_purge : std::false_type
{};

template<typename Buffer>
struct has_epoch_floor_purge<
    Buffer,
    std::void_t<decltype(
        std::declval<Buffer &>().purge_before_epoch(
            std::declval<std::uint32_t>()))>> : std::true_type
{};

template<typename Buffer>
void check_epoch_floor_purge_contract()
{
    if constexpr (!has_epoch_floor_purge<Buffer>::value)
    {
        FAIL("FutureProposalBuffer must expose a monotonic-boundary helper "
             "purge_before_epoch(first_live_epoch)");
    }
    else
    {
        Buffer buffer;
        const auto old_a = configuration(40, 1, "epoch-forty-a");
        const auto old_b = configuration(41, 2, "epoch-forty-one-b");
        const auto first_live =
            configuration(42, 1, "epoch-forty-two-live");
        const auto current =
            configuration(43, 1, "epoch-forty-three-current");

        const auto old_a_proposal = proposal(old_a, "old-a");
        const auto old_b_proposal = proposal(old_b, "old-b");
        const auto first_live_proposal =
            proposal(first_live, "first-live");
        const auto current_proposal = proposal(current, "current");

        REQUIRE(buffer.insert(old_a_proposal));
        REQUIRE(buffer.insert(old_b_proposal));
        REQUIRE(buffer.insert(first_live_proposal));
        REQUIRE(buffer.insert(current_proposal));

        CHECK(buffer.purge_before_epoch(42) == 2);
        CHECK_FALSE(buffer.contains(old_a_proposal.metadata.key()));
        CHECK_FALSE(buffer.contains(old_b_proposal.metadata.key()));
        CHECK(buffer.contains(first_live_proposal.metadata.key()));
        CHECK(buffer.contains(current_proposal.metadata.key()));
        CHECK(buffer.size() == 2);

        // The boundary is exclusive and monotonic at its caller: repeating
        // or moving a caller-side floor backwards cannot remove first-live
        // and current proposals.
        CHECK(buffer.purge_before_epoch(42) == 0);
        CHECK(buffer.purge_before_epoch(41) == 0);
        CHECK(buffer.contains(first_live_proposal.metadata.key()));
        CHECK(buffer.contains(current_proposal.metadata.key()));

        CHECK(buffer.purge_before_epoch(43) == 1);
        CHECK_FALSE(buffer.contains(first_live_proposal.metadata.key()));
        CHECK(buffer.contains(current_proposal.metadata.key()));
        CHECK(buffer.size() == 1);
    }
}

} // namespace

TEST_CASE("proposal wire metadata round trips and binds the epoch digest",
          "[p05][future-proposals][wire]")
{
    const auto config_a = configuration(8, 7, "epoch-eight-a");
    const auto config_b = configuration(8, 7, "epoch-eight-b");
    const ProposalMetadata original{
        config_a, digest("shared-block"), ReplicaID{3}};

    DataStream encoded;
    original.serialize(encoded);
    const auto original_wire = encoded.get_hex();

    ProposalMetadata decoded;
    decoded.unserialize(encoded);

    CHECK(decoded.configuration == original.configuration);
    CHECK(decoded.block_hash == original.block_hash);
    CHECK(decoded.proposer == original.proposer);
    CHECK(decoded.key() == original.key());
    CHECK(encoded.size() == 0);

    ProposalMetadata divergent = original;
    divergent.configuration = config_b;
    DataStream divergent_wire;
    divergent.serialize(divergent_wire);
    CHECK(divergent_wire.get_hex() != original_wire);
}

TEST_CASE("equal block hashes remain distinct across exact configurations",
          "[p05][future-proposals][identity]")
{
    FutureProposalBuffer buffer;
    const auto epoch8 = configuration(8, 7, "epoch-eight");
    const auto epoch9 = configuration(9, 7, "epoch-nine");
    const auto divergent_epoch9 =
        configuration(9, 7, "epoch-nine-divergent");
    const auto shared_hash = digest("same-block-hash");

    BufferedProposal first{
        ProposalMetadata{epoch8, shared_hash, ReplicaID{0}}, {0x08}};
    BufferedProposal second{
        ProposalMetadata{epoch9, shared_hash, ReplicaID{1}}, {0x09}};
    BufferedProposal third{
        ProposalMetadata{
            divergent_epoch9, shared_hash, ReplicaID{2}},
        {0x19}};

    CHECK(buffer.insert(first));
    CHECK(buffer.insert(second));
    CHECK(buffer.insert(third));
    CHECK(buffer.size() == 3);
    CHECK(buffer.contains(first.metadata.key()));
    CHECK(buffer.contains(second.metadata.key()));
    CHECK(buffer.contains(third.metadata.key()));
}

TEST_CASE("duplicate future proposals retain one buffer entry and payload",
          "[p05][future-proposals][deduplication]")
{
    FutureProposalBuffer buffer;
    const auto config = configuration(4, 11, "epoch-four");
    const auto first = proposal(config, "block-a", 2, {0x01, 0x02});
    const auto duplicate = proposal(config, "block-a", 2, {0xff});

    CHECK(buffer.insert(first));
    CHECK_FALSE(buffer.insert(duplicate));
    CHECK(buffer.size() == 1);

    const auto drained = buffer.drain(config);
    REQUIRE(drained.size() == 1);
    CHECK((drained.front().wire_payload == bytearray_t{0x01, 0x02}));
    CHECK(buffer.size() == 0);
}

TEST_CASE("exact drain is not blocked by another configuration at the head",
          "[p05][future-proposals][drain]")
{
    FutureProposalBuffer buffer;
    const auto config_a = configuration(5, 7, "epoch-five");
    const auto config_b = configuration(6, 7, "epoch-six");

    const auto b_first = proposal(config_b, "b-first", 1, {0xb1});
    const auto a_first = proposal(config_a, "a-first", 0, {0xa1});
    const auto b_second = proposal(config_b, "b-second", 1, {0xb2});
    const auto a_second = proposal(config_a, "a-second", 0, {0xa2});

    REQUIRE(buffer.insert(b_first));
    REQUIRE(buffer.insert(a_first));
    REQUIRE(buffer.insert(b_second));
    REQUIRE(buffer.insert(a_second));

    const auto drained_a = buffer.drain(config_a);
    REQUIRE(drained_a.size() == 2);
    CHECK(drained_a[0].metadata.key() == a_first.metadata.key());
    CHECK(drained_a[1].metadata.key() == a_second.metadata.key());
    CHECK(buffer.size() == 2);
    CHECK(buffer.contains(b_first.metadata.key()));
    CHECK(buffer.contains(b_second.metadata.key()));

    const auto drained_a_again = buffer.drain(config_a);
    CHECK(drained_a_again.empty());

    const auto drained_b = buffer.drain(config_b);
    REQUIRE(drained_b.size() == 2);
    CHECK(drained_b[0].metadata.key() == b_first.metadata.key());
    CHECK(drained_b[1].metadata.key() == b_second.metadata.key());
    CHECK(buffer.size() == 0);
}

TEST_CASE("drain and purge match epoch tree and digest exactly",
          "[p05][future-proposals][purge]")
{
    FutureProposalBuffer buffer;
    const auto epoch7_tree3 = configuration(7, 3, "epoch-seven-a");
    const auto same_epoch_tree4 = configuration(7, 4, "epoch-seven-a");
    const auto divergent_epoch7_tree3 =
        configuration(7, 3, "epoch-seven-b");
    const auto epoch8_tree3 = configuration(8, 3, "epoch-eight");

    const auto exact = proposal(epoch7_tree3, "exact");
    const auto other_tree = proposal(same_epoch_tree4, "other-tree");
    const auto other_digest =
        proposal(divergent_epoch7_tree3, "other-digest");
    const auto other_epoch = proposal(epoch8_tree3, "other-epoch");

    REQUIRE(buffer.insert(exact));
    REQUIRE(buffer.insert(other_tree));
    REQUIRE(buffer.insert(other_digest));
    REQUIRE(buffer.insert(other_epoch));

    const auto drained = buffer.drain(epoch7_tree3);
    REQUIRE(drained.size() == 1);
    CHECK(drained.front().metadata.key() == exact.metadata.key());
    CHECK(buffer.size() == 3);

    CHECK(buffer.purge(divergent_epoch7_tree3) == 1);
    CHECK_FALSE(buffer.contains(other_digest.metadata.key()));
    CHECK(buffer.contains(other_tree.metadata.key()));
    CHECK(buffer.contains(other_epoch.metadata.key()));
    CHECK(buffer.purge(divergent_epoch7_tree3) == 0);
    CHECK(buffer.size() == 2);
}

TEST_CASE("epoch floor purge removes only proposals below first-live",
          "[proposal-retirement][future-proposals][floor]"
          "[intentional-red]")
{
    check_epoch_floor_purge_contract<FutureProposalBuffer>();
}

#endif
