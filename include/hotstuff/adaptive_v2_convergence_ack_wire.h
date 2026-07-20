/**
 * Canonical acknowledgements for adaptive-v2 convergence observations.
 */

#ifndef HOTSTUFF_ADAPTIVE_V2_CONVERGENCE_ACK_WIRE_H_INCLUDED
#define HOTSTUFF_ADAPTIVE_V2_CONVERGENCE_ACK_WIRE_H_INCLUDED

#include <cstdint>
#include <optional>
#include <string>

#include "hotstuff/adaptive_v2_convergence_wire.h"

namespace hotstuff
{

constexpr std::uint32_t kAdaptiveV2ConvergenceAckSchemaVersionV1 = 1;

enum class AdaptiveV2ConvergenceObservationKind : std::uint8_t
{
    commit = 1,
    activation = 2,
};

enum class AdaptiveV2ConvergenceAckDisposition : std::uint8_t
{
    positive = 1,
    permanent_rejection = 2,
};

struct AdaptiveV2ConvergenceObservationAck
{
    std::uint32_t schema_version{
        kAdaptiveV2ConvergenceAckSchemaVersionV1};
    ReplicaID target_replica_id{0};
    AdaptiveV2ConvergenceObservationKind observation_kind{
        AdaptiveV2ConvergenceObservationKind::commit};
    AdaptiveV2EpochChangeIdentity identity;
    uint256_t observation_digest;
    AdaptiveV2ConvergenceAckDisposition disposition{
        AdaptiveV2ConvergenceAckDisposition::positive};
};

enum class AdaptiveV2ConvergenceAckWireError : std::uint8_t
{
    none = 0,
    invalid_limits,
    payload_too_large,
    truncated,
    trailing_bytes,
    invalid_domain,
    unsupported_schema,
    invalid_observation_kind,
    invalid_identity,
    invalid_observation_digest,
    invalid_disposition,
    noncanonical_encoding,
    allocation_failure,
    internal_failure,
};

struct AdaptiveV2ConvergenceAckDecodeResult
{
    AdaptiveV2ConvergenceAckWireError error{
        AdaptiveV2ConvergenceAckWireError::none};
    std::optional<AdaptiveV2ConvergenceObservationAck> acknowledgement;

    explicit operator bool() const noexcept
    {
        return error == AdaptiveV2ConvergenceAckWireError::none &&
               acknowledgement.has_value();
    }
};

const std::string &
adaptive_v2_convergence_observation_ack_domain() noexcept;

/**
 * Bind an acknowledgement to one observation opcode and its exact canonical
 * bytes. Only the two adaptive-v2 convergence observation opcodes are valid.
 */
uint256_t adaptive_v2_convergence_observation_digest(
    opcode_t observation_opcode,
    const bytearray_t &canonical_observation);

bytearray_t encode_adaptive_v2_convergence_observation_ack(
    const AdaptiveV2ConvergenceObservationAck &acknowledgement,
    const AdaptiveV2ConvergenceWireLimits &limits);

AdaptiveV2ConvergenceAckDecodeResult
decode_adaptive_v2_convergence_observation_ack(
    const bytearray_t &payload,
    const AdaptiveV2ConvergenceWireLimits &limits) noexcept;

struct MsgAdaptiveV2ConvergenceObservationAck
{
    static const opcode_t opcode = 0x1E;
    DataStream serialized;

    MsgAdaptiveV2ConvergenceObservationAck(
        const AdaptiveV2ConvergenceObservationAck &acknowledgement,
        const AdaptiveV2ConvergenceWireLimits &limits);
    explicit MsgAdaptiveV2ConvergenceObservationAck(
        DataStream &&serialized_payload);
};

static_assert(
    MsgAdaptiveV2ConvergenceObservationAck::opcode !=
        MsgAdaptiveV2EpochChangeCommittedObservation::opcode &&
        MsgAdaptiveV2ConvergenceObservationAck::opcode !=
            MsgAdaptiveV2EpochActivatedObservation::opcode,
    "adaptive-v2 convergence acknowledgement opcode must be distinct");

} // namespace hotstuff

#endif
