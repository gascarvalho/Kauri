#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <optional>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/evidence_lifecycle_wire.h"

namespace
{

using hotstuff::ConfigurationId;
using hotstuff::DataStream;
using hotstuff::MsgProposalLifecycleNotice;
using hotstuff::NormalProposalRuntimeInitialized;
using hotstuff::ProposalCommitted;
using hotstuff::ProposalConfigurationRetired;
using hotstuff::ProposalKey;
using hotstuff::ProposalLifecycleDecodeResult;
using hotstuff::ProposalLifecycleFact;
using hotstuff::ProposalLifecycleFactTag;
using hotstuff::ProposalLifecycleNotice;
using hotstuff::ProposalLifecycleWireError;
using hotstuff::ProposalLifecycleWireLimits;
using hotstuff::ProposalRetirementFloorAdvanced;
using hotstuff::ProposalRuntimeAborted;
using hotstuff::ReplicaID;
using hotstuff::bytearray_t;
using hotstuff::uint256_t;

static_assert(noexcept(hotstuff::decode_proposal_lifecycle_notice(
    std::declval<const bytearray_t &>(),
    std::declval<const ProposalLifecycleWireLimits &>())));

uint256_t digest(const std::string &label)
{
    return DataStream(label).get_hash();
}

ConfigurationId configuration(
    std::uint32_t epoch = 7,
    std::uint32_t tree = 3,
    const std::string &label = "lifecycle-epoch")
{
    return ConfigurationId{epoch, tree, digest(label)};
}

ProposalKey proposal(
    const std::string &label,
    ConfigurationId exact_configuration = configuration())
{
    return ProposalKey{
        std::move(exact_configuration), digest(label + "-block")};
}

template<typename Fact>
ProposalLifecycleNotice notice(
    Fact fact,
    ReplicaID source = 0,
    std::uint64_t sequence = 1)
{
    return ProposalLifecycleNotice{
        hotstuff::kProposalLifecycleNoticeSchemaVersion,
        source,
        sequence,
        ProposalLifecycleFact{std::move(fact)}};
}

ProposalLifecycleWireLimits limits()
{
    return ProposalLifecycleWireLimits{512};
}

bool same_notice(
    const ProposalLifecycleNotice &left,
    const ProposalLifecycleNotice &right)
{
    if (left.schema_version != right.schema_version ||
        left.source_replica_id != right.source_replica_id ||
        left.source_sequence != right.source_sequence ||
        left.fact.index() != right.fact.index())
    {
        return false;
    }
    switch (left.fact.index())
    {
    case 0:
        return std::get<NormalProposalRuntimeInitialized>(left.fact)
                   .proposal ==
               std::get<NormalProposalRuntimeInitialized>(right.fact)
                   .proposal;
    case 1:
        return std::get<ProposalRuntimeAborted>(left.fact).proposal ==
               std::get<ProposalRuntimeAborted>(right.fact).proposal;
    case 2:
        return std::get<ProposalCommitted>(left.fact).proposal ==
                   std::get<ProposalCommitted>(right.fact).proposal &&
               std::get<ProposalCommitted>(left.fact)
                       .evidence_sequence_fence ==
                   std::get<ProposalCommitted>(right.fact)
                       .evidence_sequence_fence;
    case 3:
        return std::get<ProposalConfigurationRetired>(left.fact)
                   .configuration ==
               std::get<ProposalConfigurationRetired>(right.fact)
                   .configuration;
    case 4:
        return std::get<ProposalRetirementFloorAdvanced>(left.fact)
                   .first_live_epoch ==
               std::get<ProposalRetirementFloorAdvanced>(right.fact)
                   .first_live_epoch;
    default:
        return false;
    }
}

template<typename UInt>
void write_big_endian(
    bytearray_t &bytes,
    std::size_t offset,
    UInt value)
{
    static_assert(std::is_unsigned<UInt>::value, "unsigned wire integer");
    REQUIRE(offset + sizeof(UInt) <= bytes.size());
    for (std::size_t index = 0; index < sizeof(UInt); ++index)
    {
        bytes[offset + index] = static_cast<std::uint8_t>(
            value >> ((sizeof(UInt) - index - 1) * 8));
    }
}

void zero_bytes(
    bytearray_t &bytes,
    std::size_t offset,
    std::size_t count)
{
    REQUIRE(offset + count <= bytes.size());
    std::fill(
        bytes.begin() + offset,
        bytes.begin() + offset + count,
        0);
}

void check_decode_error(
    const bytearray_t &payload,
    ProposalLifecycleWireError expected,
    ProposalLifecycleWireLimits configured_limits = limits())
{
    const auto result = hotstuff::decode_proposal_lifecycle_notice(
        payload, configured_limits);
    CHECK(result.error == expected);
    CHECK_FALSE(result.notice.has_value());
}

std::size_t domain_size()
{
    return hotstuff::proposal_lifecycle_notice_domain().size();
}

constexpr std::size_t kSchemaBytes = sizeof(std::uint32_t);
constexpr std::size_t kSourceBytes = sizeof(ReplicaID);
constexpr std::size_t kSequenceBytes = sizeof(std::uint64_t);
constexpr std::size_t kTagBytes = sizeof(std::uint8_t);
constexpr std::size_t kFlagsBytes = sizeof(std::uint8_t);
constexpr std::size_t kDigestBytes = 32;

std::size_t schema_offset()
{
    return domain_size();
}

std::size_t source_offset()
{
    return schema_offset() + kSchemaBytes;
}

std::size_t sequence_offset()
{
    return source_offset() + kSourceBytes;
}

std::size_t tag_offset()
{
    return sequence_offset() + kSequenceBytes;
}

std::size_t flags_offset()
{
    return tag_offset() + kTagBytes;
}

std::size_t fact_offset()
{
    return flags_offset() + kFlagsBytes;
}

} // namespace

TEST_CASE(
    "proposal lifecycle wire round trips every canonical fact variant",
    "[evidence-lifecycle][wire][canonical]")
{
    const auto exact_proposal = proposal("all-variants");
    const std::vector<ProposalLifecycleNotice> notices = {
        notice(
            NormalProposalRuntimeInitialized{exact_proposal},
            0,
            1),
        notice(ProposalRuntimeAborted{exact_proposal}, 1, 2),
        notice(
            ProposalCommitted{
                exact_proposal,
                std::numeric_limits<std::uint64_t>::max()},
            2,
            3),
        notice(
            ProposalConfigurationRetired{
                exact_proposal.configuration},
            3,
            4),
        notice(ProposalRetirementFloorAdvanced{8}, 4, 5)};

    for (const auto &expected : notices)
    {
        const auto encoded = hotstuff::encode_proposal_lifecycle_notice(
            expected, limits());
        REQUIRE_FALSE(encoded.empty());
        const auto decoded =
            hotstuff::decode_proposal_lifecycle_notice(encoded, limits());
        REQUIRE(decoded);
        REQUIRE(decoded.notice.has_value());
        CHECK(same_notice(*decoded.notice, expected));
        CHECK(hotstuff::encode_proposal_lifecycle_notice(
                  *decoded.notice, limits()) == encoded);
    }

    INFO("replica zero is a valid claimed source, not an authentication result");
    const auto encoded = hotstuff::encode_proposal_lifecycle_notice(
        notices.front(), limits());
    const auto decoded =
        hotstuff::decode_proposal_lifecycle_notice(encoded, limits());
    REQUIRE(decoded);
    CHECK(decoded.notice->source_replica_id == 0);
}

TEST_CASE(
    "proposal lifecycle wire uses domain-separated big-endian fields",
    "[evidence-lifecycle][wire][endianness]")
{
    const auto exact_configuration = configuration(
        0x11223344U, 0x55667788U, "big-endian-epoch");
    const auto value = notice(
        ProposalCommitted{
            proposal("big-endian", exact_configuration),
            0x1112131415161718ULL},
        0x0102U,
        0x0102030405060708ULL);
    const auto encoded = hotstuff::encode_proposal_lifecycle_notice(
        value, limits());
    const auto &domain = hotstuff::proposal_lifecycle_notice_domain();

    REQUIRE(encoded.size() == domain.size() + 16 + 80);
    CHECK(std::equal(domain.begin(), domain.end(), encoded.begin()));
    CHECK((std::vector<std::uint8_t>(
               encoded.begin() + schema_offset(),
               encoded.begin() + source_offset()) ==
           std::vector<std::uint8_t>{0, 0, 0, 2}));
    CHECK((std::vector<std::uint8_t>(
               encoded.begin() + source_offset(),
               encoded.begin() + sequence_offset()) ==
           std::vector<std::uint8_t>{1, 2}));
    CHECK((std::vector<std::uint8_t>(
               encoded.begin() + sequence_offset(),
               encoded.begin() + tag_offset()) ==
           std::vector<std::uint8_t>{1, 2, 3, 4, 5, 6, 7, 8}));
    CHECK(encoded[tag_offset()] ==
          static_cast<std::uint8_t>(
              ProposalLifecycleFactTag::committed));
    CHECK(encoded[flags_offset()] == 0);
    CHECK((std::vector<std::uint8_t>(
               encoded.begin() + fact_offset(),
               encoded.begin() + fact_offset() + 8) ==
           std::vector<std::uint8_t>{
               0x11, 0x22, 0x33, 0x44,
               0x55, 0x66, 0x77, 0x88}));
    CHECK((std::vector<std::uint8_t>(
               encoded.end() - sizeof(std::uint64_t),
               encoded.end()) ==
           std::vector<std::uint8_t>{
               0x11, 0x12, 0x13, 0x14,
               0x15, 0x16, 0x17, 0x18}));
}

TEST_CASE(
    "proposal lifecycle message is an opaque canonical opcode envelope",
    "[evidence-lifecycle][wire][message][opcode]")
{
    const auto value = notice(
        NormalProposalRuntimeInitialized{proposal("message")},
        6,
        9);
    const auto canonical = hotstuff::encode_proposal_lifecycle_notice(
        value, limits());
    const MsgProposalLifecycleNotice encoded(value, limits());
    CHECK(static_cast<bytearray_t>(encoded.serialized) == canonical);

    MsgProposalLifecycleNotice opaque{DataStream(canonical)};
    CHECK(static_cast<bytearray_t>(opaque.serialized) == canonical);
    CHECK(MsgProposalLifecycleNotice::opcode == 0x1A);
    CHECK(MsgProposalLifecycleNotice::opcode !=
          hotstuff::MsgEvidenceReport::opcode);
    CHECK(MsgProposalLifecycleNotice::opcode !=
          hotstuff::MsgAdaptiveV2EpochChangeBundle::opcode);
}

TEST_CASE(
    "proposal lifecycle encoder rejects invalid sequence identity and bounds",
    "[evidence-lifecycle][wire][encode][negative]")
{
    const auto valid = notice(
        NormalProposalRuntimeInitialized{proposal("encoder")},
        1,
        2);
    const auto canonical = hotstuff::encode_proposal_lifecycle_notice(
        valid, limits());

    auto malformed = valid;
    malformed.schema_version =
        hotstuff::kProposalLifecycleNoticeSchemaVersion + 1;
    CHECK_THROWS_AS(
        hotstuff::encode_proposal_lifecycle_notice(malformed, limits()),
        std::invalid_argument);

    malformed = valid;
    malformed.source_sequence = 0;
    CHECK_THROWS_AS(
        hotstuff::encode_proposal_lifecycle_notice(malformed, limits()),
        std::invalid_argument);
    malformed.source_sequence = std::numeric_limits<std::uint64_t>::max();
    CHECK_THROWS_AS(
        hotstuff::encode_proposal_lifecycle_notice(malformed, limits()),
        std::overflow_error);

    malformed = valid;
    std::get<NormalProposalRuntimeInitialized>(malformed.fact)
        .proposal.configuration.epoch_digest = uint256_t{};
    CHECK_THROWS_AS(
        hotstuff::encode_proposal_lifecycle_notice(malformed, limits()),
        std::invalid_argument);
    malformed = valid;
    std::get<NormalProposalRuntimeInitialized>(malformed.fact)
        .proposal.block_hash = uint256_t{};
    CHECK_THROWS_AS(
        hotstuff::encode_proposal_lifecycle_notice(malformed, limits()),
        std::invalid_argument);

    const auto invalid_configuration = notice(
        ProposalConfigurationRetired{ConfigurationId{}}, 1, 3);
    CHECK_THROWS_AS(
        hotstuff::encode_proposal_lifecycle_notice(
            invalid_configuration, limits()),
        std::invalid_argument);
    const auto invalid_floor = notice(
        ProposalRetirementFloorAdvanced{0}, 1, 4);
    CHECK_THROWS_AS(
        hotstuff::encode_proposal_lifecycle_notice(
            invalid_floor, limits()),
        std::invalid_argument);

    CHECK_THROWS_AS(
        hotstuff::encode_proposal_lifecycle_notice(
            valid, ProposalLifecycleWireLimits{0}),
        std::invalid_argument);
    CHECK_THROWS_AS(
        hotstuff::encode_proposal_lifecycle_notice(
            valid,
            ProposalLifecycleWireLimits{canonical.size() - 1}),
        std::length_error);
    CHECK_NOTHROW(hotstuff::encode_proposal_lifecycle_notice(
        valid, ProposalLifecycleWireLimits{canonical.size()}));
}

TEST_CASE(
    "proposal lifecycle decoder rejects domain schema tag and canonical flag mutations",
    "[evidence-lifecycle][wire][decode][header]")
{
    const auto canonical = hotstuff::encode_proposal_lifecycle_notice(
        notice(
            NormalProposalRuntimeInitialized{proposal("header")},
            1,
            2),
        limits());

    auto malformed = canonical;
    malformed.front() ^= 0xff;
    check_decode_error(
        malformed, ProposalLifecycleWireError::invalid_domain);

    malformed = canonical;
    write_big_endian<std::uint32_t>(malformed, schema_offset(), 1);
    check_decode_error(
        malformed, ProposalLifecycleWireError::unsupported_schema);

    malformed = canonical;
    malformed[tag_offset()] = 0xff;
    check_decode_error(
        malformed, ProposalLifecycleWireError::invalid_fact_tag);

    malformed = canonical;
    malformed[flags_offset()] = 1;
    check_decode_error(
        malformed, ProposalLifecycleWireError::noncanonical_encoding);
}

TEST_CASE(
    "proposal lifecycle decoder rejects sequence overflow and invalid identities",
    "[evidence-lifecycle][wire][decode][identity]")
{
    const auto canonical = hotstuff::encode_proposal_lifecycle_notice(
        notice(
            ProposalCommitted{proposal("identity")},
            1,
            2),
        limits());

    auto malformed = canonical;
    write_big_endian<std::uint64_t>(malformed, sequence_offset(), 0);
    check_decode_error(
        malformed, ProposalLifecycleWireError::zero_sequence);
    malformed = canonical;
    write_big_endian<std::uint64_t>(
        malformed,
        sequence_offset(),
        std::numeric_limits<std::uint64_t>::max());
    check_decode_error(
        malformed, ProposalLifecycleWireError::integer_overflow);

    malformed = canonical;
    zero_bytes(malformed, fact_offset() + 8, kDigestBytes);
    check_decode_error(
        malformed,
        ProposalLifecycleWireError::invalid_configuration_identity);
    malformed = canonical;
    zero_bytes(malformed, fact_offset() + 40, kDigestBytes);
    check_decode_error(
        malformed, ProposalLifecycleWireError::invalid_proposal_identity);

    const auto retired = hotstuff::encode_proposal_lifecycle_notice(
        notice(
            ProposalConfigurationRetired{configuration()},
            1,
            3),
        limits());
    malformed = retired;
    zero_bytes(malformed, fact_offset() + 8, kDigestBytes);
    check_decode_error(
        malformed,
        ProposalLifecycleWireError::invalid_configuration_identity);

    const auto floor = hotstuff::encode_proposal_lifecycle_notice(
        notice(ProposalRetirementFloorAdvanced{8}, 1, 4), limits());
    malformed = floor;
    write_big_endian<std::uint32_t>(malformed, fact_offset(), 0);
    check_decode_error(
        malformed,
        ProposalLifecycleWireError::invalid_retirement_floor);
}

TEST_CASE(
    "proposal lifecycle decoder rejects every truncation trailing byte and payload bound",
    "[evidence-lifecycle][wire][decode][bounds]")
{
    const auto canonical = hotstuff::encode_proposal_lifecycle_notice(
        notice(
            NormalProposalRuntimeInitialized{proposal("bounds")},
            1,
            2),
        limits());

    for (std::size_t size = 0; size < canonical.size(); ++size)
    {
        const bytearray_t truncated(
            canonical.begin(), canonical.begin() + size);
        const auto result = hotstuff::decode_proposal_lifecycle_notice(
            truncated, limits());
        INFO("truncated size " << size);
        CHECK(result.error == ProposalLifecycleWireError::truncated);
        CHECK_FALSE(result.notice.has_value());
    }

    auto trailing = canonical;
    trailing.push_back(0);
    check_decode_error(
        trailing, ProposalLifecycleWireError::trailing_bytes);
    check_decode_error(
        canonical,
        ProposalLifecycleWireError::payload_too_large,
        ProposalLifecycleWireLimits{canonical.size() - 1});
    check_decode_error(
        canonical,
        ProposalLifecycleWireError::invalid_limits,
        ProposalLifecycleWireLimits{0});
}

TEST_CASE(
    "proposal lifecycle decoder failure classes remain deterministic and distinct",
    "[evidence-lifecycle][wire][errors]")
{
    CHECK(ProposalLifecycleWireError::allocation_failure !=
          ProposalLifecycleWireError::internal_failure);
    CHECK(ProposalLifecycleWireError::integer_overflow !=
          ProposalLifecycleWireError::payload_too_large);
    CHECK(ProposalLifecycleWireError::noncanonical_encoding !=
          ProposalLifecycleWireError::trailing_bytes);

    const auto exact_maximum = notice(
        ProposalConfigurationRetired{configuration(
            std::numeric_limits<std::uint32_t>::max(),
            std::numeric_limits<std::uint32_t>::max(),
            "maximum-identities")},
        std::numeric_limits<ReplicaID>::max(),
        std::numeric_limits<std::uint64_t>::max() - 1);
    const auto encoded = hotstuff::encode_proposal_lifecycle_notice(
        exact_maximum, limits());
    const ProposalLifecycleDecodeResult decoded =
        hotstuff::decode_proposal_lifecycle_notice(encoded, limits());
    REQUIRE(decoded);
    CHECK(same_notice(*decoded.notice, exact_maximum));
}
