/**
 * Canonical bounded replica-to-manager adaptive-v2 readiness notice.
 */

#ifndef HOTSTUFF_ADAPTIVE_V2_READINESS_WIRE_H_INCLUDED
#define HOTSTUFF_ADAPTIVE_V2_READINESS_WIRE_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>

#include "hotstuff/configuration.h"

namespace hotstuff
{

constexpr std::uint32_t
    kAdaptiveV2ReadinessNoticeSchemaVersionV1 = 1;

struct AdaptiveV2ReadinessWireLimits
{
    std::size_t maximum_payload_bytes{256};
};

/**
 * One replica's claimed active runtime boundary.
 *
 * The claimed source has no authentication authority in this value. The
 * manager transport must map the mutual-TLS PeerId to a configured ReplicaID,
 * compare that ID with claimed_source_replica_id, and enforce a strictly
 * increasing source_sequence before recording readiness.
 *
 * This notice is observational manager input only. It is not a vote,
 * certificate, quorum contribution, activation authorization, or membership
 * update.
 */
struct AdaptiveV2ReadinessNotice
{
    std::uint32_t schema_version{
        kAdaptiveV2ReadinessNoticeSchemaVersionV1};
    ReplicaID claimed_source_replica_id{0};
    std::uint64_t source_sequence{0};
    ConfigurationId active_configuration;
    std::uint64_t activation_generation{0};
    std::uint64_t committed_height{0};
};

enum class AdaptiveV2ReadinessWireError : std::uint8_t
{
    none = 0,
    invalid_limits,
    payload_too_large,
    truncated,
    trailing_bytes,
    invalid_domain,
    unsupported_schema,
    zero_source_sequence,
    invalid_configuration_identity,
    zero_activation_generation,
    noncanonical_encoding,
    allocation_failure,
    internal_failure,
};

struct AdaptiveV2ReadinessDecodeResult
{
    AdaptiveV2ReadinessWireError error{
        AdaptiveV2ReadinessWireError::none};
    std::optional<AdaptiveV2ReadinessNotice> notice;

    explicit operator bool() const noexcept
    {
        return error == AdaptiveV2ReadinessWireError::none &&
               notice.has_value();
    }
};

const std::string &adaptive_v2_readiness_notice_domain() noexcept;

/**
 * Encode one structurally valid readiness claim in canonical big-endian form.
 */
bytearray_t encode_adaptive_v2_readiness_notice(
    const AdaptiveV2ReadinessNotice &notice,
    const AdaptiveV2ReadinessWireLimits &limits);

/**
 * Decode structural wire data only.
 *
 * Success grants no authentication, configuration, activation, membership,
 * voting, or quorum authority. The manager must cross-check the claimed
 * source and runtime fields against authenticated live state.
 */
AdaptiveV2ReadinessDecodeResult decode_adaptive_v2_readiness_notice(
    const bytearray_t &payload,
    const AdaptiveV2ReadinessWireLimits &limits) noexcept;

struct MsgAdaptiveV2ReadinessNotice
{
    static const opcode_t opcode = 0x1B;
    DataStream serialized;

    MsgAdaptiveV2ReadinessNotice(
        const AdaptiveV2ReadinessNotice &notice,
        const AdaptiveV2ReadinessWireLimits &limits);
    explicit MsgAdaptiveV2ReadinessNotice(
        DataStream &&serialized_payload);
};

static_assert(
    MsgAdaptiveV2ReadinessNotice::opcode < 0x12 ||
        MsgAdaptiveV2ReadinessNotice::opcode > 0x1A,
    "readiness opcode must remain distinct from adaptive/evidence opcodes");

} // namespace hotstuff

#endif
