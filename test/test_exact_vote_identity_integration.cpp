#include <algorithm>
#include <cctype>
#include <cstddef>
#include <cstdint>
#include <fstream>
#include <iterator>
#include <sstream>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/hotstuff.h"
#include "support/bls_fixtures.h"

#ifndef KAURI_PROJECT_SOURCE_DIR
#error "KAURI_PROJECT_SOURCE_DIR must name the repository root"
#endif

namespace
{

using hotstuff::DataStream;
using hotstuff::HotStuffCore;
using hotstuff::MsgRelay;
using hotstuff::MsgVote;
using hotstuff::PartCertBLSAgg;
using hotstuff::PartCertDummy;
using hotstuff::ProposalKey;
using hotstuff::QuorumCertAggBLS;
using hotstuff::QuorumCertDummy;
using hotstuff::ReplicaID;
using hotstuff::Vote;
using hotstuff::VoteRelay;
using hotstuff::bytearray_t;
using hotstuff::part_cert_bt;
using hotstuff::quorum_cert_bt;
using hotstuff::test::BlsTestCore;
using hotstuff::test::make_bls_private_key;
using hotstuff::test::make_digest;

constexpr std::size_t kOversizedTrailingBytes = 64 * 1024;

ProposalKey proposal_key()
{
    return ProposalKey{
        hotstuff::ConfigurationId{17, 3, make_digest(0xe1)},
        make_digest(0xe2)};
}

quorum_cert_bt make_quorum(BlsTestCore &core, const ProposalKey &key)
{
    quorum_cert_bt certificate(
        new QuorumCertAggBLS(core.get_config(), key));
    for (const ReplicaID signer : {ReplicaID{0}, ReplicaID{1}})
    {
        auto private_key = make_bls_private_key(signer);
        PartCertBLSAgg part(private_key, key);
        certificate->add_part(core.get_config(), signer, part);
    }
    certificate->compute();
    return certificate;
}

bytearray_t vote_wire(BlsTestCore &core)
{
    const auto key = proposal_key();
    auto private_key = make_bls_private_key(1);
    Vote vote(1,
              key,
              new PartCertBLSAgg(private_key, key),
              &core);
    MsgVote message(vote);
    return static_cast<bytearray_t>(message.serialized);
}

bytearray_t relay_wire(BlsTestCore &core)
{
    const auto key = proposal_key();
    VoteRelay relay(key, make_quorum(core, key), &core);
    MsgRelay message(relay);
    return static_cast<bytearray_t>(message.serialized);
}

std::size_t relay_bitmap_offset(const ProposalKey &key)
{
    DataStream envelope;
    envelope << key.configuration.epoch_number
             << key.configuration.tree_id
             << key.configuration.epoch_digest
             << key.block_hash;

    DataStream quorum_key;
    hotstuff::serialize_proposal_key(quorum_key, key);
    return envelope.size() + quorum_key.size();
}

bytearray_t relay_wire_with_signer_bit_count(BlsTestCore &core,
                                             std::uint32_t bit_count)
{
    const auto key = proposal_key();
    auto wire = relay_wire(core);
    const auto offset = relay_bitmap_offset(key);

    DataStream encoded_count;
    encoded_count << hotstuff::htole(bit_count);
    const auto count_wire = static_cast<bytearray_t>(encoded_count);
    if (offset + count_wire.size() > wire.size())
        throw std::invalid_argument(
            "relay wire does not contain the BLS signer bitmap length");
    std::copy(count_wire.begin(), count_wire.end(), wire.begin() + offset);
    return wire;
}

template<typename Message>
constexpr bool parse_returns_rejection_status()
{
    return std::is_same_v<
        decltype(std::declval<Message &>().postponed_parse(
            std::declval<HotStuffCore *>())),
        bool>;
}

template<typename Message>
constexpr bool parse_is_noexcept()
{
    return noexcept(std::declval<Message &>().postponed_parse(
        std::declval<HotStuffCore *>()));
}

struct ParseObservation
{
    bool accepted{false};
    bool escaped_exception{false};
};

template<typename Message>
ParseObservation observe_handler_parse(Message &message,
                                       HotStuffCore &core) noexcept
{
    try
    {
        if constexpr (parse_returns_rejection_status<Message>())
            return {message.postponed_parse(&core), false};
        else
        {
            message.postponed_parse(&core);
            return {true, false};
        }
    }
    catch (...)
    {
        return {false, true};
    }
}

std::string read_file(const std::string &path)
{
    std::ifstream input(path);
    REQUIRE(input.good());
    std::ostringstream contents;
    contents << input.rdbuf();
    return contents.str();
}

std::string function_body(const std::string &source,
                          const std::string &signature)
{
    const auto signature_pos = source.find(signature);
    REQUIRE(signature_pos != std::string::npos);
    const auto opening = source.find('{', signature_pos + signature.size());
    REQUIRE(opening != std::string::npos);

    std::size_t depth = 0;
    for (std::size_t index = opening; index < source.size(); ++index)
    {
        if (source[index] == '{')
            ++depth;
        else if (source[index] == '}' && --depth == 0)
            return source.substr(opening, index - opening + 1);
    }
    FAIL("unterminated function body for " << signature);
    return {};
}

std::string without_whitespace(const std::string &value)
{
    std::string normalized;
    normalized.reserve(value.size());
    std::copy_if(value.begin(), value.end(),
                 std::back_inserter(normalized),
                 [](unsigned char character) {
                     return std::isspace(character) == 0;
                 });
    return normalized;
}

bool handler_rejects_failed_parse_before_lookup(const std::string &body)
{
    const auto normalized = without_whitespace(body);
    const auto parse_gate = normalized.find(
        "if(!msg.postponed_parse(this))");
    const auto lookup = normalized.find("find_exact_runtime_tree(");
    return parse_gate != std::string::npos &&
           lookup != std::string::npos &&
           parse_gate < lookup;
}

bool relay_bitmap_is_bounded_before_deserialization(
    const std::string &body)
{
    const auto normalized = without_whitespace(body);
    const auto membership_preflight = normalized.find(
        "validate_relay_wire_bounds("
        "serialized,hsc->get_config().nreplicas)");
    const auto quorum_deserialization = normalized.find(
        "serialized>>vote");
    return membership_preflight != std::string::npos &&
           quorum_deserialization != std::string::npos &&
           membership_preflight < quorum_deserialization;
}

bool certificate_is_owned_while_deserializing(
    const std::string &body,
    const std::string &owner_type,
    const std::string &concrete_type,
    const std::string &variable)
{
    const auto normalized = without_whitespace(body);
    const auto ownership = normalized.find(
        owner_type + variable + "(new" + concrete_type + "());");
    const auto deserialization = normalized.find(
        "s>>*" + variable + ";");
    const auto successful_return = normalized.find(
        "return" + variable + ";");
    return ownership != std::string::npos &&
           deserialization != std::string::npos &&
           successful_return != std::string::npos &&
           ownership < deserialization &&
           deserialization < successful_return;
}

class ThrowingPartCert final : public PartCertDummy
{
public:
    inline static std::size_t live_instances = 0;

    ThrowingPartCert()
    {
        ++live_instances;
    }

    ~ThrowingPartCert() override
    {
        --live_instances;
    }

    void unserialize(DataStream &stream) override
    {
        std::uint8_t required_byte;
        stream >> required_byte;
    }
};

class ThrowingQuorumCert final : public QuorumCertDummy
{
public:
    inline static std::size_t live_instances = 0;

    ThrowingQuorumCert()
    {
        ++live_instances;
    }

    ~ThrowingQuorumCert() override
    {
        --live_instances;
    }

    void unserialize(DataStream &stream) override
    {
        std::uint8_t required_byte;
        stream >> required_byte;
    }
};

template<typename CertificateBox, typename CertificateType>
CertificateBox parse_owned_certificate(DataStream &stream)
{
    // HotStuffBase construction starts network services. Keep this harness
    // side-effect free while exercising the same production certificate box
    // aliases; the source-wiring test below binds this order to HotStuff.
    CertificateBox certificate(new CertificateType());
    stream >> *certificate;
    return certificate;
}

template<typename Message>
void require_normal_rejection(Message &message, BlsTestCore &core)
{
    const auto observation = observe_handler_parse(message, core);
    CHECK_FALSE(observation.escaped_exception);
    CHECK_FALSE(observation.accepted);
}

} // namespace

TEST_CASE("vote and relay parse seams report rejection without throwing",
          "[rem-a06-02][exact-vote][handler][parse][intentional-red]")
{
    CHECK(parse_returns_rejection_status<MsgVote>());
    CHECK(parse_returns_rejection_status<MsgRelay>());
    CHECK(parse_is_noexcept<MsgVote>());
    CHECK(parse_is_noexcept<MsgRelay>());

    BlsTestCore core(4);
    auto direct_wire = vote_wire(core);
    MsgVote direct{DataStream(std::move(direct_wire))};
    const auto direct_result = observe_handler_parse(direct, core);
    CHECK_FALSE(direct_result.escaped_exception);
    CHECK(direct_result.accepted);

    auto aggregate_wire = relay_wire(core);
    MsgRelay aggregate{DataStream(std::move(aggregate_wire))};
    const auto aggregate_result = observe_handler_parse(aggregate, core);
    CHECK_FALSE(aggregate_result.escaped_exception);
    CHECK(aggregate_result.accepted);
}

TEST_CASE("malformed enlarged vote wire is a normal handler rejection",
          "[rem-a06-02][exact-vote][handler][parse][vote][intentional-red]")
{
    BlsTestCore core(4);

    SECTION("truncated exact epoch digest")
    {
        auto wire = vote_wire(core);
        wire.resize(sizeof(ReplicaID) + sizeof(std::uint32_t) * 2 + 16);
        MsgVote message{DataStream(std::move(wire))};
        require_normal_rejection(message, core);
    }

    SECTION("trailing input")
    {
        auto wire = vote_wire(core);
        wire.push_back(0xff);
        MsgVote message{DataStream(std::move(wire))};
        require_normal_rejection(message, core);
    }

    SECTION("oversized trailing input")
    {
        auto wire = vote_wire(core);
        wire.insert(wire.end(), kOversizedTrailingBytes, 0xff);
        MsgVote message{DataStream(std::move(wire))};
        require_normal_rejection(message, core);
    }
}

TEST_CASE("malformed enlarged relay wire is a normal handler rejection",
          "[rem-a06-02][exact-vote][handler][parse][relay][intentional-red]")
{
    BlsTestCore core(4);

    SECTION("truncated exact epoch digest")
    {
        auto wire = relay_wire(core);
        wire.resize(sizeof(std::uint32_t) * 2 + 16);
        MsgRelay message{DataStream(std::move(wire))};
        require_normal_rejection(message, core);
    }

    SECTION("trailing input")
    {
        auto wire = relay_wire(core);
        wire.push_back(0xff);
        MsgRelay message{DataStream(std::move(wire))};
        require_normal_rejection(message, core);
    }

    SECTION("oversized trailing input")
    {
        auto wire = relay_wire(core);
        wire.insert(wire.end(), kOversizedTrailingBytes, 0xff);
        MsgRelay message{DataStream(std::move(wire))};
        require_normal_rejection(message, core);
    }

    SECTION("BLS signer bitmap exceeds exact membership")
    {
        const auto forged_bit_count =
            static_cast<std::uint32_t>(core.get_config().nreplicas + 1);
        auto wire = relay_wire_with_signer_bit_count(
            core, forged_bit_count);
        MsgRelay message{DataStream(std::move(wire))};
        require_normal_rejection(message, core);
    }
}

TEST_CASE("real vote handlers gate malformed parse results",
          "[rem-a06-02][exact-vote][handler][parse][integration]"
          "[intentional-red]")
{
    const std::string root = KAURI_PROJECT_SOURCE_DIR;
    const auto source = read_file(root + "/src/hotstuff.cpp");

    const auto vote_handler = function_body(
        source, "void HotStuffBase::vote_handler");
    INFO("vote_handler must stop on a normal parse rejection before exact "
         "tree lookup, crypto, delivery, or mutation");
    CHECK(handler_rejects_failed_parse_before_lookup(vote_handler));

    const auto relay_handler = function_body(
        source, "void HotStuffBase::vote_relay_handler");
    INFO("vote_relay_handler must stop on a normal parse rejection before "
         "exact tree lookup, crypto, delivery, or mutation");
    CHECK(handler_rejects_failed_parse_before_lookup(relay_handler));

    const auto relay_parse = function_body(
        source, "MsgRelay::postponed_parse");
    INFO("MsgRelay must compare the encoded BLS signer-bit count with exact "
         "membership before QuorumCertAggBLS delegates to Bits::unserialize "
         "and allocates from the untrusted count");
    CHECK(relay_bitmap_is_bounded_before_deserialization(relay_parse));
}

TEST_CASE("certificate boxes destroy allocations when parsing throws",
          "[rem-a06-02][exact-vote][parser-ownership]")
{
    SECTION("partial certificate")
    {
        CHECK(ThrowingPartCert::live_instances == 0);
        DataStream truncated;
        REQUIRE_THROWS_AS(
            (parse_owned_certificate<part_cert_bt, ThrowingPartCert>(
                truncated)),
            std::ios_base::failure);
        CHECK(ThrowingPartCert::live_instances == 0);

        {
            DataStream complete(bytearray_t{0x01});
            auto certificate =
                parse_owned_certificate<part_cert_bt, ThrowingPartCert>(
                    complete);
            CHECK(certificate != nullptr);
            CHECK(ThrowingPartCert::live_instances == 1);
        }
        CHECK(ThrowingPartCert::live_instances == 0);
    }

    SECTION("quorum certificate")
    {
        CHECK(ThrowingQuorumCert::live_instances == 0);
        DataStream truncated;
        REQUIRE_THROWS_AS(
            (parse_owned_certificate<quorum_cert_bt, ThrowingQuorumCert>(
                truncated)),
            std::ios_base::failure);
        CHECK(ThrowingQuorumCert::live_instances == 0);

        {
            DataStream complete(bytearray_t{0x01});
            auto certificate =
                parse_owned_certificate<quorum_cert_bt,
                                        ThrowingQuorumCert>(complete);
            CHECK(certificate != nullptr);
            CHECK(ThrowingQuorumCert::live_instances == 1);
        }
        CHECK(ThrowingQuorumCert::live_instances == 0);
    }
}

TEST_CASE("HotStuff certificate parsers own allocations before parsing",
          "[rem-a06-02][exact-vote][parser-ownership][integration]"
          "[intentional-red]")
{
    const std::string root = KAURI_PROJECT_SOURCE_DIR;
    const auto header = read_file(root + "/include/hotstuff/hotstuff.h");

    const auto part_parser = function_body(
        header,
        "part_cert_bt parse_part_cert(DataStream &s) override");
    INFO("parse_part_cert must put the new PartCertType in its owning box "
         "before operator>> can throw, and return it only after parsing "
         "succeeds");
    CHECK(certificate_is_owned_while_deserializing(
        part_parser, "part_cert_bt", "PartCertType", "pc"));

    const auto quorum_parser = function_body(
        header,
        "quorum_cert_bt parse_quorum_cert(DataStream &s) override");
    INFO("parse_quorum_cert must put the new QuorumCertType in its owning "
         "box before operator>> can throw, and return it only after parsing "
         "succeeds");
    CHECK(certificate_is_owned_while_deserializing(
        quorum_parser, "quorum_cert_bt", "QuorumCertType", "qc"));
}
