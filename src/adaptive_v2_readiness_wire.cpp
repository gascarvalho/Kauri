#include "hotstuff/adaptive_v2_readiness_wire.h"

#include <new>
#include <stdexcept>
#include <utility>

#include "detail/canonical_wire_codec.h"

namespace hotstuff
{
namespace
{

const std::string kReadinessNoticeDomain =
    "kauri-adaptive-v2-runtime-readiness-notice-v1";
constexpr std::uint8_t kCanonicalFlags = 0;

struct WireFailure
{
    AdaptiveV2ReadinessWireError error;
};

using Writer = detail::CanonicalWireWriter;
using Reader = detail::CanonicalWireReader<
    WireFailure,
    AdaptiveV2ReadinessWireError>;

bool valid_limits(
    const AdaptiveV2ReadinessWireLimits &limits) noexcept
{
    return limits.maximum_payload_bytes != 0;
}

bool zero_digest(const uint256_t &digest) noexcept
{
    return digest == uint256_t{};
}

AdaptiveV2ReadinessWireError validate_notice(
    const AdaptiveV2ReadinessNotice &notice) noexcept
{
    if (notice.schema_version !=
        kAdaptiveV2ReadinessNoticeSchemaVersionV1)
    {
        return AdaptiveV2ReadinessWireError::unsupported_schema;
    }
    if (notice.source_sequence == 0)
        return AdaptiveV2ReadinessWireError::zero_source_sequence;
    if (zero_digest(notice.active_configuration.epoch_digest))
    {
        return AdaptiveV2ReadinessWireError::
            invalid_configuration_identity;
    }
    if (notice.activation_generation == 0)
    {
        return AdaptiveV2ReadinessWireError::
            zero_activation_generation;
    }
    return AdaptiveV2ReadinessWireError::none;
}

[[noreturn]] void throw_encoding_error(
    AdaptiveV2ReadinessWireError error)
{
    switch (error)
    {
    case AdaptiveV2ReadinessWireError::unsupported_schema:
    case AdaptiveV2ReadinessWireError::zero_source_sequence:
    case AdaptiveV2ReadinessWireError::invalid_configuration_identity:
    case AdaptiveV2ReadinessWireError::zero_activation_generation:
        throw std::invalid_argument(
            "adaptive-v2 readiness notice is not canonical");
    default:
        throw std::logic_error(
            "unexpected adaptive-v2 readiness validation error");
    }
}

void encode_configuration(
    Writer &writer,
    const ConfigurationId &configuration)
{
    writer.integer(configuration.epoch_number);
    writer.integer(configuration.tree_id);
    writer.digest(
        configuration.epoch_digest,
        "adaptive-v2 readiness digest is not 32 bytes");
}

ConfigurationId decode_configuration(Reader &reader)
{
    ConfigurationId configuration;
    configuration.epoch_number = reader.integer<std::uint32_t>();
    configuration.tree_id = reader.integer<std::uint32_t>();
    configuration.epoch_digest = reader.digest();
    return configuration;
}

AdaptiveV2ReadinessDecodeResult rejected(
    AdaptiveV2ReadinessWireError error) noexcept
{
    return {error, std::nullopt};
}

AdaptiveV2ReadinessDecodeResult decode_impl(
    const bytearray_t &payload,
    const AdaptiveV2ReadinessWireLimits &limits)
{
    if (!valid_limits(limits))
        return rejected(AdaptiveV2ReadinessWireError::invalid_limits);
    if (payload.size() > limits.maximum_payload_bytes)
    {
        return rejected(
            AdaptiveV2ReadinessWireError::payload_too_large);
    }

    Reader reader(
        payload, AdaptiveV2ReadinessWireError::truncated);
    reader.domain(
        kReadinessNoticeDomain,
        AdaptiveV2ReadinessWireError::invalid_domain);

    AdaptiveV2ReadinessNotice notice;
    notice.schema_version = reader.integer<std::uint32_t>();
    if (notice.schema_version !=
        kAdaptiveV2ReadinessNoticeSchemaVersionV1)
    {
        return rejected(
            AdaptiveV2ReadinessWireError::unsupported_schema);
    }
    if (reader.integer<std::uint8_t>() != kCanonicalFlags)
    {
        return rejected(
            AdaptiveV2ReadinessWireError::noncanonical_encoding);
    }

    notice.claimed_source_replica_id = reader.integer<ReplicaID>();
    notice.source_sequence = reader.integer<std::uint64_t>();
    if (notice.source_sequence == 0)
    {
        return rejected(
            AdaptiveV2ReadinessWireError::zero_source_sequence);
    }
    notice.active_configuration = decode_configuration(reader);
    if (zero_digest(notice.active_configuration.epoch_digest))
    {
        return rejected(
            AdaptiveV2ReadinessWireError::
                invalid_configuration_identity);
    }
    notice.activation_generation = reader.integer<std::uint64_t>();
    if (notice.activation_generation == 0)
    {
        return rejected(
            AdaptiveV2ReadinessWireError::
                zero_activation_generation);
    }
    notice.committed_height = reader.integer<std::uint64_t>();

    if (!reader.empty())
    {
        return rejected(
            AdaptiveV2ReadinessWireError::trailing_bytes);
    }
    const auto canonical = encode_adaptive_v2_readiness_notice(
        notice, limits);
    if (canonical != payload)
    {
        return rejected(
            AdaptiveV2ReadinessWireError::noncanonical_encoding);
    }
    return {
        AdaptiveV2ReadinessWireError::none,
        std::move(notice)};
}

} // namespace

const std::string &adaptive_v2_readiness_notice_domain() noexcept
{
    return kReadinessNoticeDomain;
}

bytearray_t encode_adaptive_v2_readiness_notice(
    const AdaptiveV2ReadinessNotice &notice,
    const AdaptiveV2ReadinessWireLimits &limits)
{
    if (!valid_limits(limits))
    {
        throw std::invalid_argument(
            "adaptive-v2 readiness wire limit must be nonzero");
    }
    const auto validation = validate_notice(notice);
    if (validation != AdaptiveV2ReadinessWireError::none)
        throw_encoding_error(validation);

    Writer writer(
        limits.maximum_payload_bytes,
        "adaptive-v2 readiness payload exceeds byte limit");
    writer.domain(kReadinessNoticeDomain);
    writer.integer(notice.schema_version);
    writer.integer(kCanonicalFlags);
    writer.integer(notice.claimed_source_replica_id);
    writer.integer(notice.source_sequence);
    encode_configuration(writer, notice.active_configuration);
    writer.integer(notice.activation_generation);
    writer.integer(notice.committed_height);
    return std::move(writer).finish();
}

AdaptiveV2ReadinessDecodeResult decode_adaptive_v2_readiness_notice(
    const bytearray_t &payload,
    const AdaptiveV2ReadinessWireLimits &limits) noexcept
{
    try
    {
        return decode_impl(payload, limits);
    }
    catch (const WireFailure &failure)
    {
        return rejected(failure.error);
    }
    catch (const std::bad_alloc &)
    {
        return rejected(
            AdaptiveV2ReadinessWireError::allocation_failure);
    }
    catch (...)
    {
        return rejected(
            AdaptiveV2ReadinessWireError::internal_failure);
    }
}

const opcode_t MsgAdaptiveV2ReadinessNotice::opcode;

MsgAdaptiveV2ReadinessNotice::MsgAdaptiveV2ReadinessNotice(
    const AdaptiveV2ReadinessNotice &notice,
    const AdaptiveV2ReadinessWireLimits &limits)
    : serialized(
          encode_adaptive_v2_readiness_notice(notice, limits))
{}

MsgAdaptiveV2ReadinessNotice::MsgAdaptiveV2ReadinessNotice(
    DataStream &&serialized_payload)
    : serialized(std::move(serialized_payload))
{}

} // namespace hotstuff
