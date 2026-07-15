#include <cstdint>
#include <limits>
#include <memory>
#include <tuple>
#include <type_traits>
#include <unordered_map>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/hotstuff.h"
#include "support/bls_fixtures.h"

using hotstuff::Epoch;
using hotstuff::MsgRelay;
using hotstuff::MsgVote;
using hotstuff::PeerId;
using hotstuff::QuorumCertAggBLS;
using hotstuff::ReplicaConfig;
using hotstuff::ReplicaID;
using hotstuff::Tree;
using hotstuff::TreeNetwork;
using hotstuff::Vote;
using hotstuff::VoteRelay;
using hotstuff::part_cert_bt;
using hotstuff::quorum_cert_bt;
using hotstuff::test::BlsTestCore;
using hotstuff::test::add_valid_signers;
using hotstuff::test::make_bls_private_key;
using hotstuff::test::make_digest;
using hotstuff::test::make_test_proposal_key;
using hotstuff::test::serialized_hex;

namespace
{

using EpochStore = std::vector<Epoch>;

template<typename Store, typename = void>
struct has_safe_message_tree_lookup : std::false_type
{};

template<typename Store>
struct has_safe_message_tree_lookup<
    Store,
    std::void_t<decltype(find_message_tree(
        std::declval<const Store &>(),
        std::declval<std::uint32_t>(),
        std::declval<std::uint32_t>()))>>
    : std::bool_constant<std::is_convertible_v<
          decltype(find_message_tree(
              std::declval<const Store &>(),
              std::declval<std::uint32_t>(),
              std::declval<std::uint32_t>())),
          const TreeNetwork *>>
{};

template<typename Store>
const TreeNetwork *safe_message_tree(const Store &epochs,
                                     std::uint32_t epoch,
                                     std::uint32_t tree)
{
    if constexpr (has_safe_message_tree_lookup<Store>::value)
        return find_message_tree(epochs, epoch, tree);
    return nullptr;
}

template<typename Config, typename Network, typename Message, typename = void>
struct has_verified_vote_admission : std::false_type
{};

template<typename Config, typename Network, typename Message>
struct has_verified_vote_admission<
    Config,
    Network,
    Message,
    std::void_t<decltype(admit_verified_vote(
        std::declval<const Config &>(),
        std::declval<std::uint32_t>(),
        std::declval<const Network &>(),
        std::declval<const PeerId &>(),
        std::declval<const Message &>(),
        std::declval<bool>()))>>
    : std::bool_constant<std::is_convertible_v<
          decltype(admit_verified_vote(
              std::declval<const Config &>(),
              std::declval<std::uint32_t>(),
              std::declval<const Network &>(),
              std::declval<const PeerId &>(),
              std::declval<const Message &>(),
              std::declval<bool>())),
          bool>>
{};

template<typename Config, typename Network, typename Message, typename = void>
struct has_verified_relay_admission : std::false_type
{};

template<typename Config, typename Network, typename Message>
struct has_verified_relay_admission<
    Config,
    Network,
    Message,
    std::void_t<decltype(admit_verified_relay(
        std::declval<const Config &>(),
        std::declval<std::uint32_t>(),
        std::declval<const Network &>(),
        std::declval<const PeerId &>(),
        std::declval<const Message &>(),
        std::declval<bool>()))>>
    : std::bool_constant<std::is_convertible_v<
          decltype(admit_verified_relay(
              std::declval<const Config &>(),
              std::declval<std::uint32_t>(),
              std::declval<const Network &>(),
              std::declval<const PeerId &>(),
              std::declval<const Message &>(),
              std::declval<bool>())),
          bool>>
{};

template<typename Config, typename Network, typename Message>
bool admit_vote(const Config &config,
                std::uint32_t epoch,
                const Network &tree,
                const PeerId &peer,
                const Message &vote,
                bool cryptographically_verified)
{
    if constexpr (has_verified_vote_admission<Config, Network, Message>::value)
        return admit_verified_vote(
            config, epoch, tree, peer, vote, cryptographically_verified);
    return false;
}

template<typename Config, typename Network, typename Message>
bool admit_relay(const Config &config,
                 std::uint32_t epoch,
                 const Network &tree,
                 const PeerId &peer,
                 const Message &relay,
                 bool cryptographically_verified)
{
    if constexpr (has_verified_relay_admission<Config, Network, Message>::value)
        return admit_verified_relay(
            config, epoch, tree, peer, relay, cryptographically_verified);
    return false;
}

std::vector<std::tuple<hotstuff::NetAddr,
                       hotstuff::pubkey_bt,
                       hotstuff::uint256_t>>
make_tree_replicas(const BlsTestCore &core)
{
    std::vector<std::tuple<hotstuff::NetAddr,
                           hotstuff::pubkey_bt,
                           hotstuff::uint256_t>> replicas;
    replicas.reserve(core.get_config().nreplicas);

    for (ReplicaID replica = 0;
         replica < core.get_config().nreplicas;
         ++replica)
    {
        auto key = make_bls_private_key(replica);
        const hotstuff::NetAddr address(
            static_cast<std::uint32_t>(0x7f000001),
            static_cast<std::uint16_t>(12000 + replica));
        const auto peer_hash = static_cast<const hotstuff::uint256_t &>(
            core.get_config().get_peer_id(replica));
        replicas.emplace_back(address, key.get_pubkey(), peer_hash);
    }
    return replicas;
}

TreeNetwork make_tree_network(BlsTestCore &core,
                              ReplicaID local_replica,
                              std::uint32_t tree_id = 0)
{
    const Tree tree(
        tree_id, 2, 2, std::vector<std::uint32_t>{0, 1, 2, 3, 4, 5, 6});
    const auto replicas = make_tree_replicas(core);
    return TreeNetwork(tree, replicas, local_replica);
}

Vote round_trip_vote(BlsTestCore &core, Vote vote)
{
    MsgVote wire(vote);
    wire.postponed_parse(&core);
    return std::move(wire.vote);
}

VoteRelay round_trip_relay(BlsTestCore &core, VoteRelay relay)
{
    MsgRelay wire(relay);
    wire.postponed_parse(&core);
    return std::move(wire.vote);
}

quorum_cert_bt make_valid_relay_certificate(
    BlsTestCore &core,
    const hotstuff::ProposalKey &key,
    const std::vector<ReplicaID> &signers)
{
    quorum_cert_bt certificate(
        new QuorumCertAggBLS(core.get_config(), key));
    auto &bls = dynamic_cast<QuorumCertAggBLS &>(*certificate);
    add_valid_signers(bls, core, signers, key);
    certificate->compute();
    return certificate;
}

quorum_cert_bt make_invalid_relay_certificate(
    BlsTestCore &core,
    const hotstuff::ProposalKey &key)
{
    salticidae::Bits claimed(core.get_config().nreplicas);
    claimed.clear();
    claimed.set(1);
    auto wrong_part = core.make_part(2, key);
    const auto &wrong_signature =
        dynamic_cast<const hotstuff::SigSecBLSAgg &>(*wrong_part);

    hotstuff::DataStream wire;
    hotstuff::serialize_proposal_key(wire, key);
    wire << claimed << true;
    wrong_signature.SigSecBLSAgg::serialize(wire);

    quorum_cert_bt certificate(
        new QuorumCertAggBLS(core.get_config(), key));
    certificate->unserialize(wire);
    return certificate;
}

struct VerificationCounts
{
    std::size_t synchronous = 0;
    std::size_t worker = 0;
};

class CountingPartCert final : public hotstuff::PartCertBLSAgg
{
public:
    CountingPartCert(const hotstuff::PrivKeyBLS &key,
                     const hotstuff::ProposalKey &proposal_key,
                     std::shared_ptr<VerificationCounts> counts)
        : PartCertBLSAgg(key, proposal_key), counts_(std::move(counts))
    {}

    CountingPartCert(const CountingPartCert &) = default;

    bool verify(const hotstuff::PubKey &public_key) override
    {
        ++counts_->synchronous;
        return PartCertBLSAgg::verify(public_key);
    }

    hotstuff::promise_t verify(const hotstuff::PubKey &public_key,
                               hotstuff::VeriPool &) override
    {
        ++counts_->worker;
        const bool valid = PartCertBLSAgg::verify(public_key);
        return hotstuff::promise_t(
            [valid](hotstuff::promise_t &promise) { promise.resolve(valid); });
    }

    CountingPartCert *clone() override
    {
        return new CountingPartCert(*this);
    }

private:
    std::shared_ptr<VerificationCounts> counts_;
};

class VerifiedAccumulator final : public QuorumCertAggBLS
{
public:
    using QuorumCertAggBLS::QuorumCertAggBLS;

    void add_after_worker_verification(const ReplicaConfig &config,
                                       ReplicaID signer,
                                       const hotstuff::PartCert &part)
    {
        add_verified_part(config, signer, part);
    }
};

struct MutationSnapshot
{
    std::size_t signer_count;
    std::string certificate_bytes;
    std::size_t latency_observations;
    std::size_t pending_vote_updates;
};

MutationSnapshot snapshot(const QuorumCertAggBLS &certificate,
                          std::size_t latency_observations,
                          std::size_t pending_vote_updates)
{
    return {const_cast<QuorumCertAggBLS &>(certificate).get_sigs_n(),
            serialized_hex(certificate),
            latency_observations,
            pending_vote_updates};
}

bool operator==(const MutationSnapshot &left, const MutationSnapshot &right)
{
    return left.signer_count == right.signer_count &&
           left.certificate_bytes == right.certificate_bytes &&
           left.latency_observations == right.latency_observations &&
           left.pending_vote_updates == right.pending_vote_updates;
}

} // namespace

TEST_CASE("vote and relay handlers require a safe exact-tree admission seam",
          "[rem-s02-01][handler][contract]")
{
    INFO("Expected production boundary: find_message_tree(const vector<Epoch>&, epoch, tid) -> const TreeNetwork*");
    CHECK(has_safe_message_tree_lookup<EpochStore>::value);

    INFO("Expected event-loop callback boundary: admit_verified_vote(config, epoch, tree, peer, vote, immutable_result)");
    CHECK((has_verified_vote_admission<
           ReplicaConfig, TreeNetwork, Vote>::value));

    INFO("Expected event-loop callback boundary: admit_verified_relay(config, epoch, tree, peer, relay, immutable_result)");
    CHECK((has_verified_relay_admission<
           ReplicaConfig, TreeNetwork, VoteRelay>::value));
}

TEST_CASE("malformed wire vote and relay identities fail before topology lookup or mutation",
          "[rem-s02-01][handler][identity]")
{
    BlsTestCore core(7);
    const auto block_hash = make_digest(0x71);
    const auto key = make_test_proposal_key(block_hash, 0xd1, 0, 0);
    auto root_tree = make_tree_network(core, 0);
    EpochStore epochs{Epoch(0, std::vector<TreeNetwork>{root_tree})};

    QuorumCertAggBLS accumulator(core.get_config(), key);
    add_valid_signers(accumulator, core, {0}, key);
    accumulator.compute();
    std::size_t latency_observations = 0;
    std::size_t pending_vote_updates = 0;
    const auto before = snapshot(
        accumulator, latency_observations, pending_vote_updates);
    const auto map_size_before = epochs.front().system_trees.size();

    REQUIRE(has_safe_message_tree_lookup<EpochStore>::value);

    SECTION("out-of-range epoch in MsgVote")
    {
        auto vote = core.make_vote(1, 1, key);
        vote.epoch_nr = std::numeric_limits<std::uint32_t>::max();
        const auto parsed = round_trip_vote(core, std::move(vote));
        const TreeNetwork *tree = nullptr;
        REQUIRE_NOTHROW(tree = safe_message_tree(
            epochs, parsed.epoch_nr, parsed.tid));
        CHECK(tree == nullptr);
    }

    SECTION("unknown tree in MsgVote")
    {
        auto vote = core.make_vote(1, 1, key);
        vote.tid = 99;
        const auto parsed = round_trip_vote(core, std::move(vote));
        const TreeNetwork *tree = nullptr;
        REQUIRE_NOTHROW(tree = safe_message_tree(
            epochs, parsed.epoch_nr, parsed.tid));
        CHECK(tree == nullptr);
    }

    SECTION("out-of-range epoch in MsgRelay")
    {
        auto malformed_key = key;
        malformed_key.configuration.epoch_number =
            std::numeric_limits<std::uint32_t>::max();
        VoteRelay relay(
            malformed_key,
            make_valid_relay_certificate(core, key, {1}),
            &core);
        const auto parsed = round_trip_relay(core, std::move(relay));
        const TreeNetwork *tree = nullptr;
        REQUIRE_NOTHROW(tree = safe_message_tree(
            epochs, parsed.epoch_nr, parsed.tid));
        CHECK(tree == nullptr);
    }

    SECTION("unknown tree in MsgRelay")
    {
        auto malformed_key = key;
        malformed_key.configuration.tree_id = 99;
        VoteRelay relay(
            malformed_key,
            make_valid_relay_certificate(core, key, {1}),
            &core);
        const auto parsed = round_trip_relay(core, std::move(relay));
        const TreeNetwork *tree = nullptr;
        REQUIRE_NOTHROW(tree = safe_message_tree(
            epochs, parsed.epoch_nr, parsed.tid));
        CHECK(tree == nullptr);
    }

    SECTION("stored epoch identity does not match its vector position")
    {
        EpochStore malformed_epochs{
            Epoch(7, std::vector<TreeNetwork>{root_tree})};
        const TreeNetwork *tree = nullptr;
        REQUIRE_NOTHROW(tree = safe_message_tree(
            malformed_epochs, 0, 0));
        CHECK(tree == nullptr);
        CHECK(malformed_epochs.front().system_trees.empty());
    }

    CHECK(epochs.front().system_trees.size() == map_size_before);
    CHECK(snapshot(accumulator,
                   latency_observations,
                   pending_vote_updates) == before);
}

TEST_CASE("direct-vote admission distinguishes root and non-root child identity",
          "[rem-s02-01][handler][vote-admission]")
{
    BlsTestCore core(7);
    const auto block_hash = make_digest(0x72);
    const auto key = make_test_proposal_key(block_hash, 0xd1, 0, 0);
    const auto &config = core.get_config();

    REQUIRE((has_verified_vote_admission<
             ReplicaConfig, TreeNetwork, Vote>::value));

    SECTION("root accepts a valid direct child")
    {
        const auto root = make_tree_network(core, 0);
        const auto vote = core.make_vote(1, 1, key);
        CHECK(admit_vote(config,
                         0,
                         root,
                         config.get_peer_id(1),
                         vote,
                         true));
    }

    SECTION("non-root accepts a valid direct child")
    {
        const auto intermediate = make_tree_network(core, 1);
        const auto vote = core.make_vote(3, 3, key);
        CHECK(admit_vote(config,
                         0,
                         intermediate,
                         config.get_peer_id(3),
                         vote,
                         true));
    }

    SECTION("authenticated non-child is rejected")
    {
        const auto root = make_tree_network(core, 0);
        const auto vote = core.make_vote(5, 5, key);
        CHECK_FALSE(admit_vote(config,
                               0,
                               root,
                               config.get_peer_id(5),
                               vote,
                               true));
    }

    SECTION("authenticated peer and claimed voter mismatch is rejected")
    {
        const auto root = make_tree_network(core, 0);
        const auto vote = core.make_vote(2, 2, key);
        CHECK_FALSE(admit_vote(config,
                               0,
                               root,
                               config.get_peer_id(1),
                               vote,
                               true));
    }

    SECTION("invalid signature result is rejected")
    {
        const auto root = make_tree_network(core, 0);
        const auto vote = core.make_vote(1, 2, key);
        CHECK_FALSE(admit_vote(config,
                               0,
                               root,
                               config.get_peer_id(1),
                               vote,
                               false));
    }

    SECTION("exact epoch and tree mismatch is rejected")
    {
        const auto root = make_tree_network(core, 0);
        auto vote = core.make_vote(1, 1, key);
        vote.epoch_nr = 1;
        vote.tid = 1;
        CHECK_FALSE(admit_vote(config,
                               0,
                               root,
                               config.get_peer_id(1),
                               vote,
                               true));
    }
}

TEST_CASE("relay admission distinguishes root and intermediate direct children",
          "[rem-s02-01][handler][relay-admission]")
{
    BlsTestCore core(7);
    const auto block_hash = make_digest(0x73);
    const auto key = make_test_proposal_key(block_hash, 0xd1, 0, 0);
    const auto &config = core.get_config();

    REQUIRE((has_verified_relay_admission<
             ReplicaConfig, TreeNetwork, VoteRelay>::value));

    SECTION("root accepts a valid relay from its direct child")
    {
        const auto root = make_tree_network(core, 0);
        VoteRelay relay(key,
                        make_valid_relay_certificate(
                            core, key, {1, 3, 4}),
                        &core);
        CHECK(admit_relay(config,
                          0,
                          root,
                          config.get_peer_id(1),
                          relay,
                          true));
    }

    SECTION("intermediate accepts a valid relay from its direct child")
    {
        const auto intermediate = make_tree_network(core, 1);
        VoteRelay relay(key,
                        make_valid_relay_certificate(core, key, {3}),
                        &core);
        CHECK(admit_relay(config,
                          0,
                          intermediate,
                          config.get_peer_id(3),
                          relay,
                          true));
    }

    SECTION("authenticated non-child relay is rejected")
    {
        const auto root = make_tree_network(core, 0);
        VoteRelay relay(key,
                        make_valid_relay_certificate(core, key, {5}),
                        &core);
        CHECK_FALSE(admit_relay(config,
                                0,
                                root,
                                config.get_peer_id(5),
                                relay,
                                true));
    }

    SECTION("malformed relay is rejected")
    {
        const auto root = make_tree_network(core, 0);
        VoteRelay relay(key,
                        make_invalid_relay_certificate(core, key),
                        &core);
        CHECK_FALSE(admit_relay(config,
                                0,
                                root,
                                config.get_peer_id(1),
                                relay,
                                false));
    }

    SECTION("exact tree mismatch is rejected")
    {
        const auto root = make_tree_network(core, 0);
        auto wrong_tree_key = key;
        wrong_tree_key.configuration.tree_id = 1;
        VoteRelay relay(wrong_tree_key,
                        make_valid_relay_certificate(core, key, {1}),
                        &core);
        CHECK_FALSE(admit_relay(config,
                                0,
                                root,
                                config.get_peer_id(1),
                                relay,
                                true));
    }
}

TEST_CASE("accepted direct vote is cryptographically verified once before mutation",
          "[rem-s02-01][vote][verification-count]")
{
    BlsTestCore core(7);
    const auto block_hash = make_digest(0x74);
    const auto proposal_key = make_test_proposal_key(
        block_hash, 0xd1, 0, 0);
    auto counts = std::make_shared<VerificationCounts>();
    auto key = make_bls_private_key(1);
    part_cert_bt part(
        new CountingPartCert(key, proposal_key, counts));
    Vote vote(1, proposal_key, std::move(part), &core);

    VerifiedAccumulator accumulator(core.get_config(), proposal_key);
    add_valid_signers(accumulator, core, {0}, proposal_key);
    accumulator.compute();
    const auto before = accumulator.get_sigs_n();

    counts->synchronous = 0;
    counts->worker = 0;

    // The handler performs cheap envelope admission, one worker verification,
    // and event-loop mutation through the non-public verified-add seam.
    REQUIRE(hotstuff::validate_authenticated_vote(
        core.get_config(), core.get_config().get_peer_id(1), vote));
    hotstuff::EventContext event_context;
    hotstuff::VeriPool verification_pool(event_context, 0);
    vote.cert->verify(core.get_config().get_pubkey(1), verification_pool);
    accumulator.add_after_worker_verification(
        core.get_config(), vote.voter, *vote.cert);

    CHECK(counts->synchronous + counts->worker == 1);
    CHECK(accumulator.get_sigs_n() == before + 1);
}
