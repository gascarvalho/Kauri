#include "hotstuff/adaptive_v2_convergence_ack_wire.h"

#include <limits>
#include <new>
#include <stdexcept>
#include <utility>

#include "detail/canonical_wire_codec.h"

namespace hotstuff
{
namespace
{

const std::string kAcknowledgementDomain =
    "kauri-adaptive-v2-convergence-observation-ack-v1";
const std::string kObservationDigestDomain =
    "kauri-adaptive-v2-convergence-observation-digest-v1";
constexpr std::uint8_t kCanonicalFlags = 0;

struct WireFailure
{
    AdaptiveV2ConvergenceAckWireError error;
};

using Writer = detail::CanonicalWireWriter;
using Reader = detail::CanonicalWireReader<
    WireFailure,
    AdaptiveV2ConvergenceAckWireError>;

bool valid_limits(const AdaptiveV2ConvergenceWireLimits &limits) noexcept
{
    return limits.maximum_payload_bytes != 0;
}

bool valid_kind(AdaptiveV2ConvergenceObservationKind kind) noexcept
{
    return kind == AdaptiveV2ConvergenceObservationKind::commit ||
           kind == AdaptiveV2ConvergenceObservationKind::activation;
}

bool valid_disposition(
    AdaptiveV2ConvergenceAckDisposition disposition) noexcept
{
    return disposition == AdaptiveV2ConvergenceAckDisposition::positive ||
           disposition ==
               AdaptiveV2ConvergenceAckDisposition::permanent_rejection;
}

bool valid_identity(const AdaptiveV2EpochChangeIdentity &identity) noexcept
{
    if (identity.command_block_height == 0 ||
        identity.activation_delay_blocks == 0 ||
        identity.command_block_height >
            std::numeric_limits<std::uint64_t>::max() -
                identity.activation_delay_blocks)
    {
        return false;
    }
    return identity.activation_height ==
               identity.command_block_height +
                   identity.activation_delay_blocks &&
           identity.predecessor_epoch_number !=
               std::numeric_limits<std::uint32_t>::max() &&
           identity.successor_epoch_number ==
               identity.predecessor_epoch_number + 1 &&
           identity.predecessor_epoch_digest != uint256_t{} &&
           identity.successor_epoch_digest != uint256_t{} &&
           identity.command_payload_digest != uint256_t{} &&
           identity.command_block_hash != uint256_t{} &&
           identity.predecessor_epoch_digest !=
               identity.successor_epoch_digest;
}

AdaptiveV2ConvergenceAckWireError validate_ack(
    const AdaptiveV2ConvergenceObservationAck &acknowledgement) noexcept
{
    if (acknowledgement.schema_version !=
        kAdaptiveV2ConvergenceAckSchemaVersionV1)
    {
        return AdaptiveV2ConvergenceAckWireError::unsupported_schema;
    }
    if (!valid_kind(acknowledgement.observation_kind))
    {
        return AdaptiveV2ConvergenceAckWireError::invalid_observation_kind;
    }
    if (!valid_identity(acknowledgement.identity))
        return AdaptiveV2ConvergenceAckWireError::invalid_identity;
    if (acknowledgement.observation_digest == uint256_t{})
    {
        return AdaptiveV2ConvergenceAckWireError::
            invalid_observation_digest;
    }
    if (!valid_disposition(acknowledgement.disposition))
        return AdaptiveV2ConvergenceAckWireError::invalid_disposition;
    return AdaptiveV2ConvergenceAckWireError::none;
}

void encode_identity(
    Writer &writer,
    const AdaptiveV2EpochChangeIdentity &identity)
{
    writer.integer(identity.predecessor_epoch_number);
    writer.digest(
        identity.predecessor_epoch_digest,
        "adaptive-v2 ACK predecessor digest is not 32 bytes");
    writer.integer(identity.successor_epoch_number);
    writer.digest(
        identity.successor_epoch_digest,
        "adaptive-v2 ACK successor digest is not 32 bytes");
    writer.digest(
        identity.command_payload_digest,
        "adaptive-v2 ACK command digest is not 32 bytes");
    writer.integer(identity.command_block_height);
    writer.digest(
        identity.command_block_hash,
        "adaptive-v2 ACK command block hash is not 32 bytes");
    writer.integer(identity.activation_delay_blocks);
    writer.integer(identity.activation_height);
}

AdaptiveV2EpochChangeIdentity decode_identity(Reader &reader)
{
    AdaptiveV2EpochChangeIdentity identity;
    identity.predecessor_epoch_number = reader.integer<std::uint32_t>();
    identity.predecessor_epoch_digest = reader.digest();
    identity.successor_epoch_number = reader.integer<std::uint32_t>();
    identity.successor_epoch_digest = reader.digest();
    identity.command_payload_digest = reader.digest();
    identity.command_block_height = reader.integer<std::uint64_t>();
    identity.command_block_hash = reader.digest();
    identity.activation_delay_blocks = reader.integer<std::uint64_t>();
    identity.activation_height = reader.integer<std::uint64_t>();
    return identity;
}

[[noreturn]] void throw_encoding_error(
    AdaptiveV2ConvergenceAckWireError error)
{
    switch (error)
    {
    case AdaptiveV2ConvergenceAckWireError::unsupported_schema:
    case AdaptiveV2ConvergenceAckWireError::invalid_observation_kind:
    case AdaptiveV2ConvergenceAckWireError::invalid_identity:
    case AdaptiveV2ConvergenceAckWireError::invalid_observation_digest:
    case AdaptiveV2ConvergenceAckWireError::invalid_disposition:
        throw std::invalid_argument(
            "adaptive-v2 convergence acknowledgement is not canonical");
    default:
        throw std::logic_error(
            "unexpected adaptive-v2 acknowledgement validation error");
    }
}

AdaptiveV2ConvergenceAckDecodeResult rejected(
    AdaptiveV2ConvergenceAckWireError error) noexcept
{
    return {error, std::nullopt};
}

AdaptiveV2ConvergenceAckDecodeResult decode_impl(
    const bytearray_t &payload,
    const AdaptiveV2ConvergenceWireLimits &limits)
{
    if (!valid_limits(limits))
        return rejected(AdaptiveV2ConvergenceAckWireError::invalid_limits);
    if (payload.size() > limits.maximum_payload_bytes)
    {
        return rejected(
            AdaptiveV2ConvergenceAckWireError::payload_too_large);
    }

    Reader reader(payload, AdaptiveV2ConvergenceAckWireError::truncated);
    reader.domain(
        kAcknowledgementDomain,
        AdaptiveV2ConvergenceAckWireError::invalid_domain);

    AdaptiveV2ConvergenceObservationAck acknowledgement;
    acknowledgement.schema_version = reader.integer<std::uint32_t>();
    if (reader.integer<std::uint8_t>() != kCanonicalFlags)
    {
        return rejected(
            AdaptiveV2ConvergenceAckWireError::noncanonical_encoding);
    }
    acknowledgement.target_replica_id = reader.integer<ReplicaID>();
    acknowledgement.observation_kind =
        static_cast<AdaptiveV2ConvergenceObservationKind>(
            reader.integer<std::uint8_t>());
    acknowledgement.identity = decode_identity(reader);
    acknowledgement.observation_digest = reader.digest();
    acknowledgement.disposition =
        static_cast<AdaptiveV2ConvergenceAckDisposition>(
            reader.integer<std::uint8_t>());

    const auto validation = validate_ack(acknowledgement);
    if (validation != AdaptiveV2ConvergenceAckWireError::none)
        return rejected(validation);
    if (!reader.empty())
    {
        return rejected(
            AdaptiveV2ConvergenceAckWireError::trailing_bytes);
    }
    if (encode_adaptive_v2_convergence_observation_ack(
            acknowledgement, limits) != payload)
    {
        return rejected(
            AdaptiveV2ConvergenceAckWireError::noncanonical_encoding);
    }
    return {
        AdaptiveV2ConvergenceAckWireError::none,
        std::move(acknowledgement)};
}

} // namespace

const std::string &
adaptive_v2_convergence_observation_ack_domain() noexcept
{
    return kAcknowledgementDomain;
}

uint256_t adaptive_v2_convergence_observation_digest(
    opcode_t observation_opcode,
    const bytearray_t &canonical_observation)
{
    if (observation_opcode !=
            MsgAdaptiveV2EpochChangeCommittedObservation::opcode &&
        observation_opcode !=
            MsgAdaptiveV2EpochActivatedObservation::opcode)
    {
        throw std::invalid_argument(
            "unsupported adaptive-v2 convergence observation opcode");
    }

    Writer writer;
    writer.domain(kObservationDigestDomain);
    writer.integer(observation_opcode);
    writer.integer(static_cast<std::uint64_t>(canonical_observation.size()));
    writer.bytes(canonical_observation);
    return DataStream(std::move(writer).finish()).get_hash();
}

bytearray_t encode_adaptive_v2_convergence_observation_ack(
    const AdaptiveV2ConvergenceObservationAck &acknowledgement,
    const AdaptiveV2ConvergenceWireLimits &limits)
{
    if (!valid_limits(limits))
    {
        throw std::invalid_argument(
            "adaptive-v2 convergence ACK wire limit must be nonzero");
    }
    const auto validation = validate_ack(acknowledgement);
    if (validation != AdaptiveV2ConvergenceAckWireError::none)
        throw_encoding_error(validation);

    Writer writer(
        limits.maximum_payload_bytes,
        "adaptive-v2 convergence ACK exceeds byte limit");
    writer.domain(kAcknowledgementDomain);
    writer.integer(acknowledgement.schema_version);
    writer.integer(kCanonicalFlags);
    writer.integer(acknowledgement.target_replica_id);
    writer.integer(
        static_cast<std::uint8_t>(acknowledgement.observation_kind));
    encode_identity(writer, acknowledgement.identity);
    writer.digest(
        acknowledgement.observation_digest,
        "adaptive-v2 convergence observation digest is not 32 bytes");
    writer.integer(
        static_cast<std::uint8_t>(acknowledgement.disposition));
    return std::move(writer).finish();
}

AdaptiveV2ConvergenceAckDecodeResult
decode_adaptive_v2_convergence_observation_ack(
    const bytearray_t &payload,
    const AdaptiveV2ConvergenceWireLimits &limits) noexcept
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
            AdaptiveV2ConvergenceAckWireError::allocation_failure);
    }
    catch (...)
    {
        return rejected(
            AdaptiveV2ConvergenceAckWireError::internal_failure);
    }
}

const opcode_t MsgAdaptiveV2ConvergenceObservationAck::opcode;

MsgAdaptiveV2ConvergenceObservationAck::
    MsgAdaptiveV2ConvergenceObservationAck(
        const AdaptiveV2ConvergenceObservationAck &acknowledgement,
        const AdaptiveV2ConvergenceWireLimits &limits)
    : serialized(encode_adaptive_v2_convergence_observation_ack(
          acknowledgement, limits))
{}

MsgAdaptiveV2ConvergenceObservationAck::
    MsgAdaptiveV2ConvergenceObservationAck(
        DataStream &&serialized_payload)
    : serialized(std::move(serialized_payload))
{}

} // namespace hotstuff
