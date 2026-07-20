#include "hotstuff/epoch_change_bundle.h"

#include <limits>
#include <stdexcept>
#include <utility>

#include "detail/canonical_wire_codec.h"

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

using Writer = detail::CanonicalWireWriter;
using Reader = detail::CanonicalWireReader<
    BundleFailure,
    EpochChangeBundleWireError>;

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

void append_component(Writer &writer, const bytearray_t &value)
{
    if (value.size() > std::numeric_limits<std::uint32_t>::max())
        throw std::length_error(
            "epoch-change bundle component exceeds uint32 length");
    writer.integer(static_cast<std::uint32_t>(value.size()));
    writer.bytes(value);
}

bytearray_t read_component(Reader &reader, std::size_t maximum_size)
{
    const auto size = reader.integer<std::uint32_t>();
    if (size > maximum_size)
        fail(EpochChangeBundleWireError::component_too_large);
    return reader.bytes(size);
}

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
    Writer writer(
        limits.maximum_payload_bytes,
        "epoch-change bundle payload exceeds limit");
    writer.domain(kBundleDomain);
    writer.integer(kEpochChangeBundleSchemaVersionV1);
    writer.integer(
        static_cast<std::uint8_t>(EpochProtocolMode::adaptive_v2));
    append_component(writer, command);
    append_component(writer, definition);
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
        Reader reader(payload, EpochChangeBundleWireError::truncated);
        reader.domain(
            kBundleDomain,
            EpochChangeBundleWireError::invalid_domain);
        const auto schema = reader.integer<std::uint32_t>();
        if (schema != kEpochChangeBundleSchemaVersionV1)
            return rejected(EpochChangeBundleWireError::unsupported_schema);
        const auto mode = static_cast<EpochProtocolMode>(
            reader.integer<std::uint8_t>());
        if (mode != EpochProtocolMode::adaptive_v2)
            return rejected(EpochChangeBundleWireError::mode_mismatch);

        auto command_bytes = read_component(
            reader, limits.maximum_command_bytes);
        auto definition_bytes = read_component(
            reader,
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
