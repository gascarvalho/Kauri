/**
 * Canonical adaptive-v2 commit and activation observations.
 */

#ifndef HOTSTUFF_ADAPTIVE_V2_CONVERGENCE_WIRE_H_INCLUDED
#define HOTSTUFF_ADAPTIVE_V2_CONVERGENCE_WIRE_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>

#include "hotstuff/type.h"

namespace hotstuff
{

constexpr std::uint32_t
    kAdaptiveV2ConvergenceObservationSchemaVersionV1 = 1;

struct AdaptiveV2ConvergenceWireLimits
{
    std::size_t maximum_payload_bytes{512};
};

/**
 * Exact identity of one committed adaptive-v2 epoch-change command.
 *
 * activation_height is redundant by design. Canonical validation requires
 * it to equal command_block_height + activation_delay_blocks with checked
 * arithmetic, so observations cannot introduce a second schedule.
 */
struct AdaptiveV2EpochChangeIdentity
{
    std::uint32_t predecessor_epoch_number{0};
    uint256_t predecessor_epoch_digest;
    std::uint32_t successor_epoch_number{0};
    uint256_t successor_epoch_digest;
    uint256_t command_payload_digest;
    std::uint64_t command_block_height{0};
    uint256_t command_block_hash;
    std::uint64_t activation_delay_blocks{0};
    std::uint64_t activation_height{0};

    bool operator==(
        const AdaptiveV2EpochChangeIdentity &other) const noexcept;
    bool operator!=(
        const AdaptiveV2EpochChangeIdentity &other) const noexcept;
};

/**
 * Replica claim that the identified command is on its committed chain.
 *
 * The claimed source has no authentication authority. The receiving
 * transport must bind it to an authenticated replica identity.
 */
struct AdaptiveV2EpochChangeCommittedObservation
{
    std::uint32_t schema_version{
        kAdaptiveV2ConvergenceObservationSchemaVersionV1};
    ReplicaID claimed_source_replica_id{0};
    AdaptiveV2EpochChangeIdentity identity;
};

/**
 * Replica claim that the committed successor is now its active epoch.
 *
 * This is a new adaptive-v2 observation. It intentionally does not reuse the
 * legacy ActivationStatus/Arm agreement path and grants no activation power.
 */
struct AdaptiveV2EpochActivatedObservation
{
    std::uint32_t schema_version{
        kAdaptiveV2ConvergenceObservationSchemaVersionV1};
    ReplicaID claimed_source_replica_id{0};
    AdaptiveV2EpochChangeIdentity identity;
    std::uint32_t activated_epoch_number{0};
    uint256_t activated_epoch_digest;
};

enum class AdaptiveV2ConvergenceWireError : std::uint8_t
{
    none = 0,
    invalid_limits,
    payload_too_large,
    truncated,
    trailing_bytes,
    invalid_domain,
    unsupported_schema,
    zero_command_block_height,
    invalid_activation_delay,
    activation_height_overflow,
    inconsistent_activation_height,
    invalid_identity,
    noncanonical_encoding,
    allocation_failure,
    internal_failure,
};

template<typename Observation>
struct AdaptiveV2ConvergenceDecodeResult
{
    AdaptiveV2ConvergenceWireError error{
        AdaptiveV2ConvergenceWireError::none};
    std::optional<Observation> observation;

    explicit operator bool() const noexcept
    {
        return error == AdaptiveV2ConvergenceWireError::none &&
               observation.has_value();
    }
};

using AdaptiveV2EpochChangeCommittedDecodeResult =
    AdaptiveV2ConvergenceDecodeResult<
        AdaptiveV2EpochChangeCommittedObservation>;
using AdaptiveV2EpochActivatedDecodeResult =
    AdaptiveV2ConvergenceDecodeResult<
        AdaptiveV2EpochActivatedObservation>;

const std::string &
adaptive_v2_epoch_change_committed_observation_domain() noexcept;
const std::string &
adaptive_v2_epoch_activated_observation_domain() noexcept;

bytearray_t encode_adaptive_v2_epoch_change_committed_observation(
    const AdaptiveV2EpochChangeCommittedObservation &observation,
    const AdaptiveV2ConvergenceWireLimits &limits);

AdaptiveV2EpochChangeCommittedDecodeResult
decode_adaptive_v2_epoch_change_committed_observation(
    const bytearray_t &payload,
    const AdaptiveV2ConvergenceWireLimits &limits) noexcept;

bytearray_t encode_adaptive_v2_epoch_activated_observation(
    const AdaptiveV2EpochActivatedObservation &observation,
    const AdaptiveV2ConvergenceWireLimits &limits);

AdaptiveV2EpochActivatedDecodeResult
decode_adaptive_v2_epoch_activated_observation(
    const bytearray_t &payload,
    const AdaptiveV2ConvergenceWireLimits &limits) noexcept;

struct MsgAdaptiveV2EpochChangeCommittedObservation
{
    static const opcode_t opcode = 0x1C;
    DataStream serialized;

    MsgAdaptiveV2EpochChangeCommittedObservation(
        const AdaptiveV2EpochChangeCommittedObservation &observation,
        const AdaptiveV2ConvergenceWireLimits &limits);
    explicit MsgAdaptiveV2EpochChangeCommittedObservation(
        DataStream &&serialized_payload);
};

struct MsgAdaptiveV2EpochActivatedObservation
{
    static const opcode_t opcode = 0x1D;
    DataStream serialized;

    MsgAdaptiveV2EpochActivatedObservation(
        const AdaptiveV2EpochActivatedObservation &observation,
        const AdaptiveV2ConvergenceWireLimits &limits);
    explicit MsgAdaptiveV2EpochActivatedObservation(
        DataStream &&serialized_payload);
};

static_assert(
    MsgAdaptiveV2EpochChangeCommittedObservation::opcode !=
        MsgAdaptiveV2EpochActivatedObservation::opcode,
    "adaptive-v2 convergence observation opcodes must be distinct");

} // namespace hotstuff

#endif
