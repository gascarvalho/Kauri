#ifndef HOTSTUFF_ADAPTIVE_V3_REPORTING_OUTBOX_H_INCLUDED
#define HOTSTUFF_ADAPTIVE_V3_REPORTING_OUTBOX_H_INCLUDED

#include <cstdint>
#include <memory>
#include <optional>
#include <vector>

#include "hotstuff/activation_readiness_wire.h"

namespace hotstuff
{

enum class AdaptiveV3CertificateDeliveryDisposition : std::uint8_t
{
    queued = 1,
    retry_not_due,
    in_flight,
    acknowledged,
    duplicate_ack,
    rejected_ack,
    retry_exhausted,
    invalid_ack,
};

struct AdaptiveV3CertificateDeliveryConfig
{
    std::vector<ReplicaID> recipients;
    std::uint32_t maximum_attempts{0};
    std::uint64_t retry_interval_ticks{0};
    ActivationReadinessWireLimits wire_limits;
};

struct AdaptiveV3CertificateDelivery
{
    ReplicaID recipient{0};
    std::uint32_t attempt{0};
    const bytearray_t *bytes{nullptr};
    uint256_t payload_digest;
};

/** Immutable manager-to-replica certificate sender. An ACK is accepted only
 * for its recipient, certificate digest, and exact canonical payload digest. */
class AdaptiveV3CertificateOutbox final
{
public:
    AdaptiveV3CertificateOutbox(
        const ActivationReadinessCertificateV1 &,
        AdaptiveV3CertificateDeliveryConfig);
    ~AdaptiveV3CertificateOutbox();

    AdaptiveV3CertificateOutbox(
        const AdaptiveV3CertificateOutbox &) = delete;
    AdaptiveV3CertificateOutbox &operator=(
        const AdaptiveV3CertificateOutbox &) = delete;

    std::optional<AdaptiveV3CertificateDelivery> begin(
        ReplicaID, std::uint64_t) noexcept;
    AdaptiveV3CertificateDeliveryDisposition result(
        ReplicaID, std::uint32_t, bool, std::uint64_t) noexcept;
    AdaptiveV3CertificateDeliveryDisposition acknowledge(
        const ActivationReadinessAckV1 &) noexcept;
    AdaptiveV3CertificateDeliveryDisposition acknowledge(
        const ActivationReadinessAckV1 &, std::uint64_t) noexcept;
    bool retry_exhausted(std::uint64_t logical_tick) noexcept;
    bool terminal() const noexcept;
    const bytearray_t &canonical_bytes() const noexcept;

private:
    struct State;
    std::unique_ptr<State> state_;
};

} // namespace hotstuff

#endif
