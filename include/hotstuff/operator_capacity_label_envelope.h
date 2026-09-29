/**
 * Stage-A immutable, issuer-signed capacity labels for a prospective epoch.
 *
 * This input deliberately ends before any live responsiveness evidence exists.
 * A later authorization must independently bind the observed baseline before
 * an epoch is activated.
 */
#ifndef HOTSTUFF_OPERATOR_CAPACITY_LABEL_ENVELOPE_H_INCLUDED
#define HOTSTUFF_OPERATOR_CAPACITY_LABEL_ENVELOPE_H_INCLUDED

#include <cstdint>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "hotstuff/crypto.h"
#include "hotstuff/operator_capacity_policy.h"

namespace hotstuff
{

constexpr std::uint32_t kOperatorCapacityLabelEnvelopeSchemaVersion = 1;
constexpr std::size_t kOperatorCapacityLabelEnvelopeSignatureBytes = 64;

struct OperatorCapacityLabelEnvelopeWireLimits
{
    std::size_t maximum_payload_bytes{32 * 1024};
};

enum class OperatorCapacityLabelEnvelopeWireError : std::uint8_t
{
    none = 0, invalid_limits, payload_too_large, truncated, invalid_domain,
    unsupported_schema, malformed_signature, trailing_bytes,
    noncanonical_encoding, allocation_failure, internal_failure,
};

/** Externally pinned issuer material; it is not accepted from the envelope. */
struct OperatorCapacityLabelEnvelopeIssuer
{
    std::uint32_t issuer_id{0};
    std::string issuer_reference;
    uint256_t approved_capacity_digest;
    PubKeySecp256k1 public_key;
};

/**
 * A prelaunch signed input. `baseline_topology_digest` identifies the exact
 * Epoch 0 membership and tree ordering; labels are the canonical snapshot
 * bytes and digest for that same predecessor identity.
 */
struct OperatorCapacityLabelEnvelope
{
    std::uint32_t schema_version{kOperatorCapacityLabelEnvelopeSchemaVersion};
    std::uint32_t issuer_id{0};
    std::string issuer_reference;
    std::vector<ReplicaID> membership;
    AdaptationEpochId epoch0;
    uint256_t baseline_topology_digest;
    OperatorCapacityArm arm{OperatorCapacityArm::fast_priority_treatment};
    OperatorCapacitySnapshot capacity_snapshot;
    SigSecp256k1 signature;
};

/** Canonical identity for the exact E0 context and tree ordering. */
uint256_t operator_capacity_baseline_topology_digest(
    const AdaptationEpochId &epoch0,
    const std::vector<ReplicaID> &membership,
    const std::vector<EpochTreeDefinition> &trees);

bytearray_t canonical_operator_capacity_label_envelope_bytes(
    const OperatorCapacityLabelEnvelope &envelope);
uint256_t operator_capacity_label_envelope_digest(
    const OperatorCapacityLabelEnvelope &envelope);
OperatorCapacityLabelEnvelope sign_operator_capacity_label_envelope(
    const std::vector<ReplicaID> &membership,
    const AdaptationEpochId &epoch0,
    const uint256_t &baseline_topology_digest,
    OperatorCapacityArm arm,
    const OperatorCapacitySnapshot &capacity_snapshot,
    std::uint32_t issuer_id,
    const PrivKeySecp256k1 &private_key);
bytearray_t encode_operator_capacity_label_envelope(
    const OperatorCapacityLabelEnvelope &envelope,
    const OperatorCapacityLabelEnvelopeWireLimits &limits);

struct OperatorCapacityLabelEnvelopeDecodeResult
{
    OperatorCapacityLabelEnvelopeWireError error{
        OperatorCapacityLabelEnvelopeWireError::internal_failure};
    std::optional<OperatorCapacityLabelEnvelope> value;
    explicit operator bool() const noexcept
    { return error == OperatorCapacityLabelEnvelopeWireError::none && value.has_value(); }
};

OperatorCapacityLabelEnvelopeDecodeResult decode_operator_capacity_label_envelope(
    const bytearray_t &payload,
    const OperatorCapacityLabelEnvelopeWireLimits &limits) noexcept;

class VerifiedOperatorCapacityLabelEnvelope final
{
public:
    const std::vector<ReplicaID> &membership() const noexcept { return envelope_.membership; }
    const AdaptationEpochId &epoch0() const noexcept { return envelope_.epoch0; }
    const uint256_t &baseline_topology_digest() const noexcept
    { return envelope_.baseline_topology_digest; }
    OperatorCapacityArm arm() const noexcept { return envelope_.arm; }
    const OperatorCapacitySnapshot &capacity_snapshot() const noexcept
    { return envelope_.capacity_snapshot; }
    /** Immutable signed-input identity for the later consumption record. */
    const uint256_t &digest() const noexcept { return digest_; }

private:
    friend std::optional<VerifiedOperatorCapacityLabelEnvelope>
    verify_operator_capacity_label_envelope(
        const OperatorCapacityLabelEnvelope &,
        const OperatorCapacityLabelEnvelopeIssuer &,
        const std::vector<ReplicaID> &, const AdaptationEpochId &,
        const uint256_t &, std::uint64_t) noexcept;

    explicit VerifiedOperatorCapacityLabelEnvelope(
        OperatorCapacityLabelEnvelope envelope)
        : digest_(operator_capacity_label_envelope_digest(envelope)),
          envelope_(std::move(envelope)) {}
    uint256_t digest_;
    OperatorCapacityLabelEnvelope envelope_;
};

/**
 * Verify an externally pinned issuer/digest, exact E0 identity and topology,
 * and the RAW validity interval at `now_monotonic_raw_ns`.  This has no
 * dependency on a live responsiveness snapshot.
 */
std::optional<VerifiedOperatorCapacityLabelEnvelope>
verify_operator_capacity_label_envelope(
    const OperatorCapacityLabelEnvelope &envelope,
    const OperatorCapacityLabelEnvelopeIssuer &issuer,
    const std::vector<ReplicaID> &expected_membership,
    const AdaptationEpochId &expected_epoch0,
    const uint256_t &expected_baseline_topology_digest,
    std::uint64_t now_monotonic_raw_ns) noexcept;

} // namespace hotstuff

#endif
