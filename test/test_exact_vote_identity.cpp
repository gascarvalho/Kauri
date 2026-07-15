#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/hotstuff.h"
#include "support/bls_fixtures.h"
#include "support/fixtures.h"

#if __has_include("hotstuff/vote_identity.h")
#include "hotstuff/vote_identity.h"
#define KAURI_HAS_EXACT_VOTE_IDENTITY_CONTRACT 1
#else
#define KAURI_HAS_EXACT_VOTE_IDENTITY_CONTRACT 0
#endif

#if !KAURI_HAS_EXACT_VOTE_IDENTITY_CONTRACT

TEST_CASE("votes and relays require an exact cryptographic identity contract",
          "[rem-a06-02][exact-vote][intentional-red]")
{
    FAIL("missing hotstuff/vote_identity.h: Vote, VoteRelay, part certificates, "
         "and quorum certificates still authenticate only a bare block hash");
}

#else

using hotstuff::ConfigurationId;
using hotstuff::MsgRelay;
using hotstuff::MsgVote;
using hotstuff::PartCert;
using hotstuff::PartCertBLS;
using hotstuff::PartCertBLSAgg;
using hotstuff::PartCertDummy;
using hotstuff::PartCertSecp256k1;
using hotstuff::ProposalKey;
using hotstuff::PrivKey;
using hotstuff::PrivKeyBLS;
using hotstuff::PrivKeyDummy;
using hotstuff::PrivKeySecp256k1;
using hotstuff::PubKey;
using hotstuff::QuorumCert;
using hotstuff::QuorumCertAggBLS;
using hotstuff::QuorumCertDummy;
using hotstuff::QuorumCertSecp256k1;
using hotstuff::ReplicaID;
using hotstuff::VeriPool;
using hotstuff::Vote;
using hotstuff::VoteRelay;
using hotstuff::bytearray_t;
using hotstuff::part_cert_bt;
using hotstuff::privkey_bt;
using hotstuff::pubkey_bt;
using hotstuff::promise_t;
using hotstuff::quorum_cert_bt;
using hotstuff::test::BlsTestCore;
using hotstuff::test::make_bls_private_key;
using hotstuff::test::make_digest;

namespace
{

ProposalKey proposal_key(std::uint32_t epoch,
                         std::uint32_t tree,
                         std::uint8_t epoch_marker,
                         const hotstuff::uint256_t &block_hash)
{
    return ProposalKey{
        ConfigurationId{epoch, tree, make_digest(epoch_marker)}, block_hash};
}

void serialize_wire_key(hotstuff::DataStream &stream,
                        const ProposalKey &key)
{
    stream << key.configuration.epoch_number
           << key.configuration.tree_id
           << key.configuration.epoch_digest
           << key.block_hash;
}

template<typename Certificate>
hotstuff::DataStream relabel_certificate_wire(
    const Certificate &certificate,
    const ProposalKey &replacement,
    std::size_t key_offset = 0)
{
    hotstuff::DataStream original;
    original << certificate;
    auto wire = static_cast<bytearray_t>(original);

    hotstuff::DataStream replacement_stream;
    serialize_wire_key(replacement_stream, replacement);
    const auto replacement_wire =
        static_cast<bytearray_t>(replacement_stream);
    if (key_offset + replacement_wire.size() > wire.size())
        throw std::invalid_argument(
            "certificate wire does not contain a complete proposal key");
    std::copy(replacement_wire.begin(), replacement_wire.end(),
              wire.begin() + key_offset);
    return hotstuff::DataStream(std::move(wire));
}

bytearray_t secp_private_key_bytes(ReplicaID replica_id)
{
    bytearray_t raw(32, 0);
    const std::uint32_t scalar =
        static_cast<std::uint32_t>(replica_id) + 1;
    raw[28] = static_cast<std::uint8_t>(scalar >> 24);
    raw[29] = static_cast<std::uint8_t>(scalar >> 16);
    raw[30] = static_cast<std::uint8_t>(scalar >> 8);
    raw[31] = static_cast<std::uint8_t>(scalar);
    return raw;
}

template<typename PrivateKey>
privkey_bt make_scheme_private_key(ReplicaID replica_id);

template<>
privkey_bt make_scheme_private_key<PrivKeyDummy>(ReplicaID)
{
    return new PrivKeyDummy();
}

template<>
privkey_bt make_scheme_private_key<PrivKeySecp256k1>(
    ReplicaID replica_id)
{
    return new PrivKeySecp256k1(secp_private_key_bytes(replica_id));
}

template<>
privkey_bt make_scheme_private_key<PrivKeyBLS>(ReplicaID replica_id)
{
    return new PrivKeyBLS(
        hotstuff::test::make_bls_private_key_bytes(replica_id));
}

template<typename PrivateKey>
pubkey_bt make_scheme_public_key(ReplicaID replica_id)
{
    auto private_key = make_scheme_private_key<PrivateKey>(replica_id);
    return private_key->get_pubkey();
}

template<typename PrivateKey,
         typename PartCertificate,
         typename QuorumCertificate>
class ExactSchemeCore final : public hotstuff::HotStuffCore
{
public:
    explicit ExactSchemeCore(std::size_t replica_count)
        : HotStuffCore(0, make_scheme_private_key<PrivateKey>(0))
    {
        for (std::size_t index = 0; index < replica_count; ++index)
        {
            const auto replica_id = static_cast<ReplicaID>(index);
            const hotstuff::NetAddr address(
                static_cast<std::uint32_t>(0x7f000001),
                static_cast<std::uint16_t>(12000 + replica_id));
            add_replica(replica_id,
                        hotstuff::PeerId(address),
                        make_scheme_public_key<PrivateKey>(replica_id));
        }
        config.nmajority = replica_count == 1 ? 1 : 2;
    }

    part_cert_bt make_part(ReplicaID signer, const ProposalKey &key)
    {
        auto private_key = make_scheme_private_key<PrivateKey>(signer);
        return new PartCertificate(
            static_cast<const PrivateKey &>(*private_key), key);
    }

    part_cert_bt create_part_cert(const PrivKey &private_key,
                                  const ProposalKey &key) override
    {
        return new PartCertificate(
            static_cast<const PrivateKey &>(private_key), key);
    }

    part_cert_bt parse_part_cert(hotstuff::DataStream &stream) override
    {
        auto *part = new PartCertificate();
        stream >> *part;
        return part;
    }

    quorum_cert_bt create_quorum_cert(const ProposalKey &key) override
    {
        return new QuorumCertificate(get_config(), key);
    }

    quorum_cert_bt parse_quorum_cert(hotstuff::DataStream &stream) override
    {
        auto *quorum = new QuorumCertificate();
        stream >> *quorum;
        return quorum;
    }

protected:
    void do_decide(hotstuff::Finality &&) override {}
    void do_consensus(const hotstuff::block_t &) override {}
    void do_broadcast_proposal(const hotstuff::Proposal &) override {}
    void do_vote(hotstuff::Proposal, const Vote &) override {}
    void start_proposal_timer(std::size_t,
                              std::size_t,
                              hotstuff::uint256_t,
                              double,
                              std::size_t) override {}
};

using DummySchemeCore = ExactSchemeCore<
    PrivKeyDummy, PartCertDummy, QuorumCertDummy>;
using SecpSchemeCore = ExactSchemeCore<
    PrivKeySecp256k1, PartCertSecp256k1, QuorumCertSecp256k1>;
using NonAggregateBlsSchemeCore = ExactSchemeCore<
    PrivKeyBLS, PartCertBLS, QuorumCertDummy>;

struct VerificationCounts
{
    std::size_t synchronous{0};
    std::size_t worker{0};
};

class CountingPartCert final : public PartCertBLSAgg
{
public:
    explicit CountingPartCert(std::shared_ptr<VerificationCounts> counts)
        : counts_(std::move(counts))
    {}

    CountingPartCert(const CountingPartCert &) = default;

    bool verify(const PubKey &public_key) override
    {
        ++counts_->synchronous;
        return PartCertBLSAgg::verify(public_key);
    }

    promise_t verify(const PubKey &public_key, VeriPool &) override
    {
        ++counts_->worker;
        const bool valid = PartCertBLSAgg::verify(public_key);
        return promise_t([valid](promise_t &promise) {
            promise.resolve(valid);
        });
    }

    CountingPartCert *clone() override
    {
        return new CountingPartCert(*this);
    }

private:
    std::shared_ptr<VerificationCounts> counts_;
};

class CountingQuorumCert final : public QuorumCertAggBLS
{
public:
    explicit CountingQuorumCert(std::shared_ptr<VerificationCounts> counts)
        : counts_(std::move(counts))
    {}

    CountingQuorumCert(const CountingQuorumCert &) = default;

    bool verify(const hotstuff::ReplicaConfig &config) override
    {
        ++counts_->synchronous;
        return QuorumCertAggBLS::verify(config);
    }

    promise_t verify(const hotstuff::ReplicaConfig &config,
                     VeriPool &) override
    {
        ++counts_->worker;
        const bool valid = QuorumCertAggBLS::verify(config);
        return promise_t([valid](promise_t &promise) {
            promise.resolve(valid);
        });
    }

    CountingQuorumCert *clone() override
    {
        return new CountingQuorumCert(*this);
    }

private:
    std::shared_ptr<VerificationCounts> counts_;
};

part_cert_bt make_part(ReplicaID signer, const ProposalKey &key)
{
    auto private_key = make_bls_private_key(signer);
    return new PartCertBLSAgg(private_key, key);
}

quorum_cert_bt make_quorum(BlsTestCore &core,
                           const ProposalKey &key,
                           const std::vector<ReplicaID> &signers)
{
    quorum_cert_bt certificate(
        new QuorumCertAggBLS(core.get_config(), key));
    for (const auto signer : signers)
    {
        auto part = make_part(signer, key);
        certificate->add_part(core.get_config(), signer, *part);
    }
    certificate->compute();
    return certificate;
}

template<typename Core>
Vote round_trip_vote(Core &core, const Vote &vote)
{
    MsgVote message(vote);
    message.postponed_parse(&core);
    return std::move(message.vote);
}

template<typename Core>
VoteRelay round_trip_relay(Core &core, const VoteRelay &relay)
{
    MsgRelay message(relay);
    message.postponed_parse(&core);
    return std::move(message.vote);
}

std::string vote_wire_hex(const Vote &vote)
{
    MsgVote message(vote);
    return message.serialized.get_hex();
}

std::string relay_wire_hex(const VoteRelay &relay)
{
    MsgRelay message(relay);
    return message.serialized.get_hex();
}

bool resolved_boolean(promise_t promise)
{
    bool settled = false;
    bool value = false;
    promise.then([&settled, &value](bool result) {
        settled = true;
        value = result;
    });
    REQUIRE(settled);
    return value;
}

std::string serialized_hex(const QuorumCert &certificate)
{
    hotstuff::DataStream stream;
    stream << certificate;
    return stream.get_hex();
}

template<typename Core, typename PartCertificate>
void exercise_part_certificate_scheme(const char *scheme)
{
    INFO("scheme=" << scheme);
    Core core(4);
    const auto block_hash = make_digest(0x95);
    const auto key_a = proposal_key(10, 2, 0xc1, block_hash);
    const auto key_b = proposal_key(10, 2, 0xd2, block_hash);
    const auto &public_key = core.get_config().get_pubkey(1);
    const auto &peer = core.get_config().get_peer_id(1);

    auto original = core.make_part(1, key_a);
    REQUIRE(original != nullptr);
    CHECK(original->get_proposal_key() == key_a);
    CHECK(original->get_obj_hash() == block_hash);
    CHECK(original->verify(public_key));

    part_cert_bt cloned(original->clone());
    REQUIRE(cloned != nullptr);
    CHECK(cloned->get_proposal_key() == key_a);
    CHECK(cloned->get_obj_hash() == block_hash);
    CHECK(cloned->verify(public_key));

    hotstuff::DataStream certificate_wire;
    certificate_wire << *original;
    PartCertificate parsed_certificate;
    certificate_wire >> parsed_certificate;
    CHECK(parsed_certificate.get_proposal_key() == key_a);
    CHECK(parsed_certificate.get_obj_hash() == block_hash);
    CHECK(parsed_certificate.verify(public_key));

    Vote vote(1, key_a, original->clone(), &core);
    Vote copied_vote(vote);
    CHECK(copied_vote.key() == key_a);
    REQUIRE(copied_vote.cert != nullptr);
    CHECK(copied_vote.cert->get_proposal_key() == key_a);
    CHECK(copied_vote.cert->verify(public_key));

    const auto parsed_vote = round_trip_vote(core, vote);
    CHECK(parsed_vote.key() == key_a);
    REQUIRE(parsed_vote.cert != nullptr);
    CHECK(parsed_vote.cert->get_proposal_key() == key_a);
    CHECK(parsed_vote.cert->get_obj_hash() == block_hash);
    CHECK(parsed_vote.cert->verify(public_key));

    Vote envelope_relabelled(1, key_b, original->clone(), &core);
    CHECK_FALSE(hotstuff::validate_authenticated_vote(
        core.get_config(), peer, envelope_relabelled));

    auto relabelled_wire = relabel_certificate_wire(*original, key_b);
    auto *relabelled_certificate = new PartCertificate();
    relabelled_certificate->unserialize(relabelled_wire);
    Vote fully_relabelled(1, key_b, relabelled_certificate, &core);
    REQUIRE(hotstuff::validate_authenticated_vote(
        core.get_config(), peer, fully_relabelled));
    CHECK_FALSE(fully_relabelled.cert->verify(public_key));
}

template<typename Core,
         typename PartCertificate,
         typename QuorumCertificate>
void exercise_quorum_certificate_scheme(const char *scheme,
                                        std::size_t key_offset)
{
    INFO("scheme=" << scheme);
    Core core(4);
    const auto block_hash = make_digest(0x96);
    const auto key_a = proposal_key(11, 4, 0xc3, block_hash);
    const auto key_b = proposal_key(11, 4, 0xd4, block_hash);

    QuorumCertificate accumulator(core.get_config(), key_a);
    auto part_a0 = core.make_part(0, key_a);
    auto part_a1 = core.make_part(1, key_a);
    accumulator.add_part(core.get_config(), 0, *part_a0);
    accumulator.add_part(core.get_config(), 1, *part_a1);
    accumulator.compute();
    REQUIRE(accumulator.verify(core.get_config()));

    quorum_cert_bt cloned(accumulator.clone());
    REQUIRE(cloned != nullptr);
    CHECK(cloned->get_proposal_key() == key_a);
    CHECK(cloned->get_obj_hash() == block_hash);
    CHECK(cloned->verify(core.get_config()));

    hotstuff::DataStream certificate_wire;
    certificate_wire << accumulator;
    QuorumCertificate parsed_certificate;
    certificate_wire >> parsed_certificate;
    CHECK(parsed_certificate.get_proposal_key() == key_a);
    CHECK(parsed_certificate.get_obj_hash() == block_hash);
    CHECK(parsed_certificate.verify(core.get_config()));

    VoteRelay relay(key_a, accumulator.clone(), &core);
    VoteRelay copied_relay(relay);
    CHECK(copied_relay.key() == key_a);
    REQUIRE(copied_relay.cert != nullptr);
    CHECK(copied_relay.cert->get_proposal_key() == key_a);
    CHECK(copied_relay.cert->verify(core.get_config()));

    const auto parsed_relay = round_trip_relay(core, relay);
    CHECK(parsed_relay.key() == key_a);
    REQUIRE(parsed_relay.cert != nullptr);
    CHECK(parsed_relay.cert->get_proposal_key() == key_a);
    CHECK(parsed_relay.cert->get_obj_hash() == block_hash);
    CHECK(parsed_relay.cert->verify(core.get_config()));

    VoteRelay envelope_relabelled(key_b, accumulator.clone(), &core);
    CHECK_FALSE(hotstuff::validate_relay_envelope(
        core.get_config(), envelope_relabelled));

    auto relabelled_wire =
        relabel_certificate_wire(accumulator, key_b, key_offset);
    auto *relabelled_certificate = new QuorumCertificate();
    relabelled_certificate->unserialize(relabelled_wire);
    VoteRelay fully_relabelled(key_b, relabelled_certificate, &core);
    REQUIRE(hotstuff::validate_relay_envelope(
        core.get_config(), fully_relabelled));
    CHECK_FALSE(fully_relabelled.cert->verify(core.get_config()));

    const auto before_part_count = accumulator.get_sigs_n();
    const auto before_part_bytes = serialized_hex(accumulator);
    auto wrong_key_part = core.make_part(2, key_b);
    REQUIRE_THROWS_AS(
        accumulator.add_part(core.get_config(), 2, *wrong_key_part),
        std::invalid_argument);
    CHECK(accumulator.get_sigs_n() == before_part_count);
    CHECK(serialized_hex(accumulator) == before_part_bytes);

    QuorumCertificate wrong_key_quorum(core.get_config(), key_b);
    auto part_b2 = core.make_part(2, key_b);
    auto part_b3 = core.make_part(3, key_b);
    wrong_key_quorum.add_part(core.get_config(), 2, *part_b2);
    wrong_key_quorum.add_part(core.get_config(), 3, *part_b3);
    wrong_key_quorum.compute();
    REQUIRE(wrong_key_quorum.verify(core.get_config()));

    const auto before_merge_count = accumulator.get_sigs_n();
    const auto before_merge_bytes = serialized_hex(accumulator);
    REQUIRE_THROWS_AS(
        accumulator.merge_quorum(core.get_config(), wrong_key_quorum),
        std::invalid_argument);
    CHECK(accumulator.get_sigs_n() == before_merge_count);
    CHECK(serialized_hex(accumulator) == before_merge_bytes);
    CHECK(accumulator.get_proposal_key() == key_a);
    CHECK(accumulator.get_obj_hash() == block_hash);
}

template<typename Core, typename QuorumCertificate>
void exercise_quorum_signer_enumeration(const char *scheme)
{
    INFO("scheme=" << scheme);
    Core core(4);
    const auto key = proposal_key(
        14, 6, 0xe1, make_digest(0x99));
    QuorumCertificate accumulator(core.get_config(), key);

    INFO("a fresh accumulator must not invent an anonymous signer");
    CHECK(accumulator.get_sigs_n() == 0);
    CHECK(accumulator.get_signers().empty());

    for (const auto signer : std::vector<ReplicaID>{3, 1})
    {
        auto part = core.make_part(signer, key);
        accumulator.add_part(core.get_config(), signer, *part);
    }

    const std::vector<ReplicaID> expected_signers{1, 3};
    CHECK(accumulator.get_signers() == expected_signers);
    CHECK(accumulator.get_signers().size() == accumulator.get_sigs_n());

    const auto before_duplicate_count = accumulator.get_sigs_n();
    const auto before_duplicate_signers = accumulator.get_signers();
    auto duplicate = core.make_part(1, key);
    try
    {
        accumulator.add_part(core.get_config(), 1, *duplicate);
    }
    catch (const std::invalid_argument &)
    {
        // Explicit rejection and an idempotent no-op are both safe.
    }
    CHECK(accumulator.get_sigs_n() == before_duplicate_count);
    CHECK(accumulator.get_signers() == before_duplicate_signers);

    accumulator.compute();
    REQUIRE(accumulator.verify(core.get_config()));

    quorum_cert_bt cloned(accumulator.clone());
    REQUIRE(cloned != nullptr);
    CHECK(cloned->get_signers() == expected_signers);
    CHECK(cloned->get_signers().size() == cloned->get_sigs_n());

    hotstuff::DataStream wire;
    wire << accumulator;
    QuorumCertificate parsed;
    wire >> parsed;
    CHECK(parsed.get_signers() == expected_signers);
    CHECK(parsed.get_signers().size() == parsed.get_sigs_n());
    REQUIRE(parsed.verify(core.get_config()));

    QuorumCertificate overlapping(core.get_config(), key);
    for (const auto signer : expected_signers)
    {
        auto part = core.make_part(signer, key);
        overlapping.add_part(core.get_config(), signer, *part);
    }
    overlapping.compute();
    REQUIRE(overlapping.verify(core.get_config()));

    const auto before_overlap_count = accumulator.get_sigs_n();
    const auto before_overlap_signers = accumulator.get_signers();
    const auto before_overlap_bytes = serialized_hex(accumulator);
    try
    {
        accumulator.merge_quorum(core.get_config(), overlapping);
    }
    catch (const std::invalid_argument &)
    {
        // Rejecting overlap is also acceptable if mutation is atomic.
    }
    CHECK(accumulator.get_sigs_n() == before_overlap_count);
    CHECK(accumulator.get_signers() == before_overlap_signers);
    CHECK(serialized_hex(accumulator) == before_overlap_bytes);
}

hotstuff::DataStream dummy_wire_with_wrong_signer_count(
    DummySchemeCore &core,
    const ProposalKey &key)
{
    QuorumCertDummy certificate(core.get_config(), key);
    for (const auto signer : std::vector<ReplicaID>{1, 3})
    {
        auto part = core.make_part(signer, key);
        certificate.add_part(core.get_config(), signer, *part);
    }

    hotstuff::DataStream valid_wire;
    valid_wire << certificate;
    auto malformed_bytes = static_cast<bytearray_t>(valid_wire);

    hotstuff::DataStream signer_suffix;
    const auto signers = certificate.get_signers();
    signer_suffix << hotstuff::htole(
        static_cast<std::uint32_t>(signers.size()));
    for (const auto signer : signers)
        signer_suffix << signer;

    hotstuff::DataStream malformed_count;
    malformed_count << static_cast<std::size_t>(signers.size() + 7);
    const auto count_bytes = static_cast<bytearray_t>(malformed_count);
    const auto count_offset = malformed_bytes.size() -
                              signer_suffix.size() - count_bytes.size();
    std::copy(count_bytes.begin(), count_bytes.end(),
              malformed_bytes.begin() + count_offset);
    return hotstuff::DataStream(std::move(malformed_bytes));
}

} // namespace

TEST_CASE("exact vote authentication has fixed canonical bytes and digest",
          "[rem-a06-02][exact-vote][canonical]")
{
    const auto key = proposal_key(1, 2, 0xaa, make_digest(0xbb));
    const auto canonical = hotstuff::canonical_serialize_exact_vote(key);

    const std::string expected_bytes =
        "4b415552495f45584143545f564f54455f5631"
        "00000001"
        "00000002"
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
    const std::string expected_digest =
        "0cdad3a39fe016b89e43b7d098ef0fed"
        "85bc78f57e94f92b30d5a239e41021b2";

    CHECK(canonical.size() == 91);
    CHECK(hotstuff::DataStream(canonical).get_hex() == expected_bytes);
    CHECK(hotstuff::exact_vote_authentication_digest(key).to_hex() ==
          expected_digest);
}

TEST_CASE("Vote and VoteRelay round trip the complete exact proposal key",
          "[rem-a06-02][exact-vote][wire]")
{
    BlsTestCore core(7);
    const auto block_hash = make_digest(0x91);
    const auto key_a = proposal_key(4, 9, 0xa1, block_hash);
    const auto key_b = proposal_key(4, 9, 0xb2, block_hash);

    Vote vote_a(1, key_a, make_part(1, key_a), &core);
    Vote vote_b(1, key_b, make_part(1, key_b), &core);
    const auto vote_a_bytes = vote_wire_hex(vote_a);
    const auto vote_b_bytes = vote_wire_hex(vote_b);
    const auto parsed_vote = round_trip_vote(core, vote_a);

    CHECK(parsed_vote.key() == key_a);
    CHECK(parsed_vote.configuration() == key_a.configuration);
    REQUIRE(parsed_vote.cert != nullptr);
    CHECK(parsed_vote.cert->get_proposal_key() == key_a);
    CHECK(parsed_vote.cert->get_obj_hash() == block_hash);
    CHECK(vote_a_bytes != vote_b_bytes);

    VoteRelay relay_a(key_a, make_quorum(core, key_a, {1, 3, 4}), &core);
    VoteRelay relay_b(key_b, make_quorum(core, key_b, {1, 3, 4}), &core);
    const auto relay_a_bytes = relay_wire_hex(relay_a);
    const auto relay_b_bytes = relay_wire_hex(relay_b);
    const auto parsed_relay = round_trip_relay(core, relay_a);

    CHECK(parsed_relay.key() == key_a);
    CHECK(parsed_relay.configuration() == key_a.configuration);
    REQUIRE(parsed_relay.cert != nullptr);
    CHECK(parsed_relay.cert->get_proposal_key() == key_a);
    CHECK(parsed_relay.cert->get_obj_hash() == block_hash);
    CHECK(relay_a_bytes != relay_b_bytes);
}

TEST_CASE("all configured part-certificate schemes preserve exact identity",
          "[rem-a06-02][exact-vote][scheme-parity][wire][copy][relabel]")
{
    SECTION("Dummy")
    {
        exercise_part_certificate_scheme<
            DummySchemeCore, PartCertDummy>("Dummy");
    }
    SECTION("Secp256k1")
    {
        exercise_part_certificate_scheme<
            SecpSchemeCore, PartCertSecp256k1>("Secp256k1");
    }
    SECTION("non-aggregate BLS")
    {
        exercise_part_certificate_scheme<
            NonAggregateBlsSchemeCore, PartCertBLS>("non-aggregate BLS");
    }
}

TEST_CASE("non-aggregate quorum schemes preserve and isolate exact identity",
          "[rem-a06-02][exact-vote][scheme-parity][quorum][atomic]")
{
    SECTION("Dummy")
    {
        exercise_quorum_certificate_scheme<
            DummySchemeCore, PartCertDummy, QuorumCertDummy>(
                "Dummy", sizeof(std::uint32_t));
    }
    SECTION("Secp256k1")
    {
        exercise_quorum_certificate_scheme<
            SecpSchemeCore, PartCertSecp256k1, QuorumCertSecp256k1>(
                "Secp256k1", 0);
    }
}

TEST_CASE("all quorum schemes enumerate exact signer identity",
          "[rem-a06-02][exact-vote][scheme-parity][signers][intentional-red]")
{
    SECTION("Dummy")
    {
        exercise_quorum_signer_enumeration<
            DummySchemeCore, QuorumCertDummy>("Dummy");
    }
    SECTION("Secp256k1")
    {
        exercise_quorum_signer_enumeration<
            SecpSchemeCore, QuorumCertSecp256k1>("Secp256k1");
    }
    SECTION("aggregate BLS")
    {
        exercise_quorum_signer_enumeration<
            BlsTestCore, QuorumCertAggBLS>("aggregate BLS");
    }
}

TEST_CASE("Dummy quorum wire rejects signer-count divergence",
          "[rem-a06-02][exact-vote][dummy][wire][malformed][intentional-red]")
{
    DummySchemeCore core(4);
    const auto key = proposal_key(
        15, 7, 0xe2, make_digest(0x9a));
    auto malformed = dummy_wire_with_wrong_signer_count(core, key);

    QuorumCertDummy parsed;
    REQUIRE_THROWS_AS(malformed >> parsed, std::invalid_argument);
}

TEST_CASE("reused Secp256k1 quorum parser replaces all signer state",
          "[rem-a06-02][exact-vote][secp256k1][wire][reuse][intentional-red]")
{
    SecpSchemeCore core(4);
    const auto block_hash = make_digest(0x97);
    const auto key_a = proposal_key(12, 5, 0xc5, block_hash);
    const auto key_b = proposal_key(12, 5, 0xd6, block_hash);

    QuorumCertSecp256k1 first(core.get_config(), key_a);
    auto first_part_0 = core.make_part(0, key_a);
    auto first_part_1 = core.make_part(1, key_a);
    first.add_part(core.get_config(), 0, *first_part_0);
    first.add_part(core.get_config(), 1, *first_part_1);
    REQUIRE(first.verify(core.get_config()));

    QuorumCertSecp256k1 second(core.get_config(), key_b);
    auto second_part_2 = core.make_part(2, key_b);
    second.add_part(core.get_config(), 2, *second_part_2);
    REQUIRE(second.verify(core.get_config()));

    hotstuff::DataStream first_wire;
    first_wire << first;
    QuorumCertSecp256k1 reused;
    first_wire >> reused;
    REQUIRE(reused.get_sigs_n() == first.get_sigs_n());

    hotstuff::DataStream second_wire;
    second_wire << second;
    second_wire >> reused;

    CHECK(reused.get_proposal_key() == key_b);
    CHECK(reused.get_sigs_n() == second.get_sigs_n());
    CHECK(serialized_hex(reused) == serialized_hex(second));
    CHECK(reused.verify(core.get_config()));

    auto second_part_0 = core.make_part(0, key_b);
    reused.add_part(core.get_config(), 0, *second_part_0);
    CHECK(reused.get_sigs_n() == 2);
    CHECK(reused.verify(core.get_config()));
}

TEST_CASE("direct-vote relabelling cannot authenticate another exact key",
          "[rem-a06-02][exact-vote][direct][relabel]")
{
    BlsTestCore core(7);
    const auto block_hash = make_digest(0x92);
    const auto key_a = proposal_key(7, 3, 0xa3, block_hash);
    const auto key_b = proposal_key(7, 3, 0xb4, block_hash);
    const auto &peer = core.get_config().get_peer_id(1);

    SECTION("message-only relabel is rejected by the cheap envelope gate")
    {
        Vote relabelled(1, key_b, make_part(1, key_a), &core);
        CHECK_FALSE(hotstuff::validate_authenticated_vote(
            core.get_config(), peer, relabelled));
    }

    SECTION("relabelled message and certificate fail one worker verification")
    {
        auto original = make_part(1, key_a);
        auto counts = std::make_shared<VerificationCounts>();
        auto *certificate = new CountingPartCert(counts);
        auto relabelled_wire = relabel_certificate_wire(*original, key_b);
        certificate->unserialize(relabelled_wire);

        Vote relabelled(1, key_b, certificate, &core);
        REQUIRE(hotstuff::validate_authenticated_vote(
            core.get_config(), peer, relabelled));

        hotstuff::EventContext event_context;
        VeriPool verification_pool(event_context, 0);
        CHECK_FALSE(resolved_boolean(relabelled.cert->verify(
            core.get_config().get_pubkey(1), verification_pool)));
        CHECK(counts->synchronous == 0);
        CHECK(counts->worker == 1);
    }
}

TEST_CASE("aggregate relabelling cannot authenticate another exact key",
          "[rem-a06-02][exact-vote][aggregate][relabel]")
{
    BlsTestCore core(7);
    const auto block_hash = make_digest(0x93);
    const auto key_a = proposal_key(8, 5, 0xa5, block_hash);
    const auto key_b = proposal_key(8, 5, 0xb6, block_hash);

    SECTION("message-only relabel is rejected by the cheap envelope gate")
    {
        VoteRelay relabelled(
            key_b, make_quorum(core, key_a, {1, 3, 4}), &core);
        CHECK_FALSE(hotstuff::validate_relay_envelope(
            core.get_config(), relabelled));
    }

    SECTION("relabelled message and certificate fail one worker verification")
    {
        auto original = make_quorum(core, key_a, {1, 3, 4});
        auto counts = std::make_shared<VerificationCounts>();
        auto *certificate = new CountingQuorumCert(counts);
        auto relabelled_wire = relabel_certificate_wire(*original, key_b);
        certificate->unserialize(relabelled_wire);

        VoteRelay relabelled(key_b, certificate, &core);
        REQUIRE(hotstuff::validate_relay_envelope(
            core.get_config(), relabelled));

        hotstuff::EventContext event_context;
        VeriPool verification_pool(event_context, 0);
        CHECK_FALSE(resolved_boolean(relabelled.cert->verify(
            core.get_config(), verification_pool)));
        CHECK(counts->synchronous == 0);
        CHECK(counts->worker == 1);
    }
}

TEST_CASE("part and quorum accumulation reject different exact keys atomically",
          "[rem-a06-02][exact-vote][accumulator]")
{
    BlsTestCore core(7);
    const auto block_hash = make_digest(0x94);
    const auto key_a = proposal_key(9, 6, 0xa7, block_hash);
    const auto key_b = proposal_key(9, 6, 0xb8, block_hash);

    QuorumCertAggBLS accumulator(core.get_config(), key_a);
    auto local = make_part(0, key_a);
    accumulator.add_part(core.get_config(), 0, *local);
    accumulator.compute();
    REQUIRE(accumulator.verify(core.get_config()));

    const auto before_part_count = accumulator.get_sigs_n();
    const auto before_part_bytes = serialized_hex(accumulator);
    auto wrong_key_part = make_part(1, key_b);
    REQUIRE_THROWS_AS(
        accumulator.add_part(core.get_config(), 1, *wrong_key_part),
        std::invalid_argument);
    CHECK(accumulator.get_sigs_n() == before_part_count);
    CHECK(serialized_hex(accumulator) == before_part_bytes);

    auto wrong_key_quorum = make_quorum(core, key_b, {2, 3});
    const auto before_merge_count = accumulator.get_sigs_n();
    const auto before_merge_bytes = serialized_hex(accumulator);
    REQUIRE_THROWS_AS(
        accumulator.merge_quorum(core.get_config(), *wrong_key_quorum),
        std::invalid_argument);
    accumulator.compute();
    CHECK(accumulator.get_sigs_n() == before_merge_count);
    CHECK(serialized_hex(accumulator) == before_merge_bytes);
    CHECK(accumulator.get_proposal_key() == key_a);
    CHECK(accumulator.get_obj_hash() == block_hash);
}

#endif
