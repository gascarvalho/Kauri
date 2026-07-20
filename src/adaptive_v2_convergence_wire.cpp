#include "hotstuff/adaptive_v2_convergence_wire.h"

#include <limits>
#include <new>
#include <stdexcept>
#include <utility>

#include "detail/canonical_wire_codec.h"

namespace hotstuff
{
namespace
{

const std::string kCommittedObservationDomain =
    "kauri-adaptive-v2-epoch-change-committed-observation-v1";
const std::string kActivatedObservationDomain =
    "kauri-adaptive-v2-epoch-activated-observation-v1";
constexpr std::uint8_t kCanonicalFlags = 0;

struct WireFailure
{
    AdaptiveV2ConvergenceWireError error;
};

using Writer = detail::CanonicalWireWriter;
using Reader = detail::CanonicalWireReader<
    WireFailure,
    AdaptiveV2ConvergenceWireError>;

bool valid_limits(
    const AdaptiveV2ConvergenceWireLimits &limits) noexcept
{
    return limits.maximum_payload_bytes != 0;
}

bool zero_digest(const uint256_t &digest) noexcept
{
    return digest == uint256_t{};
}

AdaptiveV2ConvergenceWireError validate_identity(
    const AdaptiveV2EpochChangeIdentity &identity) noexcept
{
    if (identity.command_block_height == 0)
    {
        return AdaptiveV2ConvergenceWireError::
            zero_command_block_height;
    }
    if (identity.activation_delay_blocks == 0)
    {
        return AdaptiveV2ConvergenceWireError::
            invalid_activation_delay;
    }
    if (identity.command_block_height >
        std::numeric_limits<std::uint64_t>::max() -
            identity.activation_delay_blocks)
    {
        return AdaptiveV2ConvergenceWireError::
            activation_height_overflow;
    }
    if (identity.activation_height !=
        identity.command_block_height +
            identity.activation_delay_blocks)
    {
        return AdaptiveV2ConvergenceWireError::
            inconsistent_activation_height;
    }
    if (identity.predecessor_epoch_number ==
            std::numeric_limits<std::uint32_t>::max() ||
        identity.successor_epoch_number !=
            identity.predecessor_epoch_number + 1 ||
        zero_digest(identity.predecessor_epoch_digest) ||
        zero_digest(identity.successor_epoch_digest) ||
        zero_digest(identity.command_payload_digest) ||
        zero_digest(identity.command_block_hash) ||
        identity.predecessor_epoch_digest ==
            identity.successor_epoch_digest)
    {
        return AdaptiveV2ConvergenceWireError::invalid_identity;
    }
    return AdaptiveV2ConvergenceWireError::none;
}

AdaptiveV2ConvergenceWireError validate_committed(
    const AdaptiveV2EpochChangeCommittedObservation &observation) noexcept
{
    if (observation.schema_version !=
        kAdaptiveV2ConvergenceObservationSchemaVersionV1)
    {
        return AdaptiveV2ConvergenceWireError::unsupported_schema;
    }
    return validate_identity(observation.identity);
}

AdaptiveV2ConvergenceWireError validate_activated(
    const AdaptiveV2EpochActivatedObservation &observation) noexcept
{
    if (observation.schema_version !=
        kAdaptiveV2ConvergenceObservationSchemaVersionV1)
    {
        return AdaptiveV2ConvergenceWireError::unsupported_schema;
    }
    const auto identity_error = validate_identity(observation.identity);
    if (identity_error != AdaptiveV2ConvergenceWireError::none)
        return identity_error;
    if (observation.activated_epoch_number !=
            observation.identity.successor_epoch_number ||
        observation.activated_epoch_digest !=
            observation.identity.successor_epoch_digest)
    {
        return AdaptiveV2ConvergenceWireError::invalid_identity;
    }
    return AdaptiveV2ConvergenceWireError::none;
}

[[noreturn]] void throw_encoding_error(
    AdaptiveV2ConvergenceWireError error)
{
    switch (error)
    {
    case AdaptiveV2ConvergenceWireError::unsupported_schema:
    case AdaptiveV2ConvergenceWireError::zero_command_block_height:
    case AdaptiveV2ConvergenceWireError::invalid_activation_delay:
    case AdaptiveV2ConvergenceWireError::activation_height_overflow:
    case AdaptiveV2ConvergenceWireError::inconsistent_activation_height:
    case AdaptiveV2ConvergenceWireError::invalid_identity:
        throw std::invalid_argument(
            "adaptive-v2 convergence observation is not canonical");
    default:
        throw std::logic_error(
            "unexpected adaptive-v2 convergence validation error");
    }
}

void encode_identity(
    Writer &writer,
    const AdaptiveV2EpochChangeIdentity &identity)
{
    writer.integer(identity.predecessor_epoch_number);
    writer.digest(
        identity.predecessor_epoch_digest,
        "adaptive-v2 predecessor digest is not 32 bytes");
    writer.integer(identity.successor_epoch_number);
    writer.digest(
        identity.successor_epoch_digest,
        "adaptive-v2 successor digest is not 32 bytes");
    writer.digest(
        identity.command_payload_digest,
        "adaptive-v2 command digest is not 32 bytes");
    writer.integer(identity.command_block_height);
    writer.digest(
        identity.command_block_hash,
        "adaptive-v2 command block hash is not 32 bytes");
    writer.integer(identity.activation_delay_blocks);
    writer.integer(identity.activation_height);
}

AdaptiveV2EpochChangeIdentity decode_identity(Reader &reader)
{
    AdaptiveV2EpochChangeIdentity identity;
    identity.predecessor_epoch_number =
        reader.integer<std::uint32_t>();
    identity.predecessor_epoch_digest = reader.digest();
    identity.successor_epoch_number =
        reader.integer<std::uint32_t>();
    identity.successor_epoch_digest = reader.digest();
    identity.command_payload_digest = reader.digest();
    identity.command_block_height = reader.integer<std::uint64_t>();
    identity.command_block_hash = reader.digest();
    identity.activation_delay_blocks = reader.integer<std::uint64_t>();
    identity.activation_height = reader.integer<std::uint64_t>();
    return identity;
}

template<typename Observation>
AdaptiveV2ConvergenceDecodeResult<Observation> rejected(
    AdaptiveV2ConvergenceWireError error) noexcept
{
    return {error, std::nullopt};
}

AdaptiveV2EpochChangeCommittedDecodeResult decode_committed_impl(
    const bytearray_t &payload,
    const AdaptiveV2ConvergenceWireLimits &limits)
{
    if (!valid_limits(limits))
    {
        return rejected<AdaptiveV2EpochChangeCommittedObservation>(
            AdaptiveV2ConvergenceWireError::invalid_limits);
    }
    if (payload.size() > limits.maximum_payload_bytes)
    {
        return rejected<AdaptiveV2EpochChangeCommittedObservation>(
            AdaptiveV2ConvergenceWireError::payload_too_large);
    }

    Reader reader(payload, AdaptiveV2ConvergenceWireError::truncated);
    reader.domain(
        kCommittedObservationDomain,
        AdaptiveV2ConvergenceWireError::invalid_domain);

    AdaptiveV2EpochChangeCommittedObservation observation;
    observation.schema_version = reader.integer<std::uint32_t>();
    if (observation.schema_version !=
        kAdaptiveV2ConvergenceObservationSchemaVersionV1)
    {
        return rejected<AdaptiveV2EpochChangeCommittedObservation>(
            AdaptiveV2ConvergenceWireError::unsupported_schema);
    }
    if (reader.integer<std::uint8_t>() != kCanonicalFlags)
    {
        return rejected<AdaptiveV2EpochChangeCommittedObservation>(
            AdaptiveV2ConvergenceWireError::noncanonical_encoding);
    }
    observation.claimed_source_replica_id =
        reader.integer<ReplicaID>();
    observation.identity = decode_identity(reader);

    const auto validation = validate_committed(observation);
    if (validation != AdaptiveV2ConvergenceWireError::none)
    {
        return rejected<AdaptiveV2EpochChangeCommittedObservation>(
            validation);
    }
    if (!reader.empty())
    {
        return rejected<AdaptiveV2EpochChangeCommittedObservation>(
            AdaptiveV2ConvergenceWireError::trailing_bytes);
    }
    if (encode_adaptive_v2_epoch_change_committed_observation(
            observation, limits) != payload)
    {
        return rejected<AdaptiveV2EpochChangeCommittedObservation>(
            AdaptiveV2ConvergenceWireError::noncanonical_encoding);
    }
    return {
        AdaptiveV2ConvergenceWireError::none,
        std::move(observation)};
}

AdaptiveV2EpochActivatedDecodeResult decode_activated_impl(
    const bytearray_t &payload,
    const AdaptiveV2ConvergenceWireLimits &limits)
{
    if (!valid_limits(limits))
    {
        return rejected<AdaptiveV2EpochActivatedObservation>(
            AdaptiveV2ConvergenceWireError::invalid_limits);
    }
    if (payload.size() > limits.maximum_payload_bytes)
    {
        return rejected<AdaptiveV2EpochActivatedObservation>(
            AdaptiveV2ConvergenceWireError::payload_too_large);
    }

    Reader reader(payload, AdaptiveV2ConvergenceWireError::truncated);
    reader.domain(
        kActivatedObservationDomain,
        AdaptiveV2ConvergenceWireError::invalid_domain);

    AdaptiveV2EpochActivatedObservation observation;
    observation.schema_version = reader.integer<std::uint32_t>();
    if (observation.schema_version !=
        kAdaptiveV2ConvergenceObservationSchemaVersionV1)
    {
        return rejected<AdaptiveV2EpochActivatedObservation>(
            AdaptiveV2ConvergenceWireError::unsupported_schema);
    }
    if (reader.integer<std::uint8_t>() != kCanonicalFlags)
    {
        return rejected<AdaptiveV2EpochActivatedObservation>(
            AdaptiveV2ConvergenceWireError::noncanonical_encoding);
    }
    observation.claimed_source_replica_id =
        reader.integer<ReplicaID>();
    observation.identity = decode_identity(reader);
    observation.activated_epoch_number =
        reader.integer<std::uint32_t>();
    observation.activated_epoch_digest = reader.digest();

    const auto validation = validate_activated(observation);
    if (validation != AdaptiveV2ConvergenceWireError::none)
    {
        return rejected<AdaptiveV2EpochActivatedObservation>(validation);
    }
    if (!reader.empty())
    {
        return rejected<AdaptiveV2EpochActivatedObservation>(
            AdaptiveV2ConvergenceWireError::trailing_bytes);
    }
    if (encode_adaptive_v2_epoch_activated_observation(
            observation, limits) != payload)
    {
        return rejected<AdaptiveV2EpochActivatedObservation>(
            AdaptiveV2ConvergenceWireError::noncanonical_encoding);
    }
    return {
        AdaptiveV2ConvergenceWireError::none,
        std::move(observation)};
}

} // namespace

bool AdaptiveV2EpochChangeIdentity::operator==(
    const AdaptiveV2EpochChangeIdentity &other) const noexcept
{
    return predecessor_epoch_number ==
               other.predecessor_epoch_number &&
           predecessor_epoch_digest ==
               other.predecessor_epoch_digest &&
           successor_epoch_number == other.successor_epoch_number &&
           successor_epoch_digest == other.successor_epoch_digest &&
           command_payload_digest == other.command_payload_digest &&
           command_block_height == other.command_block_height &&
           command_block_hash == other.command_block_hash &&
           activation_delay_blocks == other.activation_delay_blocks &&
           activation_height == other.activation_height;
}

bool AdaptiveV2EpochChangeIdentity::operator!=(
    const AdaptiveV2EpochChangeIdentity &other) const noexcept
{
    return !(*this == other);
}

const std::string &
adaptive_v2_epoch_change_committed_observation_domain() noexcept
{
    return kCommittedObservationDomain;
}

const std::string &
adaptive_v2_epoch_activated_observation_domain() noexcept
{
    return kActivatedObservationDomain;
}

bytearray_t encode_adaptive_v2_epoch_change_committed_observation(
    const AdaptiveV2EpochChangeCommittedObservation &observation,
    const AdaptiveV2ConvergenceWireLimits &limits)
{
    if (!valid_limits(limits))
    {
        throw std::invalid_argument(
            "adaptive-v2 convergence wire limit must be nonzero");
    }
    const auto validation = validate_committed(observation);
    if (validation != AdaptiveV2ConvergenceWireError::none)
        throw_encoding_error(validation);

    Writer writer(
        limits.maximum_payload_bytes,
        "adaptive-v2 committed observation exceeds byte limit");
    writer.domain(kCommittedObservationDomain);
    writer.integer(observation.schema_version);
    writer.integer(kCanonicalFlags);
    writer.integer(observation.claimed_source_replica_id);
    encode_identity(writer, observation.identity);
    return std::move(writer).finish();
}

AdaptiveV2EpochChangeCommittedDecodeResult
decode_adaptive_v2_epoch_change_committed_observation(
    const bytearray_t &payload,
    const AdaptiveV2ConvergenceWireLimits &limits) noexcept
{
    try
    {
        return decode_committed_impl(payload, limits);
    }
    catch (const WireFailure &failure)
    {
        return rejected<AdaptiveV2EpochChangeCommittedObservation>(
            failure.error);
    }
    catch (const std::bad_alloc &)
    {
        return rejected<AdaptiveV2EpochChangeCommittedObservation>(
            AdaptiveV2ConvergenceWireError::allocation_failure);
    }
    catch (...)
    {
        return rejected<AdaptiveV2EpochChangeCommittedObservation>(
            AdaptiveV2ConvergenceWireError::internal_failure);
    }
}

bytearray_t encode_adaptive_v2_epoch_activated_observation(
    const AdaptiveV2EpochActivatedObservation &observation,
    const AdaptiveV2ConvergenceWireLimits &limits)
{
    if (!valid_limits(limits))
    {
        throw std::invalid_argument(
            "adaptive-v2 convergence wire limit must be nonzero");
    }
    const auto validation = validate_activated(observation);
    if (validation != AdaptiveV2ConvergenceWireError::none)
        throw_encoding_error(validation);

    Writer writer(
        limits.maximum_payload_bytes,
        "adaptive-v2 activated observation exceeds byte limit");
    writer.domain(kActivatedObservationDomain);
    writer.integer(observation.schema_version);
    writer.integer(kCanonicalFlags);
    writer.integer(observation.claimed_source_replica_id);
    encode_identity(writer, observation.identity);
    writer.integer(observation.activated_epoch_number);
    writer.digest(
        observation.activated_epoch_digest,
        "adaptive-v2 activated epoch digest is not 32 bytes");
    return std::move(writer).finish();
}

AdaptiveV2EpochActivatedDecodeResult
decode_adaptive_v2_epoch_activated_observation(
    const bytearray_t &payload,
    const AdaptiveV2ConvergenceWireLimits &limits) noexcept
{
    try
    {
        return decode_activated_impl(payload, limits);
    }
    catch (const WireFailure &failure)
    {
        return rejected<AdaptiveV2EpochActivatedObservation>(
            failure.error);
    }
    catch (const std::bad_alloc &)
    {
        return rejected<AdaptiveV2EpochActivatedObservation>(
            AdaptiveV2ConvergenceWireError::allocation_failure);
    }
    catch (...)
    {
        return rejected<AdaptiveV2EpochActivatedObservation>(
            AdaptiveV2ConvergenceWireError::internal_failure);
    }
}

const opcode_t MsgAdaptiveV2EpochChangeCommittedObservation::opcode;
const opcode_t MsgAdaptiveV2EpochActivatedObservation::opcode;

MsgAdaptiveV2EpochChangeCommittedObservation::
    MsgAdaptiveV2EpochChangeCommittedObservation(
        const AdaptiveV2EpochChangeCommittedObservation &observation,
        const AdaptiveV2ConvergenceWireLimits &limits)
    : serialized(
          encode_adaptive_v2_epoch_change_committed_observation(
              observation, limits))
{}

MsgAdaptiveV2EpochChangeCommittedObservation::
    MsgAdaptiveV2EpochChangeCommittedObservation(
        DataStream &&serialized_payload)
    : serialized(std::move(serialized_payload))
{}

MsgAdaptiveV2EpochActivatedObservation::
    MsgAdaptiveV2EpochActivatedObservation(
        const AdaptiveV2EpochActivatedObservation &observation,
        const AdaptiveV2ConvergenceWireLimits &limits)
    : serialized(
          encode_adaptive_v2_epoch_activated_observation(
              observation, limits))
{}

MsgAdaptiveV2EpochActivatedObservation::
    MsgAdaptiveV2EpochActivatedObservation(
        DataStream &&serialized_payload)
    : serialized(std::move(serialized_payload))
{}

} // namespace hotstuff
