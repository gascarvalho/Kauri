/**
 * Replica-local adaptive-v3 certified activation gate.
 *
 * The gate is deliberately separate from HotStuff voting and QC state.  It
 * installs a durable local vote fence at the scheduled boundary before it
 * exposes a signed readiness observation, and it changes configurations only
 * after a fixed-membership readiness certificate has been verified.
 */

#ifndef HOTSTUFF_ADAPTIVE_V3_ACTIVATION_READINESS_H_INCLUDED
#define HOTSTUFF_ADAPTIVE_V3_ACTIVATION_READINESS_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include "hotstuff/activation_readiness_wire.h"

namespace hotstuff
{

constexpr std::uint32_t kAdaptiveV3ActivationReadinessSchemaVersionV1 =
    kActivationReadinessSchemaVersionV1;

using AdaptiveV3ActivationReadinessLimits = ActivationReadinessWireLimits;
using AdaptiveV3ActivationReadyIdentity = ActivationReadyIdentityV1;
using AdaptiveV3ActivationReadyObservation = ActivationReadyObservationV1;
using AdaptiveV3ActivationReadinessCertificate =
    ActivationReadinessCertificateV1;

struct AdaptiveV3ActivationSchedule
{
    uint256_t membership_digest;
    std::uint32_t predecessor_epoch_number{0};
    uint256_t predecessor_epoch_digest;
    std::uint32_t successor_epoch_number{0};
    uint256_t successor_epoch_digest;
    std::uint64_t successor_activation_generation{0};
    uint256_t command_payload_digest;
    std::uint64_t command_block_height{0};
    uint256_t command_block_hash;
    std::uint64_t activation_delay_blocks{0};
    std::uint64_t activation_height{0};
};

struct AdaptiveV3ReadinessMember
{
    ReplicaID replica_id{0};
    PubKeyBLS public_key;
};

enum class AdaptiveV3ActivationReadinessWireError : std::uint8_t
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
    duplicate_signer,
    too_many_members,
    malformed_signature,
    allocation_failure,
    internal_failure,
};

template<typename Value>
struct AdaptiveV3ActivationReadinessDecodeResult
{
    AdaptiveV3ActivationReadinessWireError error{
        AdaptiveV3ActivationReadinessWireError::none};
    std::optional<Value> value;

    explicit operator bool() const noexcept
    {
        return error == AdaptiveV3ActivationReadinessWireError::none &&
               value.has_value();
    }
};

using AdaptiveV3ActivationReadyObservationDecodeResult =
    AdaptiveV3ActivationReadinessDecodeResult<
        AdaptiveV3ActivationReadyObservation>;
using AdaptiveV3ActivationReadinessCertificateDecodeResult =
    AdaptiveV3ActivationReadinessDecodeResult<
        AdaptiveV3ActivationReadinessCertificate>;

const std::string &
adaptive_v3_activation_ready_observation_domain() noexcept;
const std::string &
adaptive_v3_activation_readiness_certificate_domain() noexcept;

bytearray_t encode_adaptive_v3_activation_ready_observation(
    const AdaptiveV3ActivationReadyObservation &observation,
    const AdaptiveV3ActivationReadinessLimits &limits);
AdaptiveV3ActivationReadyObservationDecodeResult
decode_adaptive_v3_activation_ready_observation(
    const bytearray_t &payload,
    const AdaptiveV3ActivationReadinessLimits &limits) noexcept;

bytearray_t encode_adaptive_v3_activation_readiness_certificate(
    const AdaptiveV3ActivationReadinessCertificate &certificate,
    const AdaptiveV3ActivationReadinessLimits &limits);
AdaptiveV3ActivationReadinessCertificateDecodeResult
decode_adaptive_v3_activation_readiness_certificate(
    const bytearray_t &payload,
    const AdaptiveV3ActivationReadinessLimits &limits) noexcept;

AdaptiveV3ActivationReadinessCertificate
make_adaptive_v3_activation_readiness_certificate(
    const AdaptiveV3ActivationReadyIdentity &identity,
    std::vector<AdaptiveV3ActivationReadyObservation> observations,
    const AdaptiveV3ActivationReadinessLimits &limits);

enum class AdaptiveV3CertifiedActivationState : std::uint8_t
{
    awaiting_boundary = 1,
    prepared,
    active,
    blocked,
};

enum class AdaptiveV3BoundaryDisposition : std::uint8_t
{
    waiting = 1,
    prepared,
    already_prepared,
    activated_from_buffered_certificate,
    rejected,
};

struct AdaptiveV3BoundaryResult
{
    AdaptiveV3BoundaryDisposition disposition{
        AdaptiveV3BoundaryDisposition::rejected};
    std::optional<AdaptiveV3ActivationReadyObservation> observation;
};

enum class AdaptiveV3CertificateDisposition : std::uint8_t
{
    accepted = 1,
    duplicate,
    buffered_early,
    rejected_below_quorum,
    rejected_noncanonical,
    rejected_stale,
    rejected_wrong_identity,
    rejected_mixed_identity,
    rejected_invalid_signature,
    rejected_nonmember,
    terminal,
};

class AdaptiveV3CertifiedActivationGate final
{
public:
    AdaptiveV3CertifiedActivationGate(
        AdaptiveV3ActivationSchedule schedule,
        ReplicaID local_replica,
        std::shared_ptr<const PrivKeyBLS> local_private_key,
        std::vector<AdaptiveV3ReadinessMember> membership,
        std::size_t fixed_quorum);
    ~AdaptiveV3CertifiedActivationGate();

    AdaptiveV3CertifiedActivationGate(
        const AdaptiveV3CertifiedActivationGate &) = delete;
    AdaptiveV3CertifiedActivationGate &operator=(
        const AdaptiveV3CertifiedActivationGate &) = delete;
    AdaptiveV3CertifiedActivationGate(
        AdaptiveV3CertifiedActivationGate &&) = delete;
    AdaptiveV3CertifiedActivationGate &operator=(
        AdaptiveV3CertifiedActivationGate &&) = delete;

    AdaptiveV3BoundaryResult observe_predecessor_commit(
        std::uint64_t committed_height,
        const ConfigurationId &configuration,
        std::uint64_t generation,
        const uint256_t &block_hash,
        std::uint64_t source_sequence,
        std::uint64_t monotonic_raw_ns) noexcept;

    AdaptiveV3CertificateDisposition observe_certificate(
        const AdaptiveV3ActivationReadinessCertificate &certificate)
        noexcept;

    AdaptiveV3CertifiedActivationState state() const noexcept;
    const ConfigurationId &active_configuration() const noexcept;
    std::uint64_t active_generation() const noexcept;
    bool vote_fence_engaged() const noexcept;
    bool may_authorize_vote(
        const ConfigurationId &configuration,
        std::uint64_t generation) const noexcept;
    std::optional<std::uint64_t>
    scheduled_readiness_height() const noexcept;
    std::optional<std::uint64_t>
    certificate_apply_committed_height() const noexcept;

private:
    struct State;
    std::unique_ptr<State> state_;
};

} // namespace hotstuff

#endif
