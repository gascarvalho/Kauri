#include "hotstuff/epoch_change_bundle.h"

#include <limits>
#include <stdexcept>
#include <type_traits>
#include <utility>

namespace hotstuff
{
namespace
{

const std::string kBundleDomain =
    "kauri-adaptive-v2-epoch-change-bundle-v1";

struct BundleFailure
{
    EpochChangeBundleWireError error;
};

[[noreturn]] void fail(EpochChangeBundleWireError error)
{
    throw BundleFailure{error};
}

bool valid_epoch_limits(const EpochWireLimits &limits) noexcept
{
    return limits.maximum_payload_bytes != 0 &&
           limits.maximum_trees != 0 &&
           limits.maximum_members_per_tree != 0 &&
           limits.maximum_string_bytes != 0 &&
           limits.maximum_wait_exempt_leaves_per_tree != 0;
}

bool valid_limits(const EpochChangeBundleLimits &limits) noexcept
{
    return limits.maximum_payload_bytes != 0 &&
           limits.maximum_command_bytes != 0 &&
           valid_epoch_limits(limits.definition_limits);
}

void require_limits(const EpochChangeBundleLimits &limits)
{
    if (!valid_limits(limits))
        throw std::invalid_argument(
            "epoch-change bundle limits must be nonzero");
}

class Writer final
{
public:
    explicit Writer(std::size_t maximum_size) : maximum_size_(maximum_size) {}

    template <typename UInt>
    void integer(UInt value)
    {
        static_assert(
            std::is_unsigned<UInt>::value,
            "epoch-change bundle integers must be unsigned");
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

    void component(const bytearray_t &value)
    {
        if (value.size() > std::numeric_limits<std::uint32_t>::max())
            throw std::length_error(
                "epoch-change bundle component exceeds uint32 length");
        integer(static_cast<std::uint32_t>(value.size()));
        append(value.data(), value.size());
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
                "epoch-change bundle payload exceeds limit");
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
            fail(EpochChangeBundleWireError::invalid_domain);
        }
        offset_ += expected.size();
    }

    template <typename UInt>
    UInt integer()
    {
        static_assert(
            std::is_unsigned<UInt>::value,
            "epoch-change bundle integers must be unsigned");
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

    bytearray_t component(std::size_t maximum_size)
    {
        const auto size = integer<std::uint32_t>();
        if (size > maximum_size)
            fail(EpochChangeBundleWireError::component_too_large);
        require(size);
        bytearray_t value(
            bytes_.begin() + offset_, bytes_.begin() + offset_ + size);
        offset_ += size;
        return value;
    }

    bool empty() const noexcept
    {
        return offset_ == bytes_.size();
    }

private:
    void require(std::size_t size) const
    {
        if (offset_ > bytes_.size() || size > bytes_.size() - offset_)
            fail(EpochChangeBundleWireError::truncated);
    }

    const bytearray_t &bytes_;
    std::size_t offset_{0};
};

struct NormalizedComponents
{
    AuthorizedEpochChange command;
    EpochDefinitionInput definition;
    bytearray_t command_bytes;
    bytearray_t definition_bytes;
};

NormalizedComponents normalize_components(
    AuthorizedEpochChange command,
    EpochDefinitionInput definition,
    const EpochChangeBundleLimits &limits)
{
    require_limits(limits);
    if (command.schema_version != kEpochChangeSchemaVersionV1 ||
        command.protocol_mode != EpochProtocolMode::adaptive_v2)
    {
        throw std::invalid_argument(
            "epoch-change bundle requires an adaptive-v2 command");
    }
    if (definition.schema_version != kEpochDefinitionSchemaVersionV2 ||
        definition.activation_height != 0)
    {
        throw std::invalid_argument(
            "epoch-change bundle requires a schedule-free v2 definition");
    }

    const auto computed_digest = compute_epoch_digest(definition);
    if ((definition.epoch_digest &&
         *definition.epoch_digest != computed_digest) ||
        command.payload.successor_epoch_number != definition.epoch_number ||
        command.payload.predecessor_epoch_digest !=
            definition.previous_epoch_digest ||
        command.payload.successor_epoch_digest != computed_digest)
    {
        throw std::invalid_argument(
            "epoch-change command does not identify its definition");
    }

    auto command_bytes = encode_authorized_epoch_change(command);
    if (command_bytes.size() > limits.maximum_command_bytes)
        throw std::length_error(
            "epoch-change bundle command exceeds limit");
    const auto canonical_command = extract_epoch_change_block_extra(
        command_bytes, limits.maximum_command_bytes);
    if (canonical_command.disposition !=
            EpochChangeExtraDisposition::present ||
        !canonical_command.command)
    {
        throw std::invalid_argument(
            "epoch-change bundle command is not canonical");
    }
    command = std::move(*canonical_command.command);

    auto definition_bytes = encode_epoch_wire(
        EpochDefinitionReply{
            kEpochWireSchemaVersionV2,
            EpochProtocolMode::adaptive_v2,
            computed_digest,
            std::move(definition)},
        limits.definition_limits);
    const auto decoded_definition = decode_epoch_definition_reply(
        definition_bytes,
        EpochProtocolMode::adaptive_v2,
        limits.definition_limits);
    if (!decoded_definition)
        throw std::logic_error(
            "encoded epoch-change definition did not round trip");

    return {
        std::move(command),
        std::move(decoded_definition.value->definition),
        std::move(command_bytes),
        std::move(definition_bytes)};
}

bytearray_t encode_bundle(
    const bytearray_t &command,
    const bytearray_t &definition,
    const EpochChangeBundleLimits &limits)
{
    Writer writer(limits.maximum_payload_bytes);
    writer.domain(kBundleDomain);
    writer.integer(kEpochChangeBundleSchemaVersionV1);
    writer.integer(
        static_cast<std::uint8_t>(EpochProtocolMode::adaptive_v2));
    writer.component(command);
    writer.component(definition);
    return std::move(writer).finish();
}

EpochChangeBundleDecodeResult rejected(
    EpochChangeBundleWireError error,
    EpochChangeWireError command_error = EpochChangeWireError::none,
    EpochWireError definition_error = EpochWireError::none) noexcept
{
    return {error, command_error, definition_error, std::nullopt};
}

} // namespace

const std::string &epoch_change_bundle_domain() noexcept
{
    return kBundleDomain;
}

AdaptiveV2EpochChangeBundle::AdaptiveV2EpochChangeBundle(
    AuthorizedEpochChange command,
    EpochDefinitionInput definition,
    const EpochChangeBundleLimits &limits)
{
    auto normalized = normalize_components(
        std::move(command), std::move(definition), limits);
    command_ = std::move(normalized.command);
    definition_ = std::move(normalized.definition);
    canonical_bytes_ = encode_bundle(
        normalized.command_bytes, normalized.definition_bytes, limits);
}

EpochChangeBundleDecodeResult decode_adaptive_v2_epoch_change_bundle(
    const bytearray_t &payload,
    const EpochChangeBundleLimits &limits) noexcept
{
    if (!valid_limits(limits))
        return rejected(EpochChangeBundleWireError::invalid_limits);
    if (payload.size() > limits.maximum_payload_bytes)
        return rejected(EpochChangeBundleWireError::payload_too_large);

    try
    {
        Reader reader(payload);
        reader.domain(kBundleDomain);
        const auto schema = reader.integer<std::uint32_t>();
        if (schema != kEpochChangeBundleSchemaVersionV1)
            return rejected(EpochChangeBundleWireError::unsupported_schema);
        const auto mode = static_cast<EpochProtocolMode>(
            reader.integer<std::uint8_t>());
        if (mode != EpochProtocolMode::adaptive_v2)
            return rejected(EpochChangeBundleWireError::mode_mismatch);

        auto command_bytes = reader.component(limits.maximum_command_bytes);
        auto definition_bytes = reader.component(
            limits.definition_limits.maximum_payload_bytes);
        if (!reader.empty())
            return rejected(EpochChangeBundleWireError::trailing_bytes);

        auto command = extract_epoch_change_block_extra(
            command_bytes, limits.maximum_command_bytes);
        if (command.disposition != EpochChangeExtraDisposition::present ||
            !command.command)
        {
            return rejected(
                EpochChangeBundleWireError::invalid_command,
                command.wire_error == EpochChangeWireError::none
                    ? EpochChangeWireError::internal_failure
                    : command.wire_error);
        }

        auto definition = decode_epoch_definition_reply(
            definition_bytes,
            EpochProtocolMode::adaptive_v2,
            limits.definition_limits);
        if (!definition)
        {
            return rejected(
                EpochChangeBundleWireError::invalid_definition,
                EpochChangeWireError::none,
                definition.error);
        }

        AdaptiveV2EpochChangeBundle bundle(
            std::move(*command.command),
            std::move(definition.value->definition),
            limits);
        if (bundle.canonical_bytes() != payload)
            return rejected(
                EpochChangeBundleWireError::noncanonical_encoding);
        return {
            EpochChangeBundleWireError::none,
            EpochChangeWireError::none,
            EpochWireError::none,
            std::move(bundle)};
    }
    catch (const BundleFailure &failure)
    {
        return rejected(failure.error);
    }
    catch (const std::invalid_argument &)
    {
        return rejected(EpochChangeBundleWireError::identity_mismatch);
    }
    catch (const std::length_error &)
    {
        return rejected(EpochChangeBundleWireError::component_too_large);
    }
    catch (const std::bad_alloc &)
    {
        return rejected(EpochChangeBundleWireError::allocation_failure);
    }
    catch (...)
    {
        return rejected(EpochChangeBundleWireError::internal_failure);
    }
}

const opcode_t MsgAdaptiveV2EpochChangeBundle::opcode;

MsgAdaptiveV2EpochChangeBundle::MsgAdaptiveV2EpochChangeBundle(
    const AdaptiveV2EpochChangeBundle &value)
    : serialized(value.canonical_bytes())
{
}

MsgAdaptiveV2EpochChangeBundle::MsgAdaptiveV2EpochChangeBundle(
    DataStream &&serialized_payload)
    : serialized(std::move(serialized_payload))
{
}

} // namespace hotstuff
