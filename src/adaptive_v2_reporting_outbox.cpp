#include "hotstuff/adaptive_v2_reporting_outbox.h"

#include <deque>
#include <limits>
#include <new>
#include <stdexcept>
#include <utility>

namespace hotstuff
{
namespace
{

void increment(std::uint64_t &value) noexcept
{
    if (value != std::numeric_limits<std::uint64_t>::max())
        ++value;
}

bool valid_limits(
    const AdaptiveV2ReportingOutboxLimits &limits) noexcept
{
    return limits.maximum_pending_reports != 0 &&
           limits.maximum_pending_payload_bytes != 0 &&
           limits.maximum_delivery_attempts != 0 &&
           limits.initial_retry_backoff_ns != 0 &&
           limits.maximum_retry_backoff_ns != 0 &&
           limits.initial_retry_backoff_ns <=
               limits.maximum_retry_backoff_ns &&
           limits.readiness_wire.maximum_payload_bytes != 0 &&
           limits.lifecycle_wire.maximum_payload_bytes != 0 &&
           limits.convergence_wire.maximum_payload_bytes != 0 &&
           limits.evidence_wire.maximum_payload_bytes != 0 &&
           limits.evidence_wire.maximum_observations != 0 &&
           limits.evidence_wire.maximum_signers_per_observation != 0;
}

bool is_duplicate_result(
    const AdaptiveV2PendingReport &report,
    AdaptiveV2ReportingDeliveryResult result) noexcept
{
    if (report.delivery_state ==
            AdaptiveV2ReportingDeliveryState::delivered &&
        result == AdaptiveV2ReportingDeliveryResult::delivered)
    {
        return true;
    }

    if (report.delivery_state ==
            AdaptiveV2ReportingDeliveryState::retry_wait &&
        result ==
            AdaptiveV2ReportingDeliveryResult::temporary_failure)
    {
        return true;
    }

    if (report.delivery_state !=
        AdaptiveV2ReportingDeliveryState::failed)
    {
        return false;
    }

    if (result ==
        AdaptiveV2ReportingDeliveryResult::permanent_failure)
    {
        return report.failure_reason ==
               AdaptiveV2ReportingFailureReason::permanent_failure;
    }

    if (result ==
        AdaptiveV2ReportingDeliveryResult::temporary_failure)
    {
        return report.failure_reason ==
                   AdaptiveV2ReportingFailureReason::retry_exhausted ||
               report.failure_reason ==
                   AdaptiveV2ReportingFailureReason::backoff_overflow;
    }

    return false;
}

} // namespace

struct AdaptiveV2ReportingOutbox::State
{
    explicit State(AdaptiveV2ReportingOutboxConfig configured)
        : config(std::move(configured))
    {
        diagnostics.last_readiness_sequence =
            config.initial_readiness_sequence;
        diagnostics.last_lifecycle_sequence =
            config.initial_lifecycle_sequence;
        diagnostics.last_evidence_sequence =
            config.initial_evidence_sequence;
        diagnostics.last_report_id = config.initial_report_id;
        configured_limits = valid_limits(config.limits);
        if (!configured_limits)
            diagnostics.healthy = false;
    }

    bool has_capacity(std::size_t payload_size) noexcept
    {
        if (pending.size() >=
                config.limits.maximum_pending_reports ||
            payload_size >
                config.limits.maximum_pending_payload_bytes ||
            diagnostics.pending_payload_bytes >
                config.limits.maximum_pending_payload_bytes -
                    payload_size)
        {
            increment(diagnostics.capacity_failures);
            return false;
        }
        return true;
    }

    AdaptiveV2ReportingEnqueueStatus enqueue_owned(
        AdaptiveV2ReportingStream stream,
        opcode_t opcode,
        std::uint64_t first_stream_sequence,
        std::uint64_t last_stream_sequence,
        bytearray_t &&payload) noexcept
    {
        if (!has_capacity(payload.size()))
        {
            return AdaptiveV2ReportingEnqueueStatus::
                capacity_exceeded;
        }
        if (diagnostics.last_report_id ==
            std::numeric_limits<std::uint64_t>::max())
        {
            increment(diagnostics.sequence_failures);
            return AdaptiveV2ReportingEnqueueStatus::
                sequence_exhausted;
        }

        const auto next_report_id = diagnostics.last_report_id + 1;
        const auto payload_size = payload.size();
        try
        {
            AdaptiveV2PendingReport report;
            report.report_id = next_report_id;
            report.stream = stream;
            report.opcode = opcode;
            report.first_stream_sequence = first_stream_sequence;
            report.last_stream_sequence = last_stream_sequence;
            report.canonical_payload = std::move(payload);
            pending.push_back(std::move(report));
        }
        catch (const std::bad_alloc &)
        {
            increment(diagnostics.allocation_failures);
            diagnostics.healthy = false;
            return AdaptiveV2ReportingEnqueueStatus::allocation_failure;
        }
        catch (...)
        {
            increment(diagnostics.internal_failures);
            diagnostics.healthy = false;
            return AdaptiveV2ReportingEnqueueStatus::internal_failure;
        }

        diagnostics.pending_payload_bytes += payload_size;
        diagnostics.pending_reports = pending.size();
        diagnostics.last_report_id = next_report_id;
        increment(diagnostics.enqueued_reports);
        return AdaptiveV2ReportingEnqueueStatus::queued;
    }

    std::optional<AdaptiveV2ReportingEnqueueStatus>
    enqueue_precondition() const noexcept
    {
        if (!configured_limits)
        {
            return AdaptiveV2ReportingEnqueueStatus::
                invalid_configuration;
        }
        if (!diagnostics.healthy)
            return AdaptiveV2ReportingEnqueueStatus::unhealthy;
        if (diagnostics.stopped)
            return AdaptiveV2ReportingEnqueueStatus::stopped;
        return std::nullopt;
    }

    template <typename Operation>
    AdaptiveV2ReportingEnqueueStatus guarded_enqueue(
        Operation &&operation) noexcept
    {
        try
        {
            return std::forward<Operation>(operation)();
        }
        catch (const std::bad_alloc &)
        {
            increment(diagnostics.allocation_failures);
            diagnostics.healthy = false;
            return AdaptiveV2ReportingEnqueueStatus::allocation_failure;
        }
        catch (const std::invalid_argument &)
        {
            increment(diagnostics.payload_failures);
            return AdaptiveV2ReportingEnqueueStatus::invalid_payload;
        }
        catch (const std::length_error &)
        {
            increment(diagnostics.payload_failures);
            return AdaptiveV2ReportingEnqueueStatus::invalid_payload;
        }
        catch (...)
        {
            increment(diagnostics.internal_failures);
            diagnostics.healthy = false;
            return AdaptiveV2ReportingEnqueueStatus::internal_failure;
        }
    }

    std::uint64_t retry_delay(
        std::uint32_t temporary_failure_count) const noexcept
    {
        auto delay = config.limits.initial_retry_backoff_ns;
        for (std::uint32_t failure = 1;
             failure < temporary_failure_count;
             ++failure)
        {
            const auto maximum =
                config.limits.maximum_retry_backoff_ns;
            if (delay >= maximum || delay > maximum - delay)
                return maximum;
            delay *= 2;
        }
        return delay;
    }

    void fail(
        AdaptiveV2PendingReport &report,
        AdaptiveV2ReportingFailureReason reason) noexcept
    {
        report.delivery_state =
            AdaptiveV2ReportingDeliveryState::failed;
        report.failure_reason = reason;
        report.next_attempt_monotonic_ns = 0;
        increment(diagnostics.failed_reports);
        diagnostics.healthy = false;
    }

    AdaptiveV2ReportingOutboxConfig config;
    std::deque<AdaptiveV2PendingReport> pending;
    AdaptiveV2ReportingDiagnostics diagnostics;
    bool configured_limits{false};
};

AdaptiveV2ReportingOutbox::AdaptiveV2ReportingOutbox(
    AdaptiveV2ReportingOutboxConfig config)
    : state_(std::make_unique<State>(std::move(config)))
{}

AdaptiveV2ReportingOutbox::~AdaptiveV2ReportingOutbox() = default;

AdaptiveV2ReportingEnqueueStatus
AdaptiveV2ReportingOutbox::enqueue_readiness(
    const ConfigurationId &active_configuration,
    std::uint64_t activation_generation,
    std::uint64_t committed_height) noexcept
{
    if (const auto blocked = state_->enqueue_precondition())
        return *blocked;
    if (state_->diagnostics.last_readiness_sequence ==
        std::numeric_limits<std::uint64_t>::max())
    {
        increment(state_->diagnostics.sequence_failures);
        return AdaptiveV2ReportingEnqueueStatus::sequence_exhausted;
    }

    const auto next_sequence =
        state_->diagnostics.last_readiness_sequence + 1;
    return state_->guarded_enqueue([&]() {
        AdaptiveV2ReadinessNotice notice;
        notice.claimed_source_replica_id =
            state_->config.source_replica_id;
        notice.source_sequence = next_sequence;
        notice.active_configuration = active_configuration;
        notice.activation_generation = activation_generation;
        notice.committed_height = committed_height;
        auto payload = encode_adaptive_v2_readiness_notice(
            notice, state_->config.limits.readiness_wire);
        const auto status = state_->enqueue_owned(
            AdaptiveV2ReportingStream::readiness,
            MsgAdaptiveV2ReadinessNotice::opcode,
            next_sequence,
            next_sequence,
            std::move(payload));
        if (status == AdaptiveV2ReportingEnqueueStatus::queued)
        {
            state_->diagnostics.last_readiness_sequence =
                next_sequence;
        }
        return status;
    });
}

AdaptiveV2ReportingEnqueueStatus
AdaptiveV2ReportingOutbox::enqueue_lifecycle(
    const ProposalLifecycleFact &fact) noexcept
{
    if (const auto blocked = state_->enqueue_precondition())
        return *blocked;

    const auto maximum = std::numeric_limits<std::uint64_t>::max();
    // The lifecycle wire reserves UINT64_MAX as integer_overflow.
    if (state_->diagnostics.last_lifecycle_sequence >= maximum - 1)
    {
        increment(state_->diagnostics.sequence_failures);
        return AdaptiveV2ReportingEnqueueStatus::sequence_exhausted;
    }
    if (std::holds_alternative<ProposalCommitted>(fact) &&
        state_->diagnostics.last_evidence_sequence == maximum)
    {
        increment(state_->diagnostics.sequence_failures);
        return AdaptiveV2ReportingEnqueueStatus::sequence_exhausted;
    }

    const auto next_sequence =
        state_->diagnostics.last_lifecycle_sequence + 1;
    return state_->guarded_enqueue([&]() {
        ProposalLifecycleNotice notice;
        notice.source_replica_id = state_->config.source_replica_id;
        notice.source_sequence = next_sequence;
        notice.fact = fact;
        if (auto *const committed =
                std::get_if<ProposalCommitted>(&notice.fact))
        {
            // One outbox owns all three streams and preserves report FIFO.
            // Freeze the evidence prefix queued before this commit marker;
            // callers cannot inject or ratchet a different fence.
            committed->evidence_sequence_fence =
                state_->diagnostics.last_evidence_sequence;
        }
        auto payload = encode_proposal_lifecycle_notice(
            notice, state_->config.limits.lifecycle_wire);
        const auto status = state_->enqueue_owned(
            AdaptiveV2ReportingStream::lifecycle,
            MsgProposalLifecycleNotice::opcode,
            next_sequence,
            next_sequence,
            std::move(payload));
        if (status == AdaptiveV2ReportingEnqueueStatus::queued)
        {
            state_->diagnostics.last_lifecycle_sequence =
                next_sequence;
        }
        return status;
    });
}

AdaptiveV2ReportingEnqueueStatus
AdaptiveV2ReportingOutbox::enqueue_evidence(
    const bytearray_t &canonical_payload) noexcept
{
    if (const auto blocked = state_->enqueue_precondition())
        return *blocked;
    if (!state_->has_capacity(canonical_payload.size()))
    {
        return AdaptiveV2ReportingEnqueueStatus::capacity_exceeded;
    }

    const auto decoded = decode_evidence_batch(
        canonical_payload, state_->config.limits.evidence_wire);
    if (!decoded)
    {
        if (decoded.error == EvidenceWireError::allocation_failure)
        {
            increment(state_->diagnostics.allocation_failures);
            state_->diagnostics.healthy = false;
            return AdaptiveV2ReportingEnqueueStatus::
                allocation_failure;
        }
        if (decoded.error == EvidenceWireError::internal_failure)
        {
            increment(state_->diagnostics.internal_failures);
            state_->diagnostics.healthy = false;
            return AdaptiveV2ReportingEnqueueStatus::internal_failure;
        }
        increment(state_->diagnostics.payload_failures);
        return AdaptiveV2ReportingEnqueueStatus::invalid_payload;
    }
    if (decoded.batch->observations.empty())
    {
        increment(state_->diagnostics.payload_failures);
        return AdaptiveV2ReportingEnqueueStatus::invalid_payload;
    }

    const auto &observations = decoded.batch->observations;
    auto previous_sequence =
        state_->diagnostics.last_evidence_sequence;
    for (const auto &observation : observations)
    {
        if (observation.reporter_id !=
            state_->config.source_replica_id)
        {
            increment(state_->diagnostics.payload_failures);
            return AdaptiveV2ReportingEnqueueStatus::invalid_payload;
        }
        if (observation.reporter_sequence ==
            std::numeric_limits<std::uint64_t>::max())
        {
            increment(state_->diagnostics.sequence_failures);
            return AdaptiveV2ReportingEnqueueStatus::sequence_exhausted;
        }
        if (observation.reporter_sequence == 0 ||
            observation.reporter_sequence <= previous_sequence)
        {
            increment(state_->diagnostics.sequence_failures);
            return AdaptiveV2ReportingEnqueueStatus::
                sequence_regression;
        }
        previous_sequence = observation.reporter_sequence;
    }

    return state_->guarded_enqueue([&]() {
        const auto reencoded = encode_evidence_batch(
            *decoded.batch, state_->config.limits.evidence_wire);
        if (reencoded != canonical_payload)
        {
            increment(state_->diagnostics.payload_failures);
            return AdaptiveV2ReportingEnqueueStatus::invalid_payload;
        }

        bytearray_t retained_payload = canonical_payload;
        const auto first_sequence =
            observations.front().reporter_sequence;
        const auto last_sequence =
            observations.back().reporter_sequence;
        const auto status = state_->enqueue_owned(
            AdaptiveV2ReportingStream::evidence,
            MsgEvidenceReport::opcode,
            first_sequence,
            last_sequence,
            std::move(retained_payload));
        if (status == AdaptiveV2ReportingEnqueueStatus::queued)
        {
            state_->diagnostics.last_evidence_sequence =
                last_sequence;
        }
        return status;
    });
}

AdaptiveV2ReportingEnqueueStatus
AdaptiveV2ReportingOutbox::enqueue_epoch_change_committed(
    const AdaptiveV2EpochChangeIdentity &identity) noexcept
{
    return enqueue_convergence_observation(
        AdaptiveV2ConvergenceObservationKind::commit, identity);
}

AdaptiveV2ReportingEnqueueStatus
AdaptiveV2ReportingOutbox::enqueue_epoch_activated(
    const AdaptiveV2EpochChangeIdentity &identity) noexcept
{
    return enqueue_convergence_observation(
        AdaptiveV2ConvergenceObservationKind::activation, identity);
}

AdaptiveV2ReportingEnqueueStatus
AdaptiveV2ReportingOutbox::enqueue_convergence_observation(
    AdaptiveV2ConvergenceObservationKind kind,
    const AdaptiveV2EpochChangeIdentity &identity) noexcept
{
    if (const auto blocked = state_->enqueue_precondition())
        return *blocked;

    return state_->guarded_enqueue([&]() {
        bytearray_t payload;
        opcode_t opcode = 0;
        if (kind == AdaptiveV2ConvergenceObservationKind::commit)
        {
            AdaptiveV2EpochChangeCommittedObservation observation;
            observation.claimed_source_replica_id =
                state_->config.source_replica_id;
            observation.identity = identity;
            payload =
                encode_adaptive_v2_epoch_change_committed_observation(
                    observation,
                    state_->config.limits.convergence_wire);
            opcode =
                MsgAdaptiveV2EpochChangeCommittedObservation::opcode;
        }
        else if (kind ==
                 AdaptiveV2ConvergenceObservationKind::activation)
        {
            AdaptiveV2EpochActivatedObservation observation;
            observation.claimed_source_replica_id =
                state_->config.source_replica_id;
            observation.identity = identity;
            observation.activated_epoch_number =
                identity.successor_epoch_number;
            observation.activated_epoch_digest =
                identity.successor_epoch_digest;
            payload = encode_adaptive_v2_epoch_activated_observation(
                observation,
                state_->config.limits.convergence_wire);
            opcode = MsgAdaptiveV2EpochActivatedObservation::opcode;
        }
        else
        {
            throw std::invalid_argument(
                "invalid adaptive-v2 convergence observation kind");
        }
        const auto observation_digest =
            adaptive_v2_convergence_observation_digest(
                opcode, payload);
        const auto status = state_->enqueue_owned(
            AdaptiveV2ReportingStream::convergence,
            opcode,
            0,
            0,
            std::move(payload));
        if (status == AdaptiveV2ReportingEnqueueStatus::queued)
        {
            auto &report = state_->pending.back();
            report.convergence_observation_kind = kind;
            report.convergence_identity = identity;
            report.convergence_observation_digest = observation_digest;
        }
        return status;
    });
}

const AdaptiveV2PendingReport *
AdaptiveV2ReportingOutbox::front() const noexcept
{
    if (state_->pending.empty())
        return nullptr;
    return &state_->pending.front();
}

AdaptiveV2ReportingAttemptResult
AdaptiveV2ReportingOutbox::begin_delivery(
    std::uint64_t current_monotonic_ns) noexcept
{
    if (!state_->diagnostics.healthy)
    {
        return {AdaptiveV2ReportingAttemptStatus::unhealthy,
                std::nullopt,
                front()};
    }
    if (state_->diagnostics.stopped)
    {
        return {AdaptiveV2ReportingAttemptStatus::stopped,
                std::nullopt,
                nullptr};
    }
    if (state_->pending.empty())
    {
        return {AdaptiveV2ReportingAttemptStatus::empty,
                std::nullopt,
                nullptr};
    }

    auto &report = state_->pending.front();
    switch (report.delivery_state)
    {
    case AdaptiveV2ReportingDeliveryState::in_flight:
        return {AdaptiveV2ReportingAttemptStatus::already_in_flight,
                std::nullopt,
                &report};
    case AdaptiveV2ReportingDeliveryState::delivered:
    case AdaptiveV2ReportingDeliveryState::failed:
        return {AdaptiveV2ReportingAttemptStatus::terminal,
                std::nullopt,
                &report};
    case AdaptiveV2ReportingDeliveryState::retry_wait:
    case AdaptiveV2ReportingDeliveryState::awaiting_ack:
        if (current_monotonic_ns <
            report.next_attempt_monotonic_ns)
        {
            return {
                AdaptiveV2ReportingAttemptStatus::retry_not_due,
                std::nullopt,
                &report};
        }
        break;
    case AdaptiveV2ReportingDeliveryState::queued:
        break;
    }

    if (report.delivery_attempts >=
        state_->config.limits.maximum_delivery_attempts)
    {
        state_->fail(
            report,
            AdaptiveV2ReportingFailureReason::retry_exhausted);
        increment(state_->diagnostics.retry_exhaustions);
        return {AdaptiveV2ReportingAttemptStatus::terminal,
                std::nullopt,
                &report};
    }

    ++report.delivery_attempts;
    report.delivery_state =
        AdaptiveV2ReportingDeliveryState::in_flight;
    report.failure_reason =
        AdaptiveV2ReportingFailureReason::none;
    report.next_attempt_monotonic_ns = 0;
    increment(state_->diagnostics.delivery_attempts);
    const AdaptiveV2ReportingDeliveryToken token{
        report.report_id, report.delivery_attempts};
    return {AdaptiveV2ReportingAttemptStatus::started,
            token,
            &report};
}

AdaptiveV2ReportingTransitionStatus
AdaptiveV2ReportingOutbox::acknowledge_delivery(
    const AdaptiveV2ReportingDeliveryToken &token,
    AdaptiveV2ReportingDeliveryResult result,
    std::uint64_t current_monotonic_ns) noexcept
{
    if (!state_->diagnostics.healthy)
        return AdaptiveV2ReportingTransitionStatus::unhealthy;
    if (state_->diagnostics.stopped)
        return AdaptiveV2ReportingTransitionStatus::stopped;
    if (state_->pending.empty() ||
        token.report_id != state_->pending.front().report_id)
    {
        increment(state_->diagnostics.invalid_transitions);
        return AdaptiveV2ReportingTransitionStatus::stale_report;
    }

    auto &report = state_->pending.front();
    if (token.attempt_number != report.delivery_attempts)
    {
        increment(state_->diagnostics.invalid_transitions);
        return AdaptiveV2ReportingTransitionStatus::stale_attempt;
    }
    if (is_duplicate_result(report, result))
    {
        increment(state_->diagnostics.duplicate_acknowledgements);
        return AdaptiveV2ReportingTransitionStatus::duplicate;
    }
    if (report.delivery_state !=
        AdaptiveV2ReportingDeliveryState::in_flight)
    {
        increment(state_->diagnostics.invalid_transitions);
        return AdaptiveV2ReportingTransitionStatus::invalid_transition;
    }

    switch (result)
    {
    case AdaptiveV2ReportingDeliveryResult::delivered:
        if (report.stream == AdaptiveV2ReportingStream::convergence)
        {
            const auto delay = state_->retry_delay(
                report.delivery_attempts);
            if (current_monotonic_ns >
                std::numeric_limits<std::uint64_t>::max() - delay)
            {
                state_->fail(
                    report,
                    AdaptiveV2ReportingFailureReason::backoff_overflow);
                return AdaptiveV2ReportingTransitionStatus::failed;
            }
            report.delivery_state =
                AdaptiveV2ReportingDeliveryState::awaiting_ack;
            report.failure_reason =
                AdaptiveV2ReportingFailureReason::none;
            report.next_attempt_monotonic_ns =
                current_monotonic_ns + delay;
            return AdaptiveV2ReportingTransitionStatus::retry_scheduled;
        }
        report.delivery_state =
            AdaptiveV2ReportingDeliveryState::delivered;
        report.failure_reason =
            AdaptiveV2ReportingFailureReason::none;
        increment(state_->diagnostics.delivered_reports);
        return AdaptiveV2ReportingTransitionStatus::delivered;

    case AdaptiveV2ReportingDeliveryResult::temporary_failure:
    {
        ++report.temporary_failures;
        increment(state_->diagnostics.temporary_failures);
        if (report.delivery_attempts >=
            state_->config.limits.maximum_delivery_attempts)
        {
            state_->fail(
                report,
                AdaptiveV2ReportingFailureReason::retry_exhausted);
            increment(state_->diagnostics.retry_exhaustions);
            return AdaptiveV2ReportingTransitionStatus::failed;
        }

        const auto delay =
            state_->retry_delay(report.temporary_failures);
        if (current_monotonic_ns >
            std::numeric_limits<std::uint64_t>::max() - delay)
        {
            state_->fail(
                report,
                AdaptiveV2ReportingFailureReason::backoff_overflow);
            return AdaptiveV2ReportingTransitionStatus::failed;
        }
        report.delivery_state =
            AdaptiveV2ReportingDeliveryState::retry_wait;
        report.next_attempt_monotonic_ns =
            current_monotonic_ns + delay;
        return AdaptiveV2ReportingTransitionStatus::retry_scheduled;
    }

    case AdaptiveV2ReportingDeliveryResult::permanent_failure:
        state_->fail(
            report,
            AdaptiveV2ReportingFailureReason::permanent_failure);
        return AdaptiveV2ReportingTransitionStatus::failed;
    }

    increment(state_->diagnostics.invalid_transitions);
    state_->fail(
        report,
        AdaptiveV2ReportingFailureReason::invalid_delivery_result);
    return AdaptiveV2ReportingTransitionStatus::failed;
}

AdaptiveV2ReportingTransitionStatus
AdaptiveV2ReportingOutbox::acknowledge_convergence_observation(
    const AdaptiveV2ConvergenceObservationAck &acknowledgement) noexcept
{
    if (!state_->diagnostics.healthy)
        return AdaptiveV2ReportingTransitionStatus::unhealthy;
    if (state_->diagnostics.stopped)
        return AdaptiveV2ReportingTransitionStatus::stopped;
    if (state_->pending.empty())
    {
        increment(state_->diagnostics.invalid_transitions);
        return AdaptiveV2ReportingTransitionStatus::stale_report;
    }

    auto &report = state_->pending.front();
    if (report.stream != AdaptiveV2ReportingStream::convergence ||
        acknowledgement.schema_version !=
            kAdaptiveV2ConvergenceAckSchemaVersionV1 ||
        acknowledgement.target_replica_id !=
            state_->config.source_replica_id ||
        !report.convergence_observation_kind.has_value() ||
        acknowledgement.observation_kind !=
            *report.convergence_observation_kind ||
        !report.convergence_identity.has_value() ||
        acknowledgement.identity != *report.convergence_identity ||
        acknowledgement.observation_digest !=
            report.convergence_observation_digest)
    {
        increment(state_->diagnostics.invalid_transitions);
        return AdaptiveV2ReportingTransitionStatus::stale_report;
    }

    if (report.delivery_state ==
        AdaptiveV2ReportingDeliveryState::delivered)
    {
        increment(state_->diagnostics.duplicate_acknowledgements);
        return AdaptiveV2ReportingTransitionStatus::duplicate;
    }
    if (report.delivery_state == AdaptiveV2ReportingDeliveryState::failed)
    {
        increment(state_->diagnostics.invalid_transitions);
        return AdaptiveV2ReportingTransitionStatus::invalid_transition;
    }

    switch (acknowledgement.disposition)
    {
    case AdaptiveV2ConvergenceAckDisposition::positive:
        report.delivery_state =
            AdaptiveV2ReportingDeliveryState::delivered;
        report.failure_reason = AdaptiveV2ReportingFailureReason::none;
        report.next_attempt_monotonic_ns = 0;
        increment(state_->diagnostics.delivered_reports);
        return AdaptiveV2ReportingTransitionStatus::delivered;

    case AdaptiveV2ConvergenceAckDisposition::permanent_rejection:
        state_->fail(
            report,
            AdaptiveV2ReportingFailureReason::permanent_failure);
        return AdaptiveV2ReportingTransitionStatus::failed;
    }

    increment(state_->diagnostics.invalid_transitions);
    state_->fail(
        report,
        AdaptiveV2ReportingFailureReason::invalid_delivery_result);
    return AdaptiveV2ReportingTransitionStatus::failed;
}

AdaptiveV2ReportingReleaseStatus
AdaptiveV2ReportingOutbox::release_terminal(
    std::uint64_t report_id) noexcept
{
    if (!state_->diagnostics.healthy)
        return AdaptiveV2ReportingReleaseStatus::unhealthy;
    if (state_->pending.empty())
        return AdaptiveV2ReportingReleaseStatus::empty;
    if (state_->pending.front().report_id != report_id)
        return AdaptiveV2ReportingReleaseStatus::stale_report;

    const auto state = state_->pending.front().delivery_state;
    if (state != AdaptiveV2ReportingDeliveryState::delivered &&
        state != AdaptiveV2ReportingDeliveryState::failed)
    {
        return AdaptiveV2ReportingReleaseStatus::not_terminal;
    }

    state_->diagnostics.pending_payload_bytes -=
        state_->pending.front().canonical_payload.size();
    state_->pending.pop_front();
    state_->diagnostics.pending_reports = state_->pending.size();
    return AdaptiveV2ReportingReleaseStatus::released;
}

AdaptiveV2ReportingDiagnostics
AdaptiveV2ReportingOutbox::diagnostics() const noexcept
{
    return state_->diagnostics;
}

bool AdaptiveV2ReportingOutbox::healthy() const noexcept
{
    return state_->diagnostics.healthy;
}

void AdaptiveV2ReportingOutbox::shutdown() noexcept
{
    state_->diagnostics.stopped = true;
}

} // namespace hotstuff
