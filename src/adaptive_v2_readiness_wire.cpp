#include "hotstuff/adaptive_v2_readiness_wire.h"

#include <algorithm>
#include <new>
#include <stdexcept>
#include <type_traits>
#include <utility>

namespace hotstuff
{
namespace
{

const std::string kReadinessNoticeDomain =
    "kauri-adaptive-v2-runtime-readiness-notice-v1";
constexpr std::size_t kDigestSize = 32;
constexpr std::uint8_t kCanonicalFlags = 0;

struct WireFailure
{
    AdaptiveV2ReadinessWireError error;
};

[[noreturn]] void fail(AdaptiveV2ReadinessWireError error)
{
    throw WireFailure{error};
}

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

class Writer final
{
public:
    explicit Writer(std::size_t maximum_size)
        : maximum_size_(maximum_size)
    {}

    template<typename UInt>
    void integer(UInt value)
    {
        static_assert(
            std::is_unsigned<UInt>::value,
            "adaptive-v2 readiness integers must be unsigned");
        ensure(sizeof(UInt));
        for (std::size_t shift = sizeof(UInt); shift > 0; --shift)
        {
            bytes_.push_back(static_cast<std::uint8_t>(
                value >> ((shift - 1) * 8)));
        }
    }

    void domain(const std::string &value)
    {
        append(
            reinterpret_cast<const std::uint8_t *>(value.data()),
            value.size());
    }

    void digest(const uint256_t &value)
    {
        const bytearray_t bytes = static_cast<bytearray_t>(value);
        if (bytes.size() != kDigestSize)
        {
            throw std::logic_error(
                "adaptive-v2 readiness digest is not 32 bytes");
        }
        append(bytes.data(), bytes.size());
    }

    bytearray_t finish() &&
    {
        return std::move(bytes_);
    }

private:
    void ensure(std::size_t additional)
    {
        if (bytes_.size() > maximum_size_ ||
            additional > maximum_size_ - bytes_.size())
        {
            throw std::length_error(
                "adaptive-v2 readiness payload exceeds byte limit");
        }
    }

    void append(const std::uint8_t *data, std::size_t size)
    {
        ensure(size);
        bytes_.insert(bytes_.end(), data, data + size);
    }

    std::size_t maximum_size_;
    bytearray_t bytes_;
};

class Reader final
{
public:
    explicit Reader(const bytearray_t &bytes) : bytes_(bytes) {}

    void domain(const std::string &expected)
    {
        require(expected.size());
        if (!std::equal(
                expected.begin(),
                expected.end(),
                bytes_.begin() + offset_))
        {
            fail(AdaptiveV2ReadinessWireError::invalid_domain);
        }
        offset_ += expected.size();
    }

    template<typename UInt>
    UInt integer()
    {
        static_assert(
            std::is_unsigned<UInt>::value,
            "adaptive-v2 readiness integers must be unsigned");
        require(sizeof(UInt));
        UInt value = 0;
        for (std::size_t index = 0; index < sizeof(UInt); ++index)
        {
            value = static_cast<UInt>(
                (value << 8) | bytes_[offset_ + index]);
        }
        offset_ += sizeof(UInt);
        return value;
    }

    uint256_t digest()
    {
        require(kDigestSize);
        const uint256_t result(bytes_.data() + offset_);
        offset_ += kDigestSize;
        return result;
    }

    bool empty() const noexcept
    {
        return offset_ == bytes_.size();
    }

private:
    void require(std::size_t size) const
    {
        if (offset_ > bytes_.size() ||
            size > bytes_.size() - offset_)
        {
            fail(AdaptiveV2ReadinessWireError::truncated);
        }
    }

    const bytearray_t &bytes_;
    std::size_t offset_{0};
};

void encode_configuration(
    Writer &writer,
    const ConfigurationId &configuration)
{
    writer.integer(configuration.epoch_number);
    writer.integer(configuration.tree_id);
    writer.digest(configuration.epoch_digest);
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

    Reader reader(payload);
    reader.domain(kReadinessNoticeDomain);

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

    Writer writer(limits.maximum_payload_bytes);
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
