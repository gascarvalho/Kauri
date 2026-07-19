#include <cstddef>
#include <cstdint>
#include <limits>
#include <string>
#include <type_traits>
#include <utility>
#include <variant>
#include <vector>

#include "catch.hpp"
#include "hotstuff/adaptive_v2_reporting_outbox.h"

namespace
{

using hotstuff::AdaptiveV2PendingReport;
using hotstuff::AdaptiveV2ReportingAttemptStatus;
using hotstuff::AdaptiveV2ReportingDeliveryResult;
using hotstuff::AdaptiveV2ReportingDeliveryState;
using hotstuff::AdaptiveV2ReportingEnqueueStatus;
using hotstuff::AdaptiveV2ReportingFailureReason;
using hotstuff::AdaptiveV2ReportingOutbox;
using hotstuff::AdaptiveV2ReportingOutboxConfig;
using hotstuff::AdaptiveV2ReportingOutboxLimits;
using hotstuff::AdaptiveV2ReportingReleaseStatus;
using hotstuff::AdaptiveV2ReportingStream;
using hotstuff::AdaptiveV2ReportingTransitionStatus;
using hotstuff::ConfigurationId;
using hotstuff::DataStream;
using hotstuff::ExpectedMessageType;
using hotstuff::NormalProposalRuntimeInitialized;
using hotstuff::ProposalCommitted;
using hotstuff::ProposalKey;
using hotstuff::ProposalLifecycleFact;
using hotstuff::ReplicaID;
using hotstuff::ResponseObservation;
using hotstuff::ResponseObservationBatch;
using hotstuff::ResponseOutcome;
using hotstuff::bytearray_t;
using hotstuff::uint256_t;

constexpr ReplicaID kSource = 7;

static_assert(noexcept(
    std::declval<AdaptiveV2ReportingOutbox &>().enqueue_readiness(
        std::declval<const ConfigurationId &>(), 1, 0)));
static_assert(noexcept(
    std::declval<AdaptiveV2ReportingOutbox &>().enqueue_lifecycle(
        std::declval<const ProposalLifecycleFact &>())));
static_assert(noexcept(
    std::declval<AdaptiveV2ReportingOutbox &>().enqueue_evidence(
        std::declval<const bytearray_t &>())));

uint256_t digest(const std::string &label)
{
    return DataStream(label).get_hash();
}

ConfigurationId configuration(
    std::uint32_t epoch,
    std::uint32_t tree,
    const std::string &label)
{
    return ConfigurationId{epoch, tree, digest(label)};
}

ProposalKey proposal(
    const ConfigurationId &exact_configuration,
    const std::string &label)
{
    return ProposalKey{
        exact_configuration, digest(label + "-block")};
}

AdaptiveV2ReportingOutboxLimits limits(
    std::size_t maximum_pending = 16,
    std::size_t maximum_bytes = 64 * 1024,
    std::uint32_t maximum_attempts = 3,
    std::uint64_t initial_backoff = 10,
    std::uint64_t maximum_backoff = 25)
{
    AdaptiveV2ReportingOutboxLimits value;
    value.maximum_pending_reports = maximum_pending;
    value.maximum_pending_payload_bytes = maximum_bytes;
    value.maximum_delivery_attempts = maximum_attempts;
    value.initial_retry_backoff_ns = initial_backoff;
    value.maximum_retry_backoff_ns = maximum_backoff;
    value.readiness_wire.maximum_payload_bytes = 256;
    value.lifecycle_wire.maximum_payload_bytes = 512;
    value.evidence_wire.maximum_payload_bytes = 4096;
    value.evidence_wire.maximum_observations = 8;
    value.evidence_wire.maximum_signers_per_observation = 8;
    return value;
}

AdaptiveV2ReportingOutboxConfig config(
    AdaptiveV2ReportingOutboxLimits configured_limits = limits(),
    std::uint64_t readiness_sequence = 0,
    std::uint64_t lifecycle_sequence = 0,
    std::uint64_t evidence_sequence = 0)
{
    return AdaptiveV2ReportingOutboxConfig{
        kSource,
        readiness_sequence,
        lifecycle_sequence,
        evidence_sequence,
        0,
        std::move(configured_limits)};
}

ResponseObservation observation(
    std::uint64_t sequence,
    const ProposalKey &exact_proposal,
    ReplicaID source = kSource,
    ReplicaID observed = 2)
{
    ResponseObservation value;
    value.reporter_id = source;
    value.observed_replica_id = observed;
    value.configuration = exact_proposal.configuration;
    value.block_hash = exact_proposal.block_hash;
    value.expected_message_type = ExpectedMessageType::direct_vote;
    value.outcome = ResponseOutcome::on_time;
    value.response_duration_us = 50;
    value.deadline_duration_us = 100;
    value.reporter_monotonic_ns = sequence * 1'000;
    value.reporter_sequence = sequence;
    value.signer_set = {observed};
    value.observation_id =
        hotstuff::compute_response_observation_id(
            value.attempt_identity());
    return value;
}

bytearray_t evidence_payload(
    const std::vector<ResponseObservation> &observations,
    hotstuff::EvidenceWireLimits wire_limits =
        limits().evidence_wire)
{
    ResponseObservationBatch batch;
    batch.observations = observations;
    return hotstuff::encode_evidence_batch(batch, wire_limits);
}

void deliver_and_release(
    AdaptiveV2ReportingOutbox &outbox,
    std::uint64_t now)
{
    const auto attempt = outbox.begin_delivery(now);
    REQUIRE(attempt.status ==
            AdaptiveV2ReportingAttemptStatus::started);
    REQUIRE(attempt.token.has_value());
    REQUIRE(attempt.report != nullptr);
    const auto report_id = attempt.report->report_id;
    CHECK(outbox.acknowledge_delivery(
              *attempt.token,
              AdaptiveV2ReportingDeliveryResult::delivered,
              now) ==
          AdaptiveV2ReportingTransitionStatus::delivered);
    CHECK(outbox.release_terminal(report_id) ==
          AdaptiveV2ReportingReleaseStatus::released);
}

} // namespace

TEST_CASE(
    "reporting outbox preserves FIFO order independent stream sequences and exact identities",
    "[adaptive-v2][reporting-outbox][ordering][identity][wire]")
{
    AdaptiveV2ReportingOutbox outbox(config());
    const auto first_configuration =
        configuration(4, 2, "readiness-first");
    const auto exact_proposal = proposal(
        first_configuration, "lifecycle-proposal");
    const auto evidence_configuration =
        configuration(5, 3, "evidence-configuration");
    const auto evidence_proposal = proposal(
        evidence_configuration, "evidence-proposal");
    const auto canonical_evidence = evidence_payload(
        {observation(1, evidence_proposal)});

    REQUIRE(outbox.enqueue_readiness(
                first_configuration, 9, 100) ==
            AdaptiveV2ReportingEnqueueStatus::queued);
    REQUIRE(outbox.enqueue_lifecycle(
                ProposalLifecycleFact{
                    NormalProposalRuntimeInitialized{
                        exact_proposal}}) ==
            AdaptiveV2ReportingEnqueueStatus::queued);
    REQUIRE(outbox.enqueue_evidence(canonical_evidence) ==
            AdaptiveV2ReportingEnqueueStatus::queued);
    REQUIRE(outbox.enqueue_readiness(
                evidence_configuration, 10, 101) ==
            AdaptiveV2ReportingEnqueueStatus::queued);

    REQUIRE(outbox.front() != nullptr);
    CHECK(outbox.front()->report_id == 1);
    CHECK(outbox.front()->stream ==
          AdaptiveV2ReportingStream::readiness);
    CHECK(outbox.front()->opcode ==
          hotstuff::MsgAdaptiveV2ReadinessNotice::opcode);
    CHECK(outbox.front()->first_stream_sequence == 1);
    const auto decoded_readiness =
        hotstuff::decode_adaptive_v2_readiness_notice(
            outbox.front()->canonical_payload,
            limits().readiness_wire);
    REQUIRE(decoded_readiness);
    CHECK(decoded_readiness.notice->claimed_source_replica_id ==
          kSource);
    CHECK(decoded_readiness.notice->source_sequence == 1);
    CHECK(decoded_readiness.notice->active_configuration ==
          first_configuration);
    CHECK(decoded_readiness.notice->activation_generation == 9);
    CHECK(decoded_readiness.notice->committed_height == 100);
    deliver_and_release(outbox, 1);

    REQUIRE(outbox.front() != nullptr);
    CHECK(outbox.front()->report_id == 2);
    CHECK(outbox.front()->stream ==
          AdaptiveV2ReportingStream::lifecycle);
    CHECK(outbox.front()->opcode ==
          hotstuff::MsgProposalLifecycleNotice::opcode);
    CHECK(outbox.front()->first_stream_sequence == 1);
    const auto decoded_lifecycle =
        hotstuff::decode_proposal_lifecycle_notice(
            outbox.front()->canonical_payload,
            limits().lifecycle_wire);
    REQUIRE(decoded_lifecycle);
    CHECK(decoded_lifecycle.notice->source_replica_id == kSource);
    CHECK(decoded_lifecycle.notice->source_sequence == 1);
    REQUIRE(std::holds_alternative<
            NormalProposalRuntimeInitialized>(
        decoded_lifecycle.notice->fact));
    CHECK(std::get<NormalProposalRuntimeInitialized>(
              decoded_lifecycle.notice->fact)
              .proposal == exact_proposal);
    deliver_and_release(outbox, 2);

    REQUIRE(outbox.front() != nullptr);
    CHECK(outbox.front()->report_id == 3);
    CHECK(outbox.front()->stream ==
          AdaptiveV2ReportingStream::evidence);
    CHECK(outbox.front()->opcode ==
          hotstuff::MsgEvidenceReport::opcode);
    CHECK(outbox.front()->first_stream_sequence == 1);
    CHECK(outbox.front()->last_stream_sequence == 1);
    CHECK(outbox.front()->canonical_payload == canonical_evidence);
    const auto decoded_evidence = hotstuff::decode_evidence_batch(
        outbox.front()->canonical_payload,
        limits().evidence_wire);
    REQUIRE(decoded_evidence);
    REQUIRE(decoded_evidence.batch->observations.size() == 1);
    CHECK(decoded_evidence.batch->observations.front()
              .proposal_key() == evidence_proposal);
    CHECK(decoded_evidence.batch->observations.front()
              .reporter_sequence == 1);
    deliver_and_release(outbox, 3);

    REQUIRE(outbox.front() != nullptr);
    CHECK(outbox.front()->report_id == 4);
    CHECK(outbox.front()->stream ==
          AdaptiveV2ReportingStream::readiness);
    CHECK(outbox.front()->first_stream_sequence == 2);
    const auto second_readiness =
        hotstuff::decode_adaptive_v2_readiness_notice(
            outbox.front()->canonical_payload,
            limits().readiness_wire);
    REQUIRE(second_readiness);
    CHECK(second_readiness.notice->source_sequence == 2);
    CHECK(second_readiness.notice->active_configuration ==
          evidence_configuration);
    CHECK(second_readiness.notice->activation_generation == 10);
    deliver_and_release(outbox, 4);

    const auto diagnostics = outbox.diagnostics();
    CHECK(diagnostics.pending_reports == 0);
    CHECK(diagnostics.pending_payload_bytes == 0);
    CHECK(diagnostics.last_readiness_sequence == 2);
    CHECK(diagnostics.last_lifecycle_sequence == 1);
    CHECK(diagnostics.last_evidence_sequence == 1);
    CHECK(diagnostics.enqueued_reports == 4);
    CHECK(diagnostics.delivered_reports == 4);
}

TEST_CASE(
    "one bootstrap readiness precedes monotonic exact proposal lifecycle facts",
    "[adaptive-v2][reporting-outbox][bootstrap][lifecycle][sequence]")
{
    AdaptiveV2ReportingOutbox outbox(config());
    const auto epoch_zero = configuration(0, 0, "bootstrap-epoch-zero");
    const auto exact_proposal = proposal(epoch_zero, "bootstrap-proposal");

    REQUIRE(outbox.enqueue_readiness(epoch_zero, 1, 0) ==
            AdaptiveV2ReportingEnqueueStatus::queued);
    REQUIRE(outbox.enqueue_lifecycle(
                ProposalLifecycleFact{
                    NormalProposalRuntimeInitialized{exact_proposal}}) ==
            AdaptiveV2ReportingEnqueueStatus::queued);
    REQUIRE(outbox.enqueue_lifecycle(
                ProposalLifecycleFact{
                    ProposalCommitted{exact_proposal}}) ==
            AdaptiveV2ReportingEnqueueStatus::queued);

    auto diagnostics = outbox.diagnostics();
    CHECK(diagnostics.last_readiness_sequence == 1);
    CHECK(diagnostics.last_lifecycle_sequence == 2);
    CHECK(diagnostics.pending_reports == 3);

    REQUIRE(outbox.front() != nullptr);
    const auto readiness = hotstuff::decode_adaptive_v2_readiness_notice(
        outbox.front()->canonical_payload, limits().readiness_wire);
    REQUIRE(readiness);
    CHECK(readiness.notice->source_sequence == 1);
    CHECK(readiness.notice->active_configuration == epoch_zero);
    CHECK(readiness.notice->activation_generation == 1);
    const auto readiness_bytes = outbox.front()->canonical_payload;
    const auto unavailable = outbox.begin_delivery(100);
    REQUIRE(unavailable.token.has_value());
    CHECK(outbox.acknowledge_delivery(
              *unavailable.token,
              AdaptiveV2ReportingDeliveryResult::temporary_failure,
              100) ==
          AdaptiveV2ReportingTransitionStatus::retry_scheduled);
    CHECK(outbox.begin_delivery(109).status ==
          AdaptiveV2ReportingAttemptStatus::retry_not_due);
    const auto connected = outbox.begin_delivery(110);
    REQUIRE(connected.token.has_value());
    REQUIRE(connected.report != nullptr);
    CHECK(connected.report->canonical_payload == readiness_bytes);
    CHECK(outbox.acknowledge_delivery(
              *connected.token,
              AdaptiveV2ReportingDeliveryResult::delivered,
              110) ==
          AdaptiveV2ReportingTransitionStatus::delivered);
    CHECK(outbox.release_terminal(1) ==
          AdaptiveV2ReportingReleaseStatus::released);

    REQUIRE(outbox.front() != nullptr);
    const auto initialized = hotstuff::decode_proposal_lifecycle_notice(
        outbox.front()->canonical_payload, limits().lifecycle_wire);
    REQUIRE(initialized);
    CHECK(initialized.notice->source_sequence == 1);
    REQUIRE(std::holds_alternative<NormalProposalRuntimeInitialized>(
        initialized.notice->fact));
    CHECK(std::get<NormalProposalRuntimeInitialized>(
              initialized.notice->fact)
              .proposal == exact_proposal);
    deliver_and_release(outbox, 2);

    REQUIRE(outbox.front() != nullptr);
    const auto committed = hotstuff::decode_proposal_lifecycle_notice(
        outbox.front()->canonical_payload, limits().lifecycle_wire);
    REQUIRE(committed);
    CHECK(committed.notice->source_sequence == 2);
    REQUIRE(std::holds_alternative<ProposalCommitted>(
        committed.notice->fact));
    CHECK(std::get<ProposalCommitted>(committed.notice->fact).proposal ==
          exact_proposal);
    deliver_and_release(outbox, 3);

    diagnostics = outbox.diagnostics();
    CHECK(diagnostics.pending_reports == 0);
    CHECK(diagnostics.delivered_reports == 3);
    CHECK(diagnostics.temporary_failures == 1);
}

TEST_CASE(
    "temporary delivery failures retry immutable bytes with capped backoff",
    "[adaptive-v2][reporting-outbox][retry][backoff][bytes]")
{
    AdaptiveV2ReportingOutbox outbox(config(
        limits(16, 64 * 1024, 4, 10, 25)));
    const auto exact_proposal = proposal(
        configuration(8, 1, "retry-epoch"), "retry-proposal");
    REQUIRE(outbox.enqueue_lifecycle(
                ProposalLifecycleFact{
                    ProposalCommitted{exact_proposal}}) ==
            AdaptiveV2ReportingEnqueueStatus::queued);
    REQUIRE(outbox.front() != nullptr);
    const auto retained_bytes = outbox.front()->canonical_payload;

    const auto first = outbox.begin_delivery(100);
    REQUIRE(first.status ==
            AdaptiveV2ReportingAttemptStatus::started);
    REQUIRE(first.token.has_value());
    CHECK(first.token->attempt_number == 1);
    CHECK(outbox.acknowledge_delivery(
              *first.token,
              AdaptiveV2ReportingDeliveryResult::temporary_failure,
              100) ==
          AdaptiveV2ReportingTransitionStatus::retry_scheduled);
    REQUIRE(outbox.front() != nullptr);
    CHECK(outbox.front()->delivery_state ==
          AdaptiveV2ReportingDeliveryState::retry_wait);
    CHECK(outbox.front()->next_attempt_monotonic_ns == 110);
    CHECK(outbox.front()->canonical_payload == retained_bytes);

    CHECK(outbox.begin_delivery(109).status ==
          AdaptiveV2ReportingAttemptStatus::retry_not_due);
    const auto second = outbox.begin_delivery(110);
    REQUIRE(second.status ==
            AdaptiveV2ReportingAttemptStatus::started);
    REQUIRE(second.token.has_value());
    CHECK(second.token->attempt_number == 2);
    CHECK(outbox.acknowledge_delivery(
              *second.token,
              AdaptiveV2ReportingDeliveryResult::temporary_failure,
              110) ==
          AdaptiveV2ReportingTransitionStatus::retry_scheduled);
    REQUIRE(outbox.front() != nullptr);
    CHECK(outbox.front()->next_attempt_monotonic_ns == 130);
    CHECK(outbox.front()->canonical_payload == retained_bytes);

    const auto third = outbox.begin_delivery(130);
    REQUIRE(third.status ==
            AdaptiveV2ReportingAttemptStatus::started);
    REQUIRE(third.token.has_value());
    CHECK(third.token->attempt_number == 3);
    CHECK(outbox.acknowledge_delivery(
              *third.token,
              AdaptiveV2ReportingDeliveryResult::temporary_failure,
              130) ==
          AdaptiveV2ReportingTransitionStatus::retry_scheduled);
    REQUIRE(outbox.front() != nullptr);
    CHECK(outbox.front()->next_attempt_monotonic_ns == 155);
    CHECK(outbox.front()->canonical_payload == retained_bytes);

    const auto fourth = outbox.begin_delivery(155);
    REQUIRE(fourth.status ==
            AdaptiveV2ReportingAttemptStatus::started);
    REQUIRE(fourth.token.has_value());
    CHECK(fourth.token->attempt_number == 4);
    CHECK(outbox.acknowledge_delivery(
              *fourth.token,
              AdaptiveV2ReportingDeliveryResult::delivered,
              155) ==
          AdaptiveV2ReportingTransitionStatus::delivered);
    REQUIRE(outbox.front() != nullptr);
    CHECK(outbox.front()->delivery_state ==
          AdaptiveV2ReportingDeliveryState::delivered);

    const auto before_duplicate = outbox.diagnostics();
    CHECK(outbox.acknowledge_delivery(
              *fourth.token,
              AdaptiveV2ReportingDeliveryResult::delivered,
              156) ==
          AdaptiveV2ReportingTransitionStatus::duplicate);
    const auto after_duplicate = outbox.diagnostics();
    CHECK(after_duplicate.delivered_reports ==
          before_duplicate.delivered_reports);
    CHECK(after_duplicate.duplicate_acknowledgements ==
          before_duplicate.duplicate_acknowledgements + 1);
    CHECK(outbox.front()->canonical_payload == retained_bytes);
}

TEST_CASE(
    "retry exhaustion and permanent failure are explicit bounded terminal states",
    "[adaptive-v2][reporting-outbox][exhaustion][failed][bounded]")
{
    SECTION("temporary failures exhaust the configured attempt bound")
    {
        AdaptiveV2ReportingOutbox outbox(config(
            limits(4, 4096, 2, 5, 5)));
        REQUIRE(outbox.enqueue_readiness(
                    configuration(2, 0, "exhaustion"), 3, 9) ==
                AdaptiveV2ReportingEnqueueStatus::queued);

        const auto first = outbox.begin_delivery(10);
        REQUIRE(first.token.has_value());
        REQUIRE(outbox.acknowledge_delivery(
                    *first.token,
                    AdaptiveV2ReportingDeliveryResult::temporary_failure,
                    10) ==
                AdaptiveV2ReportingTransitionStatus::retry_scheduled);
        const auto second = outbox.begin_delivery(15);
        REQUIRE(second.token.has_value());
        CHECK(outbox.acknowledge_delivery(
                  *second.token,
                  AdaptiveV2ReportingDeliveryResult::temporary_failure,
                  15) ==
              AdaptiveV2ReportingTransitionStatus::failed);
        REQUIRE(outbox.front() != nullptr);
        CHECK(outbox.front()->delivery_state ==
              AdaptiveV2ReportingDeliveryState::failed);
        CHECK(outbox.front()->failure_reason ==
              AdaptiveV2ReportingFailureReason::retry_exhausted);
        CHECK(outbox.front()->delivery_attempts == 2);
        CHECK_FALSE(outbox.healthy());
        CHECK(outbox.begin_delivery(20).status ==
              AdaptiveV2ReportingAttemptStatus::unhealthy);
        CHECK(outbox.release_terminal(outbox.front()->report_id) ==
              AdaptiveV2ReportingReleaseStatus::unhealthy);
        CHECK(outbox.diagnostics().pending_reports == 1);
        CHECK(outbox.diagnostics().retry_exhaustions == 1);
    }

    SECTION("permanent failure does not enter retry state")
    {
        AdaptiveV2ReportingOutbox outbox(config());
        REQUIRE(outbox.enqueue_lifecycle(
                    ProposalLifecycleFact{
                        ProposalCommitted{proposal(
                            configuration(3, 1, "permanent"),
                            "permanent")}}) ==
                AdaptiveV2ReportingEnqueueStatus::queued);
        const auto attempt = outbox.begin_delivery(1);
        REQUIRE(attempt.token.has_value());
        CHECK(outbox.acknowledge_delivery(
                  *attempt.token,
                  AdaptiveV2ReportingDeliveryResult::permanent_failure,
                  1) ==
              AdaptiveV2ReportingTransitionStatus::failed);
        REQUIRE(outbox.front() != nullptr);
        CHECK(outbox.front()->failure_reason ==
              AdaptiveV2ReportingFailureReason::permanent_failure);
        CHECK_FALSE(outbox.healthy());

        const auto blocked_configuration =
            configuration(4, 1, "blocked-unhealthy");
        CHECK(outbox.enqueue_readiness(
                  blocked_configuration, 2, 2) ==
              AdaptiveV2ReportingEnqueueStatus::unhealthy);
        CHECK(outbox.enqueue_lifecycle(
                  ProposalLifecycleFact{
                      ProposalCommitted{proposal(
                          blocked_configuration,
                          "blocked-unhealthy")}}) ==
              AdaptiveV2ReportingEnqueueStatus::unhealthy);
        CHECK(outbox.enqueue_evidence(evidence_payload({
                  observation(
                      1,
                      proposal(
                          blocked_configuration,
                          "blocked-evidence"))})) ==
              AdaptiveV2ReportingEnqueueStatus::unhealthy);
        CHECK(outbox.begin_delivery(2).status ==
              AdaptiveV2ReportingAttemptStatus::unhealthy);
        CHECK(outbox.acknowledge_delivery(
                  *attempt.token,
                  AdaptiveV2ReportingDeliveryResult::permanent_failure,
                  2) ==
              AdaptiveV2ReportingTransitionStatus::unhealthy);
        CHECK(outbox.release_terminal(
                  outbox.front()->report_id) ==
              AdaptiveV2ReportingReleaseStatus::unhealthy);
        CHECK(outbox.diagnostics().pending_reports == 1);
    }

    SECTION("retry deadline overflow fails closed and stays unhealthy")
    {
        AdaptiveV2ReportingOutbox outbox(config(
            limits(4, 4096, 3, 5, 5)));
        REQUIRE(outbox.enqueue_readiness(
                    configuration(4, 0, "overflow"), 1, 10) ==
                AdaptiveV2ReportingEnqueueStatus::queued);
        const auto attempt = outbox.begin_delivery(
            std::numeric_limits<std::uint64_t>::max() - 4);
        REQUIRE(attempt.token.has_value());
        CHECK(outbox.acknowledge_delivery(
                  *attempt.token,
                  AdaptiveV2ReportingDeliveryResult::temporary_failure,
                  std::numeric_limits<std::uint64_t>::max() - 4) ==
              AdaptiveV2ReportingTransitionStatus::failed);
        REQUIRE(outbox.front() != nullptr);
        CHECK(outbox.front()->failure_reason ==
              AdaptiveV2ReportingFailureReason::backoff_overflow);
        CHECK_FALSE(outbox.healthy());
    }

    SECTION("lifecycle wire reserves its maximum sequence value")
    {
        AdaptiveV2ReportingOutbox outbox(config(
            limits(),
            0,
            std::numeric_limits<std::uint64_t>::max() - 2));
        const auto exact_proposal = proposal(
            configuration(5, 1, "lifecycle-exhaustion"),
            "lifecycle-exhaustion");
        REQUIRE(outbox.enqueue_lifecycle(
                    ProposalLifecycleFact{
                        ProposalCommitted{exact_proposal}}) ==
                AdaptiveV2ReportingEnqueueStatus::queued);
        REQUIRE(outbox.front() != nullptr);
        CHECK(outbox.front()->first_stream_sequence ==
              std::numeric_limits<std::uint64_t>::max() - 1);
        const auto final_legal_notice =
            hotstuff::decode_proposal_lifecycle_notice(
                outbox.front()->canonical_payload,
                limits().lifecycle_wire);
        REQUIRE(final_legal_notice);
        CHECK(final_legal_notice.notice->source_sequence ==
              std::numeric_limits<std::uint64_t>::max() - 1);
        CHECK(outbox.enqueue_lifecycle(
                  ProposalLifecycleFact{
                      ProposalCommitted{exact_proposal}}) ==
              AdaptiveV2ReportingEnqueueStatus::sequence_exhausted);
        CHECK(outbox.diagnostics().last_lifecycle_sequence ==
              std::numeric_limits<std::uint64_t>::max() - 1);
        CHECK(outbox.diagnostics().pending_reports == 1);
    }

    SECTION("queue capacity failure consumes no stream sequence")
    {
        AdaptiveV2ReportingOutbox outbox(config(limits(1)));
        const auto active = configuration(1, 0, "capacity");
        REQUIRE(outbox.enqueue_readiness(active, 1, 0) ==
                AdaptiveV2ReportingEnqueueStatus::queued);
        CHECK(outbox.enqueue_readiness(active, 1, 1) ==
              AdaptiveV2ReportingEnqueueStatus::capacity_exceeded);
        CHECK(outbox.diagnostics().last_readiness_sequence == 1);
        deliver_and_release(outbox, 1);
        REQUIRE(outbox.enqueue_readiness(active, 1, 1) ==
                AdaptiveV2ReportingEnqueueStatus::queued);
        REQUIRE(outbox.front() != nullptr);
        CHECK(outbox.front()->first_stream_sequence == 2);
    }
}

TEST_CASE(
    "evidence remains caller-created canonical ordered and source exact",
    "[adaptive-v2][reporting-outbox][evidence][sequence][no-synthesis]")
{
    AdaptiveV2ReportingOutbox outbox(config(limits(), 0, 0, 5));
    const auto exact_proposal = proposal(
        configuration(9, 4, "evidence-order"), "evidence-order");
    const auto first_payload = evidence_payload(
        {observation(6, exact_proposal)});
    REQUIRE(outbox.enqueue_evidence(first_payload) ==
            AdaptiveV2ReportingEnqueueStatus::queued);
    REQUIRE(outbox.front() != nullptr);
    CHECK(outbox.front()->canonical_payload == first_payload);

    CHECK(outbox.enqueue_evidence(first_payload) ==
          AdaptiveV2ReportingEnqueueStatus::sequence_regression);
    CHECK(outbox.enqueue_evidence(evidence_payload({
              observation(8, exact_proposal),
              observation(7, exact_proposal)})) ==
          AdaptiveV2ReportingEnqueueStatus::sequence_regression);
    CHECK(outbox.enqueue_evidence(evidence_payload({
              observation(7, exact_proposal, 99)})) ==
          AdaptiveV2ReportingEnqueueStatus::invalid_payload);
    CHECK(outbox.diagnostics().last_evidence_sequence == 6);
    CHECK(outbox.diagnostics().pending_reports == 1);
}
