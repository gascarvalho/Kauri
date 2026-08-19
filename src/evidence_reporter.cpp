#include "hotstuff/evidence_reporter.h"

#include <algorithm>
#include <deque>
#include <limits>
#include <new>
#include <utility>

namespace hotstuff
{
namespace
{

bool valid_message_type(ExpectedMessageType type) noexcept
{
    return type == ExpectedMessageType::direct_vote ||
           type == ExpectedMessageType::aggregate_relay;
}

bool valid_outcome(ResponseOutcome outcome) noexcept
{
    return outcome == ResponseOutcome::on_time ||
           outcome == ResponseOutcome::timeout ||
           outcome == ResponseOutcome::late;
}

bool canonical_signers(
    const std::vector<ReplicaID> &signers) noexcept
{
    return std::adjacent_find(
               signers.begin(), signers.end(),
               [](ReplicaID left, ReplicaID right) {
                   return left >= right;
               }) == signers.end();
}

bool valid_fact(
    const ResponseAttemptFact &fact,
    const EvidenceWireLimits &limits) noexcept
{
    const bool has_attempt_start =
        fact.attempt_start_monotonic_ns != 0;
    const bool has_local_commit =
        fact.reporter_local_commit_monotonic_ns != 0;
    if (has_local_commit && !has_attempt_start)
        return false;
    if (has_attempt_start && has_local_commit)
    {
        if (fact.outcome != ResponseOutcome::timeout ||
            fact.response_duration_us != 0 ||
            !fact.signer_set.empty() ||
            fact.deadline_duration_us == 0 ||
            fact.deadline_duration_us >
                std::numeric_limits<std::uint64_t>::max() / 1'000)
        {
            return false;
        }
        const auto deadline_duration_ns =
            fact.deadline_duration_us * 1'000;
        if (fact.attempt_start_monotonic_ns >
            std::numeric_limits<std::uint64_t>::max() -
                deadline_duration_ns)
        {
            return false;
        }
        const auto absolute_deadline_ns =
            fact.attempt_start_monotonic_ns + deadline_duration_ns;
        if (fact.reporter_local_commit_monotonic_ns <
                fact.attempt_start_monotonic_ns ||
            fact.reporter_local_commit_monotonic_ns >=
                absolute_deadline_ns ||
            absolute_deadline_ns > fact.fact_monotonic_ns)
        {
            return false;
        }
    }
    if (!valid_message_type(fact.key.expected_message_type) ||
        !valid_outcome(fact.outcome) ||
        fact.deadline_duration_us == 0 ||
        fact.signer_set.size() > limits.maximum_signers_per_observation ||
        !canonical_signers(fact.signer_set))
    {
        return false;
    }

    if (fact.outcome == ResponseOutcome::timeout)
    {
        return fact.response_duration_us == 0 &&
               fact.signer_set.empty();
    }

    if (fact.signer_set.empty())
        return false;
    if (fact.outcome == ResponseOutcome::late &&
        fact.response_duration_us < fact.deadline_duration_us)
    {
        return false;
    }

    if (fact.key.expected_message_type ==
        ExpectedMessageType::direct_vote)
    {
        return fact.signer_set.size() == 1 &&
               fact.signer_set.front() ==
                   fact.key.observed_replica_id;
    }
    return true;
}

void increment(std::uint64_t &value) noexcept
{
    if (value != std::numeric_limits<std::uint64_t>::max())
        ++value;
}

} // namespace

struct EvidenceReporter::State
{
    explicit State(EvidenceReporterConfig configured)
        : config(std::move(configured))
    {
        diagnostics.last_reporter_sequence =
            config.initial_reporter_sequence;
        diagnostics.last_reporter_monotonic_ns =
            config.initial_reporter_monotonic_ns;
        configured_limits =
            config.limits.maximum_pending_reports != 0 &&
            config.wire_limits.maximum_payload_bytes != 0 &&
            config.wire_limits.maximum_observations != 0 &&
            config.wire_limits.maximum_signers_per_observation != 0;
        if (!configured_limits)
            diagnostics.healthy = false;
    }

    EvidenceReporterConfig config;
    std::deque<PendingEvidenceReport> pending;
    EvidenceReporterDiagnostics diagnostics;
    bool configured_limits{false};
};

EvidenceReporter::EvidenceReporter(EvidenceReporterConfig config)
    : state_(std::make_unique<State>(std::move(config)))
{}

EvidenceReporter::~EvidenceReporter() = default;

bool EvidenceReporter::enable_exact_timeout_attempt_evidence_v3() noexcept
{
    if (!state_->diagnostics.healthy || state_->diagnostics.stopped ||
        !state_->pending.empty())
        return false;
    state_->config.exact_timeout_attempt_evidence_v3 = true;
    return true;
}

bool EvidenceReporter::enqueue(const ResponseAttemptFact &fact)
{
    if (state_->diagnostics.stopped || !state_->configured_limits)
        return false;

    if (state_->pending.size() >=
        state_->config.limits.maximum_pending_reports)
    {
        increment(state_->diagnostics.capacity_failures);
        state_->diagnostics.healthy = false;
        return false;
    }

    if (state_->diagnostics.last_reporter_sequence >=
        std::numeric_limits<std::uint64_t>::max() - 1)
    {
        increment(state_->diagnostics.sequence_overflows);
        state_->diagnostics.healthy = false;
        return false;
    }

    if (fact.fact_monotonic_ns <
        state_->diagnostics.last_reporter_monotonic_ns)
    {
        increment(state_->diagnostics.timestamp_regressions);
        state_->diagnostics.healthy = false;
        return false;
    }

    if (!valid_fact(fact, state_->config.wire_limits))
    {
        increment(state_->diagnostics.rejected_facts);
        state_->diagnostics.healthy = false;
        return false;
    }
    if (state_->config.exact_timeout_attempt_evidence_v3 &&
        (fact.attempt_start_monotonic_ns == 0 ||
         fact.fact_monotonic_ns < fact.attempt_start_monotonic_ns ||
         fact.deadline_duration_us == 0))
    {
        increment(state_->diagnostics.rejected_facts);
        state_->diagnostics.healthy = false;
        return false;
    }

    const auto next_sequence =
        state_->diagnostics.last_reporter_sequence + 1;
    try
    {
        ResponseObservation observation;
        if (state_->config.exact_timeout_attempt_evidence_v3)
        {
            observation.schema_version =
                kResponseObservationSchemaVersionV3;
        }
        else if (fact.attempt_start_monotonic_ns != 0)
        {
            observation.schema_version =
                kResponseObservationSchemaVersionV2;
        }
        observation.reporter_id =
            state_->config.trusted_reporter_id;
        observation.observed_replica_id =
            fact.key.observed_replica_id;
        observation.configuration = fact.key.proposal.configuration;
        observation.block_hash = fact.key.proposal.block_hash;
        observation.expected_message_type =
            fact.key.expected_message_type;
        observation.outcome = fact.outcome;
        observation.response_duration_us =
            fact.response_duration_us;
        observation.deadline_duration_us =
            fact.deadline_duration_us;
        observation.reporter_monotonic_ns =
            fact.fact_monotonic_ns;
        observation.reporter_sequence = next_sequence;
        observation.attempt_start_monotonic_ns =
            fact.attempt_start_monotonic_ns;
        observation.reporter_local_commit_monotonic_ns =
            fact.reporter_local_commit_monotonic_ns;
        observation.signer_set = fact.signer_set;
        observation.observation_id =
            compute_response_observation_id(observation);

        ResponseObservationBatch batch;
        batch.observations.push_back(observation);
        auto payload = encode_evidence_batch(
            batch, state_->config.wire_limits);

        EvidenceReportEnvelope envelope;
        envelope.observation = std::move(observation);
        envelope.canonical_payload = std::move(payload);
        PendingEvidenceReport pending;
        pending.envelope = std::move(envelope);
        state_->pending.push_back(std::move(pending));
    }
    catch (const std::bad_alloc &)
    {
        increment(state_->diagnostics.allocation_failures);
        state_->diagnostics.healthy = false;
        return false;
    }
    catch (...)
    {
        increment(state_->diagnostics.payload_failures);
        state_->diagnostics.healthy = false;
        return false;
    }

    state_->diagnostics.last_reporter_sequence = next_sequence;
    state_->diagnostics.last_reporter_monotonic_ns =
        fact.fact_monotonic_ns;
    return true;
}

std::optional<EvidenceTransportResult> EvidenceReporter::dispatch_one(
    const EvidenceTransportCallback &transport)
{
    if (state_->diagnostics.stopped || state_->pending.empty() ||
        state_->pending.front().permanently_failed)
    {
        return std::nullopt;
    }

    auto &pending = state_->pending.front();
    increment(pending.delivery_attempts);

    EvidenceTransportResult result;
    try
    {
        result = transport(pending.envelope);
    }
    catch (...)
    {
        increment(pending.callback_exceptions);
        increment(state_->diagnostics.callback_exceptions);
        state_->diagnostics.healthy = false;
        throw;
    }

    switch (result)
    {
    case EvidenceTransportResult::accepted:
        state_->pending.pop_front();
        increment(state_->diagnostics.accepted_reports);
        return result;
    case EvidenceTransportResult::temporary_failure:
        increment(pending.temporary_failures);
        increment(state_->diagnostics.temporary_failures);
        return result;
    case EvidenceTransportResult::permanent_failure:
        pending.permanently_failed = true;
        increment(state_->diagnostics.permanent_failures);
        state_->diagnostics.healthy = false;
        return result;
    }

    pending.permanently_failed = true;
    increment(state_->diagnostics.permanent_failures);
    state_->diagnostics.healthy = false;
    return EvidenceTransportResult::permanent_failure;
}

const PendingEvidenceReport *EvidenceReporter::front() const noexcept
{
    if (state_->pending.empty())
        return nullptr;
    return &state_->pending.front();
}

std::size_t EvidenceReporter::pending_size() const noexcept
{
    return state_->pending.size();
}

EvidenceReporterDiagnostics EvidenceReporter::diagnostics() const noexcept
{
    auto result = state_->diagnostics;
    result.pending_reports = state_->pending.size();
    return result;
}

bool EvidenceReporter::healthy() const noexcept
{
    return state_->diagnostics.healthy;
}

void EvidenceReporter::shutdown() noexcept
{
    state_->diagnostics.stopped = true;
}

} // namespace hotstuff
