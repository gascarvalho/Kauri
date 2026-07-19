#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/adaptive_v2_readiness_wire.h"

namespace
{

using hotstuff::AdaptiveV2ReadinessDecodeResult;
using hotstuff::AdaptiveV2ReadinessNotice;
using hotstuff::AdaptiveV2ReadinessWireError;
using hotstuff::AdaptiveV2ReadinessWireLimits;
using hotstuff::ConfigurationId;
using hotstuff::DataStream;
using hotstuff::MsgAdaptiveV2ReadinessNotice;
using hotstuff::ReplicaID;
using hotstuff::bytearray_t;
using hotstuff::uint256_t;

static_assert(noexcept(hotstuff::decode_adaptive_v2_readiness_notice(
    std::declval<const bytearray_t &>(),
    std::declval<const AdaptiveV2ReadinessWireLimits &>())));
static_assert(MsgAdaptiveV2ReadinessNotice::opcode == 0x1B);
static_assert(
    MsgAdaptiveV2ReadinessNotice::opcode < 0x12 ||
    MsgAdaptiveV2ReadinessNotice::opcode > 0x1A,
    "readiness opcode must remain outside existing adaptive wire range");

uint256_t digest(const std::string &label)
{
    return DataStream(label).get_hash();
}

AdaptiveV2ReadinessNotice notice(
    ReplicaID source = 0,
    std::uint64_t sequence = 1,
    ConfigurationId active_configuration =
        ConfigurationId{7, 3, digest("active-epoch")},
    std::uint64_t activation_generation = 9,
    std::uint64_t committed_height = 0)
{
    return AdaptiveV2ReadinessNotice{
        hotstuff::kAdaptiveV2ReadinessNoticeSchemaVersionV1,
        source,
        sequence,
        std::move(active_configuration),
        activation_generation,
        committed_height};
}

AdaptiveV2ReadinessWireLimits limits()
{
    return AdaptiveV2ReadinessWireLimits{256};
}

bool same_notice(
    const AdaptiveV2ReadinessNotice &left,
    const AdaptiveV2ReadinessNotice &right)
{
    return left.schema_version == right.schema_version &&
           left.claimed_source_replica_id ==
               right.claimed_source_replica_id &&
           left.source_sequence == right.source_sequence &&
           left.active_configuration == right.active_configuration &&
           left.activation_generation == right.activation_generation &&
           left.committed_height == right.committed_height;
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
    AdaptiveV2ReadinessWireError expected,
    AdaptiveV2ReadinessWireLimits configured_limits = limits())
{
    const AdaptiveV2ReadinessDecodeResult result =
        hotstuff::decode_adaptive_v2_readiness_notice(
            payload, configured_limits);
    CHECK(result.error == expected);
    CHECK_FALSE(result.notice.has_value());
}

std::size_t domain_size()
{
    return hotstuff::adaptive_v2_readiness_notice_domain().size();
}

constexpr std::size_t kSchemaBytes = sizeof(std::uint32_t);
constexpr std::size_t kFlagsBytes = sizeof(std::uint8_t);
constexpr std::size_t kSourceBytes = sizeof(ReplicaID);
constexpr std::size_t kSequenceBytes = sizeof(std::uint64_t);
constexpr std::size_t kEpochBytes = sizeof(std::uint32_t);
constexpr std::size_t kTreeBytes = sizeof(std::uint32_t);
constexpr std::size_t kDigestBytes = 32;
constexpr std::size_t kGenerationBytes = sizeof(std::uint64_t);
constexpr std::size_t kHeightBytes = sizeof(std::uint64_t);

std::size_t schema_offset()
{
    return domain_size();
}

std::size_t flags_offset()
{
    return schema_offset() + kSchemaBytes;
}

std::size_t source_offset()
{
    return flags_offset() + kFlagsBytes;
}

std::size_t sequence_offset()
{
    return source_offset() + kSourceBytes;
}

std::size_t epoch_offset()
{
    return sequence_offset() + kSequenceBytes;
}

std::size_t tree_offset()
{
    return epoch_offset() + kEpochBytes;
}

std::size_t digest_offset()
{
    return tree_offset() + kTreeBytes;
}

std::size_t generation_offset()
{
    return digest_offset() + kDigestBytes;
}

std::size_t height_offset()
{
    return generation_offset() + kGenerationBytes;
}

} // namespace

TEST_CASE(
    "adaptive v2 readiness notice round trips canonical epoch-zero readiness",
    "[adaptive-v2][readiness][wire][canonical]")
{
    const auto expected = notice(
        0,
        1,
        ConfigurationId{0, 0, digest("epoch-zero")},
        1,
        0);
    const auto encoded = hotstuff::encode_adaptive_v2_readiness_notice(
        expected, limits());
    const auto decoded = hotstuff::decode_adaptive_v2_readiness_notice(
        encoded, limits());

    REQUIRE(decoded);
    REQUIRE(decoded.notice.has_value());
    CHECK(same_notice(*decoded.notice, expected));
    CHECK(decoded.notice->committed_height == 0);
    CHECK(decoded.notice->claimed_source_replica_id == 0);
    CHECK(hotstuff::encode_adaptive_v2_readiness_notice(
              *decoded.notice, limits()) == encoded);
}

TEST_CASE(
    "adaptive v2 readiness wire is domain separated and big endian",
    "[adaptive-v2][readiness][wire][endianness]")
{
    const auto value = notice(
        0x0102U,
        0x0102030405060708ULL,
        ConfigurationId{
            0x11223344U,
            0x55667788U,
            digest("big-endian-readiness")},
        0x1112131415161718ULL,
        0x2122232425262728ULL);
    const auto encoded = hotstuff::encode_adaptive_v2_readiness_notice(
        value, limits());
    const auto &domain =
        hotstuff::adaptive_v2_readiness_notice_domain();

    REQUIRE(encoded.size() == domain.size() + kSchemaBytes +
                                  kFlagsBytes + kSourceBytes +
                                  kSequenceBytes + kEpochBytes +
                                  kTreeBytes + kDigestBytes +
                                  kGenerationBytes + kHeightBytes);
    CHECK(std::equal(domain.begin(), domain.end(), encoded.begin()));
    CHECK((std::vector<std::uint8_t>(
               encoded.begin() + schema_offset(),
               encoded.begin() + flags_offset()) ==
           std::vector<std::uint8_t>{0, 0, 0, 1}));
    CHECK(encoded[flags_offset()] == 0);
    CHECK((std::vector<std::uint8_t>(
               encoded.begin() + source_offset(),
               encoded.begin() + sequence_offset()) ==
           std::vector<std::uint8_t>{1, 2}));
    CHECK((std::vector<std::uint8_t>(
               encoded.begin() + sequence_offset(),
               encoded.begin() + epoch_offset()) ==
           std::vector<std::uint8_t>{1, 2, 3, 4, 5, 6, 7, 8}));
    CHECK((std::vector<std::uint8_t>(
               encoded.begin() + epoch_offset(),
               encoded.begin() + digest_offset()) ==
           std::vector<std::uint8_t>{
               0x11, 0x22, 0x33, 0x44,
               0x55, 0x66, 0x77, 0x88}));
    CHECK((std::vector<std::uint8_t>(
               encoded.begin() + generation_offset(),
               encoded.begin() + height_offset()) ==
           std::vector<std::uint8_t>{
               0x11, 0x12, 0x13, 0x14,
               0x15, 0x16, 0x17, 0x18}));
    CHECK((std::vector<std::uint8_t>(
               encoded.begin() + height_offset(), encoded.end()) ==
           std::vector<std::uint8_t>{
               0x21, 0x22, 0x23, 0x24,
               0x25, 0x26, 0x27, 0x28}));
}

TEST_CASE(
    "adaptive v2 readiness message preserves one opaque canonical claim",
    "[adaptive-v2][readiness][wire][message][opcode]")
{
    const auto value = notice(6, 9);
    const auto canonical =
        hotstuff::encode_adaptive_v2_readiness_notice(value, limits());

    const MsgAdaptiveV2ReadinessNotice encoded(value, limits());
    CHECK(static_cast<bytearray_t>(encoded.serialized) == canonical);

    MsgAdaptiveV2ReadinessNotice opaque{DataStream(canonical)};
    CHECK(static_cast<bytearray_t>(opaque.serialized) == canonical);
    CHECK(MsgAdaptiveV2ReadinessNotice::opcode == 0x1B);
    CHECK((MsgAdaptiveV2ReadinessNotice::opcode < 0x12 ||
           MsgAdaptiveV2ReadinessNotice::opcode > 0x1A));
}

TEST_CASE(
    "adaptive v2 readiness encoder rejects invalid schema identity and bounds",
    "[adaptive-v2][readiness][wire][encode][negative]")
{
    const auto valid = notice(3, 4);
    const auto canonical =
        hotstuff::encode_adaptive_v2_readiness_notice(valid, limits());

    auto malformed = valid;
    ++malformed.schema_version;
    CHECK_THROWS_AS(
        hotstuff::encode_adaptive_v2_readiness_notice(
            malformed, limits()),
        std::invalid_argument);

    malformed = valid;
    malformed.source_sequence = 0;
    CHECK_THROWS_AS(
        hotstuff::encode_adaptive_v2_readiness_notice(
            malformed, limits()),
        std::invalid_argument);

    malformed = valid;
    malformed.active_configuration.epoch_digest = uint256_t{};
    CHECK_THROWS_AS(
        hotstuff::encode_adaptive_v2_readiness_notice(
            malformed, limits()),
        std::invalid_argument);

    malformed = valid;
    malformed.activation_generation = 0;
    CHECK_THROWS_AS(
        hotstuff::encode_adaptive_v2_readiness_notice(
            malformed, limits()),
        std::invalid_argument);

    CHECK_THROWS_AS(
        hotstuff::encode_adaptive_v2_readiness_notice(
            valid, AdaptiveV2ReadinessWireLimits{0}),
        std::invalid_argument);
    CHECK_THROWS_AS(
        hotstuff::encode_adaptive_v2_readiness_notice(
            valid,
            AdaptiveV2ReadinessWireLimits{canonical.size() - 1}),
        std::length_error);
    CHECK_NOTHROW(hotstuff::encode_adaptive_v2_readiness_notice(
        valid, AdaptiveV2ReadinessWireLimits{canonical.size()}));
}

TEST_CASE(
    "adaptive v2 readiness decoder rejects malformed canonical fields",
    "[adaptive-v2][readiness][wire][decode][negative]")
{
    const auto canonical = hotstuff::encode_adaptive_v2_readiness_notice(
        notice(2, 7), limits());

    check_decode_error(
        canonical,
        AdaptiveV2ReadinessWireError::invalid_limits,
        AdaptiveV2ReadinessWireLimits{0});
    check_decode_error(
        canonical,
        AdaptiveV2ReadinessWireError::payload_too_large,
        AdaptiveV2ReadinessWireLimits{canonical.size() - 1});

    auto malformed = canonical;
    malformed.front() ^= 0x01;
    check_decode_error(
        malformed, AdaptiveV2ReadinessWireError::invalid_domain);

    malformed = canonical;
    write_big_endian<std::uint32_t>(
        malformed,
        schema_offset(),
        hotstuff::kAdaptiveV2ReadinessNoticeSchemaVersionV1 + 1);
    check_decode_error(
        malformed, AdaptiveV2ReadinessWireError::unsupported_schema);

    malformed = canonical;
    malformed[flags_offset()] = 1;
    check_decode_error(
        malformed, AdaptiveV2ReadinessWireError::noncanonical_encoding);

    malformed = canonical;
    zero_bytes(malformed, sequence_offset(), kSequenceBytes);
    check_decode_error(
        malformed, AdaptiveV2ReadinessWireError::zero_source_sequence);

    malformed = canonical;
    zero_bytes(malformed, digest_offset(), kDigestBytes);
    check_decode_error(
        malformed,
        AdaptiveV2ReadinessWireError::invalid_configuration_identity);

    malformed = canonical;
    zero_bytes(malformed, generation_offset(), kGenerationBytes);
    check_decode_error(
        malformed,
        AdaptiveV2ReadinessWireError::zero_activation_generation);

    malformed = canonical;
    malformed.pop_back();
    check_decode_error(
        malformed, AdaptiveV2ReadinessWireError::truncated);

    malformed = canonical;
    malformed.push_back(0);
    check_decode_error(
        malformed, AdaptiveV2ReadinessWireError::trailing_bytes);
}

TEST_CASE(
    "readiness decode grants no authentication or quorum authority",
    "[adaptive-v2][readiness][wire][authority]")
{
    auto canonical = hotstuff::encode_adaptive_v2_readiness_notice(
        notice(1, 5), limits());
    write_big_endian<ReplicaID>(canonical, source_offset(), 6);

    const auto decoded = hotstuff::decode_adaptive_v2_readiness_notice(
        canonical, limits());
    REQUIRE(decoded);
    REQUIRE(decoded.notice.has_value());
    CHECK(decoded.notice->claimed_source_replica_id == 6);
    INFO("the transport owner must compare this claim with mutual TLS");
}
