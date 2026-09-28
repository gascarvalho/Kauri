/** Issuer-authenticated, immutable input for an operator-capacity epoch. */
#ifndef HOTSTUFF_OPERATOR_CAPACITY_AUTHORIZATION_H_INCLUDED
#define HOTSTUFF_OPERATOR_CAPACITY_AUTHORIZATION_H_INCLUDED

#include <cstdint>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "hotstuff/crypto.h"
#include "hotstuff/operator_capacity_policy.h"

namespace hotstuff
{

constexpr std::uint32_t kOperatorCapacityAuthorizationSchemaVersion = 1;
constexpr std::size_t kOperatorCapacityAuthorizationSignatureBytes = 64;

struct OperatorCapacityAuthorizationWireLimits
{
    std::size_t maximum_payload_bytes{128 * 1024};
};

enum class OperatorCapacityAuthorizationWireError : std::uint8_t
{
    none = 0, invalid_limits, payload_too_large, truncated, invalid_domain,
    unsupported_schema, malformed_signature, trailing_bytes,
    noncanonical_encoding, allocation_failure, internal_failure,
};

struct OperatorCapacityIssuer
{
    std::uint32_t issuer_id{0};
    std::string issuer_reference;
    uint256_t approved_capacity_digest;
    PubKeySecp256k1 public_key;
};

struct OperatorCapacityAuthorization
{
    std::uint32_t schema_version{kOperatorCapacityAuthorizationSchemaVersion};
    std::uint32_t issuer_id{0};
    std::vector<ReplicaID> membership;
    OperatorCapacityPolicyConfig policy;
    SigSecp256k1 signature;
};

bytearray_t canonical_operator_capacity_authorization_bytes(
    const OperatorCapacityAuthorization &authorization);
uint256_t operator_capacity_authorization_digest(
    const OperatorCapacityAuthorization &authorization);
OperatorCapacityAuthorization authorize_operator_capacity(
    const std::vector<ReplicaID> &membership,
    const OperatorCapacityPolicyConfig &policy,
    std::uint32_t issuer_id,
    const PrivKeySecp256k1 &private_key);
bytearray_t encode_operator_capacity_authorization(
    const OperatorCapacityAuthorization &authorization,
    const OperatorCapacityAuthorizationWireLimits &limits);
struct OperatorCapacityAuthorizationDecodeResult
{
    OperatorCapacityAuthorizationWireError error{
        OperatorCapacityAuthorizationWireError::internal_failure};
    std::optional<OperatorCapacityAuthorization> value;
    explicit operator bool() const noexcept
    { return error == OperatorCapacityAuthorizationWireError::none && value.has_value(); }
};
OperatorCapacityAuthorizationDecodeResult decode_operator_capacity_authorization(
    const bytearray_t &payload,
    const OperatorCapacityAuthorizationWireLimits &limits) noexcept;

class VerifiedOperatorCapacityAuthorization final
{
public:
    const std::vector<ReplicaID> &membership() const noexcept { return authorization_.membership; }
    const OperatorCapacityPolicyConfig &policy() const noexcept { return authorization_.policy; }
    std::uint32_t issuer_id() const noexcept { return authorization_.issuer_id; }
    const uint256_t &capacity_digest() const noexcept
    {
        return authorization_.policy.capacity_snapshot.canonical_digest;
    }

private:
    friend std::optional<VerifiedOperatorCapacityAuthorization>
    verify_operator_capacity_authorization(
        const OperatorCapacityAuthorization &, const OperatorCapacityIssuer &,
        const std::vector<ReplicaID> &) noexcept;

    explicit VerifiedOperatorCapacityAuthorization(
        OperatorCapacityAuthorization authorization)
        : authorization_(std::move(authorization)) {}
    OperatorCapacityAuthorization authorization_;
};

std::optional<VerifiedOperatorCapacityAuthorization>
verify_operator_capacity_authorization(
    const OperatorCapacityAuthorization &authorization,
    const OperatorCapacityIssuer &issuer,
    const std::vector<ReplicaID> &expected_membership) noexcept;

} // namespace hotstuff
#endif
