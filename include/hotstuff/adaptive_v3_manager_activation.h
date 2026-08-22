#ifndef HOTSTUFF_ADAPTIVE_V3_MANAGER_ACTIVATION_H_INCLUDED
#define HOTSTUFF_ADAPTIVE_V3_MANAGER_ACTIVATION_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <utility>
#include <vector>

#include "hotstuff/adaptive_v3_manager_readiness.h"
#include "hotstuff/adaptive_v3_reporting_outbox.h"

namespace hotstuff
{

struct AdaptiveV3ManagerActivationConfig
{
    ActivationReadyIdentityV1 expected_identity;
    std::vector<std::pair<ReplicaID, PubKeyBLS>> membership;
    std::size_t required_release_count{0};
    std::uint32_t maximum_delivery_attempts{0};
    std::uint64_t retry_interval_ticks{0};
    ActivationReadinessWireLimits wire_limits;
};

enum class AdaptiveV3ManagerActivationStatus : std::uint8_t
{
    collecting = 1,
    distributing,
    terminal,
};

/**
 * Transport-independent manager owner for one exact adaptive-v3 activation.
 *
 * TLS peer identity is supplied by the authenticated transport. This class
 * never receives crash identities, process state, fault receipts, or voting
 * material. It owns availability/evidence state only.
 */
class AdaptiveV3ManagerActivation final
{
public:
    explicit AdaptiveV3ManagerActivation(
        AdaptiveV3ManagerActivationConfig config);
    ~AdaptiveV3ManagerActivation();

    AdaptiveV3ManagerActivation(const AdaptiveV3ManagerActivation &) = delete;
    AdaptiveV3ManagerActivation &operator=(
        const AdaptiveV3ManagerActivation &) = delete;

    AdaptiveV3ManagerObservationResult record_observation(
        ReplicaID tls_peer,
        const bytearray_t &canonical_payload) noexcept;

    std::optional<AdaptiveV3CertificateDelivery> begin_delivery(
        ReplicaID recipient,
        std::uint64_t logical_tick) noexcept;
    AdaptiveV3CertificateDeliveryDisposition record_delivery_result(
        ReplicaID recipient,
        std::uint32_t attempt,
        bool enqueued,
        std::uint64_t logical_tick) noexcept;
    AdaptiveV3CertificateDeliveryDisposition record_acknowledgement(
        ReplicaID tls_peer, std::uint64_t logical_tick,
        const bytearray_t &canonical_payload) noexcept;
    bool delivery_retry_exhausted(
        std::uint64_t logical_tick) noexcept;

    AdaptiveV3ManagerActivationStatus status() const noexcept;
    const ActivationReadinessCertificateV1 *certificate() const noexcept;
    const bytearray_t *canonical_certificate_bytes() const noexcept;
    std::size_t accepted_count() const noexcept;
    bool quarantined(ReplicaID source) const noexcept;

private:
    struct State;
    std::unique_ptr<State> state_;
};

} // namespace hotstuff

#endif
