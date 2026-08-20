/**
 * Canonical signed adaptive-v3 activation-readiness messages.
 *
 * These messages are availability evidence only.  They are deliberately
 * separate from HotStuff votes, partial certificates, and quorum certificates.
 */

#ifndef HOTSTUFF_ACTIVATION_READINESS_WIRE_H_INCLUDED
#define HOTSTUFF_ACTIVATION_READINESS_WIRE_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "hotstuff/crypto.h"
#include "hotstuff/epoch_activation.h"
#include "hotstuff/epoch_wire.h"

namespace hotstuff
{

constexpr std::uint32_t kActivationReadinessSchemaVersionV1 = 1;

struct ActivationReadinessWireLimits
{
    std::size_t maximum_payload_bytes{4096};
    std::size_t maximum_members{0};
};

struct ActivationReadyIdentityV1
{
    std::uint32_t schema_version{kActivationReadinessSchemaVersionV1};
    uint256_t membership_digest;
    ConfigurationId predecessor_boundary_configuration;
    std::uint64_t predecessor_boundary_generation{0};
    ConfigurationId successor_configuration;
    std::uint64_t successor_activation_generation{0};
    uint256_t command_payload_digest;
    std::uint64_t command_block_height{0};
    uint256_t command_block_hash;
    std::uint64_t activation_delay_blocks{0};
    std::uint64_t activation_height{0};
    uint256_t activation_boundary_block_hash;

    bool operator==(const ActivationReadyIdentityV1 &other) const noexcept;
    bool operator!=(const ActivationReadyIdentityV1 &other) const noexcept;
};

struct ActivationReadyObservationV1
{
    ActivationReadyIdentityV1 identity;
    ReplicaID signer_replica_id{0};
    std::uint64_t signer_source_sequence{0};
    std::uint64_t signer_monotonic_raw_ns{0};
    bool vote_fence_engaged{false};
    SigSecBLS signature;
};

struct ActivationReadinessCertificateV1
{
    std::uint32_t schema_version{kActivationReadinessSchemaVersionV1};
    ActivationReadyIdentityV1 identity;
    std::vector<ActivationReadyObservationV1> observations;
    uint256_t certificate_digest;
};

enum class ActivationReadinessAckDisposition : std::uint8_t
{
    positive = 1,
    permanent_rejection = 2,
};

struct ActivationReadinessAckV1
{
    std::uint32_t schema_version{kActivationReadinessSchemaVersionV1};
    opcode_t acknowledged_opcode{0};
    ReplicaID recipient_replica_id{0};
    ActivationReadyIdentityV1 identity;
    uint256_t certificate_digest;
    uint256_t payload_digest;
    ActivationReadinessAckDisposition disposition{
        ActivationReadinessAckDisposition::positive};
};

enum class ActivationReadinessWireError : std::uint8_t
{
    none = 0,
    invalid_limits,
    payload_too_large,
    truncated,
    trailing_bytes,
    invalid_domain,
    unsupported_schema,
    noncanonical_encoding,
    invalid_identity,
    invalid_observation,
    invalid_certificate,
    invalid_acknowledgement,
    too_many_observations,
    allocation_failure,
    internal_failure,
};

template<typename Value>
struct ActivationReadinessDecodeResult
{
    ActivationReadinessWireError error{ActivationReadinessWireError::none};
    std::optional<Value> value;
    explicit operator bool() const noexcept
    {
        return error == ActivationReadinessWireError::none && value.has_value();
    }
};

using ActivationReadyObservationDecodeResult =
    ActivationReadinessDecodeResult<ActivationReadyObservationV1>;
using ActivationReadyIdentityDecodeResult =
    ActivationReadinessDecodeResult<ActivationReadyIdentityV1>;
using ActivationReadinessCertificateDecodeResult =
    ActivationReadinessDecodeResult<ActivationReadinessCertificateV1>;
using ActivationReadinessAckDecodeResult =
    ActivationReadinessDecodeResult<ActivationReadinessAckV1>;

const std::string &activation_ready_observation_domain() noexcept;
const std::string &activation_ready_identity_domain() noexcept;
const std::string &activation_readiness_certificate_domain() noexcept;
const std::string &activation_readiness_ack_domain() noexcept;

uint256_t canonical_activation_readiness_membership_digest(
    const std::vector<std::pair<ReplicaID, PubKeyBLS>> &members);

uint256_t activation_ready_observation_digest(
    const ActivationReadyObservationV1 &observation);
uint256_t activation_readiness_certificate_digest(
    const ActivationReadinessCertificateV1 &certificate);
uint256_t activation_readiness_ack_payload_digest(
    opcode_t acknowledged_opcode,
    const bytearray_t &canonical_payload);

ActivationReadyObservationV1 sign_activation_ready_observation(
    ActivationReadyIdentityV1 identity,
    ReplicaID signer_replica_id,
    std::uint64_t signer_source_sequence,
    std::uint64_t signer_monotonic_raw_ns,
    const PrivKeyBLS &private_key);

ActivationReadinessCertificateV1 make_activation_readiness_certificate(
    ActivationReadyIdentityV1 identity,
    std::vector<ActivationReadyObservationV1> observations);

bytearray_t encode_activation_ready_identity_v1(
    const ActivationReadyIdentityV1 &identity,
    const ActivationReadinessWireLimits &limits);
ActivationReadyIdentityDecodeResult decode_activation_ready_identity_v1(
    const bytearray_t &payload,
    const ActivationReadinessWireLimits &limits) noexcept;

bytearray_t encode_activation_ready_observation(
    const ActivationReadyObservationV1 &observation,
    const ActivationReadinessWireLimits &limits);
ActivationReadyObservationDecodeResult decode_activation_ready_observation(
    const bytearray_t &payload,
    const ActivationReadinessWireLimits &limits) noexcept;

bytearray_t encode_activation_readiness_certificate(
    const ActivationReadinessCertificateV1 &certificate,
    const ActivationReadinessWireLimits &limits);
ActivationReadinessCertificateDecodeResult decode_activation_readiness_certificate(
    const bytearray_t &payload,
    const ActivationReadinessWireLimits &limits) noexcept;

bytearray_t encode_activation_readiness_ack(
    const ActivationReadinessAckV1 &acknowledgement,
    const ActivationReadinessWireLimits &limits);
ActivationReadinessAckDecodeResult decode_activation_readiness_ack(
    const bytearray_t &payload,
    const ActivationReadinessWireLimits &limits) noexcept;

bool verify_activation_ready_observation(
    const ActivationReadyObservationV1 &observation,
    const PubKeyBLS &public_key) noexcept;

/**
 * Verify fixed-membership signatures and the protocol quorum only.  The
 * focused operational release cardinality R is intentionally not an input.
 */
bool verify_activation_readiness_certificate(
    const ActivationReadinessCertificateV1 &certificate,
    const ActivationReadyIdentityV1 &expected_identity,
    const uint256_t &expected_membership_digest,
    const std::vector<std::pair<ReplicaID, PubKeyBLS>> &members) noexcept;

struct MsgActivationReadyObservation
{
    static const opcode_t opcode = 0x21;
    DataStream serialized;
    MsgActivationReadyObservation(const ActivationReadyObservationV1 &, const ActivationReadinessWireLimits &);
    explicit MsgActivationReadyObservation(DataStream &&value);
};
struct MsgActivationReadinessCertificate
{
    static const opcode_t opcode = 0x22;
    DataStream serialized;
    MsgActivationReadinessCertificate(const ActivationReadinessCertificateV1 &, const ActivationReadinessWireLimits &);
    explicit MsgActivationReadinessCertificate(DataStream &&value);
};
struct MsgActivationReadinessAck
{
    static const opcode_t opcode = 0x23;
    DataStream serialized;
    MsgActivationReadinessAck(const ActivationReadinessAckV1 &, const ActivationReadinessWireLimits &);
    explicit MsgActivationReadinessAck(DataStream &&value);
};

} // namespace hotstuff

#endif
