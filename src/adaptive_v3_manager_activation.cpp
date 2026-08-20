#include "hotstuff/adaptive_v3_manager_activation.h"

#include <stdexcept>

namespace hotstuff
{

struct AdaptiveV3ManagerActivation::State
{
    ActivationReadinessWireLimits wire_limits;
    AdaptiveV3CertificateDeliveryConfig delivery_config;
    AdaptiveV3ManagerReadinessCollector collector;
    std::unique_ptr<AdaptiveV3CertificateOutbox> outbox;
    std::uint64_t last_tick{0};
    bool terminal{false};

    explicit State(AdaptiveV3ManagerActivationConfig config)
        : wire_limits(config.wire_limits),
          delivery_config{
              {},
              config.maximum_delivery_attempts,
              config.retry_interval_ticks,
              config.wire_limits},
          collector(
              std::move(config.expected_identity),
              {std::move(config.membership),
               config.required_release_count})
    {
        if (wire_limits.maximum_payload_bytes == 0 ||
            wire_limits.maximum_members == 0)
        {
            throw std::invalid_argument(
                "adaptive-v3 manager wire limits are invalid");
        }
    }
};

AdaptiveV3ManagerActivation::AdaptiveV3ManagerActivation(
    AdaptiveV3ManagerActivationConfig config)
    : state_(new State(std::move(config)))
{}

AdaptiveV3ManagerActivation::~AdaptiveV3ManagerActivation() = default;

AdaptiveV3ManagerObservationResult
AdaptiveV3ManagerActivation::record_observation(
    ReplicaID tls_peer,
    const bytearray_t &canonical_payload) noexcept
{
    AdaptiveV3ManagerObservationResult result;
    try
    {
        const auto decoded = decode_activation_ready_observation(
            canonical_payload, state_->wire_limits);
        result.wire_error = decoded.error;
        if (!decoded)
            return result;

        result.observation_digest =
            activation_ready_observation_digest(*decoded.value);
        result.disposition = state_->collector.ingest(
            tls_peer, *decoded.value);

        if (result.disposition ==
                AdaptiveV3ManagerReadinessDisposition::released &&
            state_->outbox == nullptr)
        {
            const auto *certificate = state_->collector.certificate();
            if (certificate == nullptr)
            {
                result.disposition =
                    AdaptiveV3ManagerReadinessDisposition::
                        rejected_invalid_observation;
                return result;
            }
            auto delivery_config = state_->delivery_config;
            delivery_config.recipients.reserve(
                certificate->observations.size());
            for (const auto &observation : certificate->observations)
            {
                delivery_config.recipients.push_back(
                    observation.signer_replica_id);
            }
            state_->outbox = std::make_unique<
                AdaptiveV3CertificateOutbox>(
                    *certificate, std::move(delivery_config));
            result.certificate_assembled = true;
        }
        return result;
    }
    catch (...)
    {
        result.wire_error = ActivationReadinessWireError::internal_failure;
        result.disposition =
            AdaptiveV3ManagerReadinessDisposition::
                rejected_invalid_observation;
        return result;
    }
}

std::optional<AdaptiveV3CertificateDelivery>
AdaptiveV3ManagerActivation::begin_delivery(
    ReplicaID recipient,
    std::uint64_t logical_tick) noexcept
{
    if (state_->terminal || logical_tick < state_->last_tick) return std::nullopt;
    state_->last_tick = logical_tick;
    return state_->outbox == nullptr
               ? std::nullopt
               : state_->outbox->begin(recipient, logical_tick);
}

AdaptiveV3CertificateDeliveryDisposition
AdaptiveV3ManagerActivation::record_delivery_result(
    ReplicaID recipient,
    std::uint32_t attempt,
    bool enqueued,
    std::uint64_t logical_tick) noexcept
{
    if (state_->terminal) return AdaptiveV3CertificateDeliveryDisposition::retry_exhausted;
    if (logical_tick < state_->last_tick)
        return AdaptiveV3CertificateDeliveryDisposition::invalid_ack;
    state_->last_tick = logical_tick;
    return state_->outbox == nullptr
               ? AdaptiveV3CertificateDeliveryDisposition::invalid_ack
               : state_->outbox->result(
                     recipient, attempt, enqueued, logical_tick);
}

AdaptiveV3CertificateDeliveryDisposition
AdaptiveV3ManagerActivation::record_acknowledgement(
    ReplicaID tls_peer, std::uint64_t logical_tick,
    const bytearray_t &canonical_payload) noexcept
{
    if (state_->terminal) return AdaptiveV3CertificateDeliveryDisposition::retry_exhausted;
    if (logical_tick < state_->last_tick)
        return AdaptiveV3CertificateDeliveryDisposition::invalid_ack;
    state_->last_tick = logical_tick;
    if (state_->outbox == nullptr)
        return AdaptiveV3CertificateDeliveryDisposition::invalid_ack;
    const auto decoded = decode_activation_readiness_ack(
        canonical_payload, state_->wire_limits);
    if (!decoded || decoded.value->recipient_replica_id != tls_peer)
        return AdaptiveV3CertificateDeliveryDisposition::invalid_ack;
    return state_->outbox->acknowledge(*decoded.value, logical_tick);
}

bool AdaptiveV3ManagerActivation::delivery_retry_exhausted(
    std::uint64_t logical_tick) noexcept
{
    if (state_->terminal || logical_tick < state_->last_tick) return state_->terminal;
    state_->last_tick = logical_tick;
    if (state_->outbox != nullptr && state_->outbox->retry_exhausted(logical_tick))
        state_->terminal = true;
    return state_->terminal;
}

AdaptiveV3ManagerActivationStatus
AdaptiveV3ManagerActivation::status() const noexcept
{
    if (state_->outbox == nullptr)
        return AdaptiveV3ManagerActivationStatus::collecting;
    return state_->terminal || state_->outbox->terminal()
               ? AdaptiveV3ManagerActivationStatus::terminal
               : AdaptiveV3ManagerActivationStatus::distributing;
}

const ActivationReadinessCertificateV1 *
AdaptiveV3ManagerActivation::certificate() const noexcept
{
    return state_->collector.certificate();
}

const bytearray_t *
AdaptiveV3ManagerActivation::canonical_certificate_bytes() const noexcept
{
    return state_->outbox == nullptr ? nullptr
                                     : &state_->outbox->canonical_bytes();
}

std::size_t AdaptiveV3ManagerActivation::accepted_count() const noexcept
{
    return state_->collector.accepted_count();
}

bool AdaptiveV3ManagerActivation::quarantined(
    ReplicaID source) const noexcept
{
    return state_->collector.quarantined(source);
}

} // namespace hotstuff
