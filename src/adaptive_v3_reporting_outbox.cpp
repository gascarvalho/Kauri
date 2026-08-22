#include "hotstuff/adaptive_v3_reporting_outbox.h"

#include <algorithm>
#include <limits>
#include <stdexcept>

namespace hotstuff
{
namespace
{
void validate_config(const AdaptiveV3CertificateDeliveryConfig &config)
{
    if (config.recipients.empty() || config.maximum_attempts == 0 ||
        config.retry_interval_ticks == 0)
    {
        throw std::invalid_argument(
            "adaptive-v3 certificate delivery configuration is empty");
    }
    for (std::size_t i = 1; i < config.recipients.size(); ++i)
    {
        if (config.recipients[i - 1] >= config.recipients[i])
            throw std::invalid_argument(
                "adaptive-v3 certificate recipients are not canonical");
    }
}

std::uint64_t checked_retry_tick(
    std::uint64_t now,
    std::uint64_t interval) noexcept
{
    return now > std::numeric_limits<std::uint64_t>::max() - interval
               ? std::numeric_limits<std::uint64_t>::max()
               : now + interval;
}
} // namespace

struct AdaptiveV3CertificateOutbox::State
{
    struct Entry
    {
        ReplicaID id;
        std::uint32_t attempts{0};
        std::uint64_t due{0};
        bool in_flight{false};
        bool done{false};
    };

    bytearray_t bytes;
    uint256_t payload_digest;
    uint256_t certificate_digest;
    ActivationReadyIdentityV1 identity;
    AdaptiveV3CertificateDeliveryConfig config;
    std::vector<Entry> entries;
    std::uint64_t last_tick{0};
    bool exhausted{false};

    State(const ActivationReadinessCertificateV1 &certificate,
          AdaptiveV3CertificateDeliveryConfig input)
        : bytes(encode_activation_readiness_certificate(
              certificate, input.wire_limits)),
          payload_digest(activation_readiness_ack_payload_digest(
              MsgActivationReadinessCertificate::opcode, bytes)),
          certificate_digest(certificate.certificate_digest),
          identity(certificate.identity),
          config(std::move(input))
    {
        validate_config(config);
        entries.reserve(config.recipients.size());
        for (const auto id : config.recipients)
            entries.push_back({id});
    }

    Entry *entry(ReplicaID id) noexcept
    {
        const auto found = std::lower_bound(
            entries.begin(), entries.end(), id,
            [](const Entry &entry, ReplicaID value) {
                return entry.id < value;
            });
        return found == entries.end() || found->id != id ? nullptr : &*found;
    }
};

AdaptiveV3CertificateOutbox::AdaptiveV3CertificateOutbox(
    const ActivationReadinessCertificateV1 &certificate,
    AdaptiveV3CertificateDeliveryConfig config)
    : state_(new State(certificate, std::move(config)))
{}

AdaptiveV3CertificateOutbox::~AdaptiveV3CertificateOutbox() = default;

std::optional<AdaptiveV3CertificateDelivery>
AdaptiveV3CertificateOutbox::begin(
    ReplicaID recipient,
    std::uint64_t logical_tick) noexcept
{
    auto &state = *state_;
    if (state.exhausted || logical_tick < state.last_tick) return {};
    state.last_tick = logical_tick;
    auto *entry = state.entry(recipient);
    if (entry == nullptr || entry->done || entry->in_flight ||
        logical_tick < entry->due ||
        entry->attempts >= state.config.maximum_attempts)
    {
        return {};
    }

    entry->in_flight = true;
    ++entry->attempts;
    return AdaptiveV3CertificateDelivery{
        recipient,
        entry->attempts,
        &state.bytes,
        state.payload_digest};
}

AdaptiveV3CertificateDeliveryDisposition
AdaptiveV3CertificateOutbox::result(
    ReplicaID recipient,
    std::uint32_t attempt,
    bool enqueued,
    std::uint64_t logical_tick) noexcept
{
    auto &state = *state_;
    if (state.exhausted || logical_tick < state.last_tick)
        return AdaptiveV3CertificateDeliveryDisposition::invalid_ack;
    state.last_tick = logical_tick;
    auto *entry = state.entry(recipient);
    if (entry == nullptr || !entry->in_flight ||
        entry->attempts != attempt)
    {
        return AdaptiveV3CertificateDeliveryDisposition::invalid_ack;
    }

    entry->in_flight = false;
    if (!enqueued && entry->attempts >= state.config.maximum_attempts)
        return AdaptiveV3CertificateDeliveryDisposition::retry_exhausted;

    // Every nonterminal result opens a bounded ACK/retry interval. The final
    // successful enqueue still needs this interval because transport success
    // is not an authenticated application ACK.
    entry->due = checked_retry_tick(
        logical_tick, state.config.retry_interval_ticks);
    return AdaptiveV3CertificateDeliveryDisposition::queued;
}

AdaptiveV3CertificateDeliveryDisposition
AdaptiveV3CertificateOutbox::acknowledge(
    const ActivationReadinessAckV1 &acknowledgement) noexcept
{
    (void)acknowledgement;
    // Time is part of the half-open delivery contract; callers must use the
    // tick-bearing overload and cannot bypass it with an implicit zero tick.
    return AdaptiveV3CertificateDeliveryDisposition::invalid_ack;
}

AdaptiveV3CertificateDeliveryDisposition
AdaptiveV3CertificateOutbox::acknowledge(
    const ActivationReadinessAckV1 &acknowledgement,
    std::uint64_t logical_tick) noexcept
{
    auto &state = *state_;
    if (state.exhausted || logical_tick < state.last_tick)
        return AdaptiveV3CertificateDeliveryDisposition::invalid_ack;
    state.last_tick = logical_tick;
    auto *entry = state.entry(acknowledgement.recipient_replica_id);
    if (entry == nullptr || entry->attempts == 0 ||
        acknowledgement.schema_version !=
            kActivationReadinessSchemaVersionV1 ||
        acknowledgement.acknowledged_opcode !=
            MsgActivationReadinessCertificate::opcode ||
        acknowledgement.identity != state.identity ||
        acknowledgement.certificate_digest != state.certificate_digest ||
        acknowledgement.payload_digest != state.payload_digest)
    {
        return AdaptiveV3CertificateDeliveryDisposition::invalid_ack;
    }
    if (entry->done)
        return AdaptiveV3CertificateDeliveryDisposition::duplicate_ack;
    // The final delivery window is half-open. At its due tick the timeout
    // wins regardless of callback order, so a late ACK cannot publish.
    if (!entry->in_flight &&
        entry->attempts >= state.config.maximum_attempts &&
        logical_tick >= entry->due)
        return AdaptiveV3CertificateDeliveryDisposition::retry_exhausted;
    if (acknowledgement.disposition !=
        ActivationReadinessAckDisposition::positive)
    {
        return AdaptiveV3CertificateDeliveryDisposition::rejected_ack;
    }

    entry->done = true;
    entry->in_flight = false;
    return AdaptiveV3CertificateDeliveryDisposition::acknowledged;
}

bool AdaptiveV3CertificateOutbox::retry_exhausted(
    std::uint64_t logical_tick) noexcept
{
    auto &state = *state_;
    if (state.exhausted || logical_tick < state.last_tick)
        return state.exhausted;
    state.last_tick = logical_tick;
    for (const auto &entry : state.entries)
    {
        if (!entry.done && !entry.in_flight &&
            entry.attempts >= state.config.maximum_attempts &&
            logical_tick >= entry.due)
        {
            state.exhausted = true;
            return true;
        }
    }
    return false;
}

bool AdaptiveV3CertificateOutbox::terminal() const noexcept
{
    for (const auto &entry : state_->entries)
    {
        if (!entry.done)
            return false;
    }
    return true;
}

const bytearray_t &AdaptiveV3CertificateOutbox::canonical_bytes() const noexcept
{
    return state_->bytes;
}
} // namespace hotstuff
