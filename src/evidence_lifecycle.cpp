#include "hotstuff/evidence_lifecycle.h"

#include <limits>
#include <map>
#include <set>
#include <stdexcept>
#include <type_traits>
#include <utility>

namespace hotstuff
{
namespace
{

constexpr std::size_t kCanonicalObservationFixedBytes = 150;

template <typename Value>
bool checked_add(Value current, Value increment, Value limit) noexcept
{
    static_assert(std::is_unsigned<Value>::value,
                  "bounded evidence counters are unsigned");
    return current <= limit && increment <= limit - current;
}

std::optional<EvidenceRetentionCost> retention_cost(
    const ResponseObservation &observation) noexcept
{
    if (observation.signer_set.size() >
        (std::numeric_limits<std::size_t>::max() -
         kCanonicalObservationFixedBytes) /
            sizeof(ReplicaID))
    {
        return std::nullopt;
    }
    return EvidenceRetentionCost{
        kCanonicalObservationFixedBytes +
            observation.signer_set.size() * sizeof(ReplicaID),
        observation.signer_set.size()};
}

struct QuarantineFactKey
{
    uint256_t observation_id;
    ResponseOutcome outcome{ResponseOutcome::on_time};

    bool operator<(const QuarantineFactKey &other) const noexcept
    {
        if (observation_id != other.observation_id)
            return observation_id < other.observation_id;
        return static_cast<std::uint8_t>(outcome) <
               static_cast<std::uint8_t>(other.outcome);
    }
};

QuarantineFactKey fact_key(
    const ResponseObservation &observation) noexcept
{
    return {observation.observation_id, observation.outcome};
}

} // namespace

EvidenceLifecycleAccounting::EvidenceLifecycleAccounting(
    EvidenceLifecycleAccountingLimits limits) noexcept
    : limits_(limits)
{}

bool EvidenceLifecycleAccounting::try_retain(
    EvidenceRetentionCost cost) noexcept
{
    if (!checked_add(
            stats_.retained_records,
            std::size_t{1},
            limits_.maximum_records) ||
        !checked_add(
            stats_.retained_canonical_bytes,
            cost.canonical_bytes,
            limits_.maximum_canonical_bytes) ||
        !checked_add(
            stats_.retained_signer_entries,
            cost.signer_entries,
            limits_.maximum_signer_entries))
    {
        return false;
    }

    auto retained = retained_costs_.find(cost);
    if (retained != retained_costs_.end())
    {
        if (retained->second == std::numeric_limits<std::size_t>::max())
            return false;
        ++retained->second;
    }
    else
    {
        try
        {
            retained_costs_.emplace(cost, std::size_t{1});
        }
        catch (...)
        {
            return false;
        }
    }

    ++stats_.retained_records;
    stats_.retained_canonical_bytes += cost.canonical_bytes;
    stats_.retained_signer_entries += cost.signer_entries;
    return true;
}

bool EvidenceLifecycleAccounting::release(
    EvidenceRetentionCost cost) noexcept
{
    const auto retained = retained_costs_.find(cost);
    if (retained == retained_costs_.end() || retained->second == 0 ||
        stats_.retained_records == 0 ||
        cost.canonical_bytes > stats_.retained_canonical_bytes ||
        cost.signer_entries > stats_.retained_signer_entries)
    {
        return false;
    }

    --stats_.retained_records;
    stats_.retained_canonical_bytes -= cost.canonical_bytes;
    stats_.retained_signer_entries -= cost.signer_entries;
    if (retained->second == 1)
        retained_costs_.erase(retained);
    else
        --retained->second;
    return true;
}

EvidenceLifecycleAccountingLimits
EvidenceLifecycleAccounting::limits() const noexcept
{
    return limits_;
}

EvidenceLifecycleAccountingStats
EvidenceLifecycleAccounting::stats() const noexcept
{
    return stats_;
}

struct ProposalLifecycleEvidenceCoordinator::State
{
    struct RetainedObservation
    {
        AuthenticatedReporter authenticated_reporter;
        ResponseObservation observation;
        EvidenceRetentionCost cost;
        QuarantineFactKey key;
    };

    using ReporterQueues =
        std::map<ReplicaID, std::vector<RetainedObservation>>;
    using DeduplicationIndex = std::set<QuarantineFactKey>;
    using LifecycleSources = std::map<ReplicaID, std::uint64_t>;

    State(ProposalEvidenceIndex &proposal_index_,
          const ProposalEvidenceWindow &proposal_window_,
          EvidenceLedger &ledger_,
          EvidenceLifecycleAccounting accounting_,
          EvidenceLifecycleLimits limits_) noexcept
        : proposal_index(proposal_index_),
          proposal_window(proposal_window_),
          ledger(ledger_),
          accounting(std::move(accounting_)),
          limits(limits_),
          healthy(valid_limits(limits_) &&
                  proposal_index_.healthy() &&
                  !proposal_index_.stats().stopped &&
                  ledger_.healthy())
    {}

    static bool valid_limits(
        const EvidenceLifecycleLimits &candidate) noexcept
    {
        return candidate.maximum_quarantined_records != 0 &&
               candidate.maximum_quarantined_bytes != 0 &&
               candidate.maximum_reporter_queues != 0 &&
               candidate.maximum_signer_entries != 0 &&
               candidate.maximum_deduplication_entries != 0 &&
               candidate.maximum_lifecycle_sources != 0 &&
               candidate.maximum_quarantined_records_per_reporter != 0;
    }

    std::size_t retained_records() const noexcept
    {
        return accounting.stats().retained_records;
    }

    void fail_closed() noexcept
    {
        if (!healthy)
            return;
        healthy = false;
        if (capacity_failures !=
            std::numeric_limits<std::uint64_t>::max())
        {
            ++capacity_failures;
        }
    }

    bool borrowed_state_healthy() const noexcept
    {
        const auto index_stats = proposal_index.stats();
        return proposal_index.healthy() && !index_stats.stopped &&
               ledger.healthy();
    }

    bool remove_committed_head(
        ReporterQueues::iterator reporter) noexcept
    {
        auto &queue = reporter->second;
        if (queue.empty())
            return false;

        const auto cost = queue.front().cost;
        const auto key = queue.front().key;
        if (!accounting.release(cost))
            return false;
        deduplication.erase(key);
        queue.erase(queue.begin());
        if (queue.empty())
            reporter_queues.erase(reporter);
        return true;
    }

    bool drain(
        ProposalLifecycleApplyResult &result,
        std::optional<ReplicaID> only_reporter = std::nullopt) noexcept
    {
        auto reporter = only_reporter.has_value()
            ? reporter_queues.find(*only_reporter)
            : reporter_queues.begin();
        while (reporter != reporter_queues.end())
        {
            if (reporter->second.empty())
            {
                reporter = reporter_queues.erase(reporter);
                continue;
            }

            const auto status = proposal_window.classify_for_reporter(
                reporter->second.front().authenticated_reporter,
                reporter->second.front().observation);
            if (status == ProposalEvidenceStatus::unknown)
            {
                if (only_reporter.has_value())
                    return true;
                ++reporter;
                continue;
            }

            const auto accepted_before = ledger.accepted().size();
            const auto rejected_before = ledger.rejected().size();
            bool threw = false;
            try
            {
                ledger.ingest(
                    reporter->second.front().authenticated_reporter,
                    reporter->second.front().observation);
            }
            catch (...)
            {
                threw = true;
            }

            const auto accepted_after = ledger.accepted().size();
            const auto rejected_after = ledger.rejected().size();
            const auto accepted = accepted_after - accepted_before;
            const auto rejected = rejected_after - rejected_before;
            const bool one_audited_record =
                accepted <= 1 && rejected <= 1 &&
                accepted + rejected == 1;

            if (one_audited_record)
            {
                if (!remove_committed_head(reporter))
                {
                    fail_closed();
                    return false;
                }
                ++result.retried_observations;
                result.accepted_observations += accepted;
                result.rejected_observations += rejected;
            }

            if (threw || !ledger.healthy() || !one_audited_record)
            {
                fail_closed();
                return false;
            }

            reporter = only_reporter.has_value()
                ? reporter_queues.find(*only_reporter)
                : reporter_queues.begin();
        }
        return true;
    }

    ProposalEvidenceIndex &proposal_index;
    const ProposalEvidenceWindow &proposal_window;
    EvidenceLedger &ledger;
    EvidenceLifecycleAccounting accounting;
    EvidenceLifecycleLimits limits;
    ReporterQueues reporter_queues;
    DeduplicationIndex deduplication;
    LifecycleSources lifecycle_sources;
    std::uint64_t duplicate_observations{0};
    std::uint64_t applied_lifecycle_notices{0};
    std::uint64_t quarantine_quota_rejections{0};
    std::uint64_t capacity_failures{0};
    bool healthy{true};
    bool stopped{false};
};

ProposalLifecycleEvidenceCoordinator::
    ProposalLifecycleEvidenceCoordinator(
        ProposalEvidenceIndex &proposal_index,
        EvidenceLedger &ledger,
        EvidenceLifecycleAccounting &&accounting,
        EvidenceLifecycleLimits limits)
    : ProposalLifecycleEvidenceCoordinator(
          proposal_index,
          proposal_index,
          ledger,
          std::move(accounting),
          limits)
{}

ProposalLifecycleEvidenceCoordinator::
    ProposalLifecycleEvidenceCoordinator(
        ProposalEvidenceIndex &proposal_index,
        const ProposalEvidenceWindow &proposal_window,
        EvidenceLedger &ledger,
        EvidenceLifecycleAccounting &&accounting,
        EvidenceLifecycleLimits limits)
{
    const auto retained = accounting.stats();
    if (retained.retained_records != 0 ||
        retained.retained_canonical_bytes != 0 ||
        retained.retained_signer_entries != 0)
    {
        throw std::invalid_argument{
            "lifecycle coordinator accounting must be empty"};
    }

    state_ = std::make_unique<State>(
        proposal_index,
        proposal_window,
        ledger,
        std::move(accounting),
        limits);
}

ProposalLifecycleEvidenceCoordinator::
    ~ProposalLifecycleEvidenceCoordinator() = default;

ProposalLifecycleApplyResult
ProposalLifecycleEvidenceCoordinator::apply_notice(
    const AuthenticatedReporter &authenticated_source,
    const ProposalLifecycleNotice &notice) noexcept
{
    auto &state = *state_;
    ProposalLifecycleApplyResult result;
    result.remaining_quarantined = state.retained_records();

    if (state.stopped)
    {
        result.status = ProposalLifecycleApplyStatus::stopped;
        return result;
    }
    if (!state.healthy || !state.borrowed_state_healthy())
    {
        state.fail_closed();
        result.status = ProposalLifecycleApplyStatus::evidence_unhealthy;
        return result;
    }
    if (notice.schema_version != kProposalLifecycleNoticeSchemaVersion)
    {
        result.status = ProposalLifecycleApplyStatus::rejected_schema;
        return result;
    }
    if (authenticated_source.replica_id != notice.source_replica_id)
    {
        result.status =
            ProposalLifecycleApplyStatus::rejected_authentication;
        return result;
    }

    const auto existing_source =
        state.lifecycle_sources.find(notice.source_replica_id);
    if (existing_source != state.lifecycle_sources.end() &&
        notice.source_sequence <= existing_source->second)
    {
        result.status = ProposalLifecycleApplyStatus::rejected_sequence;
        return result;
    }
    if (existing_source == state.lifecycle_sources.end() &&
        state.lifecycle_sources.size() >=
            state.limits.maximum_lifecycle_sources)
    {
        state.fail_closed();
        result.status = ProposalLifecycleApplyStatus::evidence_unhealthy;
        return result;
    }
    if (state.applied_lifecycle_notices ==
        std::numeric_limits<std::uint64_t>::max())
    {
        state.fail_closed();
        result.status = ProposalLifecycleApplyStatus::evidence_unhealthy;
        return result;
    }

    const bool admission_notice =
        std::holds_alternative<NormalProposalRuntimeInitialized>(
            notice.fact);

    const auto prepare_index = [&]() {
        return std::visit(
            [&state](const auto &fact) {
                using Fact = std::decay_t<decltype(fact)>;
                if constexpr (std::is_same<
                                  Fact,
                                  NormalProposalRuntimeInitialized>::value)
                {
                    return state.proposal_index.prepare_admit(
                        fact.proposal);
                }
                else if constexpr (std::is_same<
                                       Fact,
                                       ProposalRuntimeAborted>::value)
                {
                    return state.proposal_index.
                        prepare_stale_if_admitted(fact.proposal);
                }
                else if constexpr (std::is_same<
                                       Fact,
                                       ProposalCommitted>::value)
                {
                    return state.proposal_index.prepare_mark_stale(
                        fact.proposal);
                }
                else if constexpr (std::is_same<
                                       Fact,
                                       ProposalConfigurationRetired>::value)
                {
                    return state.proposal_index.
                        prepare_retire_configuration(
                            fact.configuration);
                }
                else
                {
                    return state.proposal_index.
                        prepare_advance_retirement_floor(
                            fact.first_live_epoch);
                }
            },
            notice.fact);
    };

    try
    {
        if (admission_notice)
        {
            auto staged_sources = state.lifecycle_sources;
            staged_sources[notice.source_replica_id] =
                notice.source_sequence;
            auto prepared = prepare_index();
            result.index_changed = prepared.changed();
            if (!prepared.commit())
            {
                throw std::logic_error(
                    "prepared index mutation was consumed");
            }
            state.lifecycle_sources.swap(staged_sources);
        }
        else
        {
            auto prepared = prepare_index();
            result.index_changed = prepared.changed();
            if (!prepared.commit())
            {
                throw std::logic_error(
                    "prepared index mutation was consumed");
            }
        }
    }
    catch (...)
    {
        state.fail_closed();
        result.status = ProposalLifecycleApplyStatus::evidence_unhealthy;
        result.index_changed = false;
        result.remaining_quarantined = state.retained_records();
        return result;
    }

    if (!state.drain(result))
    {
        if (admission_notice)
            state.proposal_index.shutdown();
        result.status = ProposalLifecycleApplyStatus::evidence_unhealthy;
        result.remaining_quarantined = state.retained_records();
        return result;
    }

    if (!admission_notice)
    {
        try
        {
            // Lifecycle source sequence is the notice commit marker for
            // irreversible stale/retirement drains. Stage it only after the
            // exact ledger prefix is durable, then publish with a no-throw
            // map swap. A failed stage leaves the fully audited prefix owned
            // and makes every later operation an identical no-op.
            auto staged_sources = state.lifecycle_sources;
            staged_sources[notice.source_replica_id] =
                notice.source_sequence;
            state.lifecycle_sources.swap(staged_sources);
        }
        catch (...)
        {
            state.fail_closed();
            result.status =
                ProposalLifecycleApplyStatus::evidence_unhealthy;
            result.remaining_quarantined = state.retained_records();
            return result;
        }
    }

    ++state.applied_lifecycle_notices;
    result.status = ProposalLifecycleApplyStatus::applied;
    result.remaining_quarantined = state.retained_records();
    return result;
}

ProposalLifecycleApplyResult
ProposalLifecycleEvidenceCoordinator::retry_reporter(
    const AuthenticatedReporter &authenticated_reporter) noexcept
{
    auto &state = *state_;
    ProposalLifecycleApplyResult result;
    result.remaining_quarantined = state.retained_records();

    if (state.stopped)
    {
        result.status = ProposalLifecycleApplyStatus::stopped;
        return result;
    }
    if (!state.healthy || !state.borrowed_state_healthy())
    {
        state.fail_closed();
        result.status = ProposalLifecycleApplyStatus::evidence_unhealthy;
        return result;
    }
    if (!state.drain(result, authenticated_reporter.replica_id))
    {
        result.status = ProposalLifecycleApplyStatus::evidence_unhealthy;
        result.remaining_quarantined = state.retained_records();
        return result;
    }

    result.status = ProposalLifecycleApplyStatus::applied;
    result.remaining_quarantined = state.retained_records();
    return result;
}

EvidenceObservationResult
ProposalLifecycleEvidenceCoordinator::ingest_observation(
    const AuthenticatedReporter &authenticated_reporter,
    const ResponseObservation &observation) noexcept
{
    auto &state = *state_;
    EvidenceObservationResult result;
    result.quarantined_observations = state.retained_records();

    if (state.stopped)
    {
        result.disposition = EvidenceObservationDisposition::stopped;
        return result;
    }
    if (!state.healthy || !state.borrowed_state_healthy())
    {
        state.fail_closed();
        result.disposition =
            EvidenceObservationDisposition::evidence_unhealthy;
        return result;
    }

    const auto reporter =
        state.reporter_queues.find(authenticated_reporter.replica_id);
    const auto proposal_status =
        state.proposal_window.classify_for_reporter(
            authenticated_reporter, observation);
    const bool may_need_quarantine =
        reporter != state.reporter_queues.end() ||
        proposal_status == ProposalEvidenceStatus::unknown;

    if (may_need_quarantine)
    {
        const auto accepted_before = state.ledger.accepted().size();
        const auto rejected_before = state.ledger.rejected().size();
        try
        {
            if (state.ledger.ingest_if_proposal_independent_rejected(
                    authenticated_reporter, observation))
            {
                const auto accepted =
                    state.ledger.accepted().size() - accepted_before;
                const auto rejected =
                    state.ledger.rejected().size() - rejected_before;
                if (!state.ledger.healthy() || accepted != 0 ||
                    rejected != 1)
                {
                    state.fail_closed();
                    result.disposition =
                        EvidenceObservationDisposition::evidence_unhealthy;
                    return result;
                }
                result.disposition =
                    EvidenceObservationDisposition::ingested;
                result.rejected_observations = 1;
                return result;
            }
        }
        catch (...)
        {
            state.fail_closed();
            result.disposition =
                EvidenceObservationDisposition::evidence_unhealthy;
            return result;
        }
    }

    if (reporter == state.reporter_queues.end() &&
        proposal_status != ProposalEvidenceStatus::unknown)
    {
        const auto accepted_before = state.ledger.accepted().size();
        const auto rejected_before = state.ledger.rejected().size();
        try
        {
            state.ledger.ingest(authenticated_reporter, observation);
        }
        catch (...)
        {
            state.fail_closed();
            result.disposition =
                EvidenceObservationDisposition::evidence_unhealthy;
            return result;
        }

        const auto accepted =
            state.ledger.accepted().size() - accepted_before;
        const auto rejected =
            state.ledger.rejected().size() - rejected_before;
        if (!state.ledger.healthy() || accepted + rejected != 1)
        {
            state.fail_closed();
            result.disposition =
                EvidenceObservationDisposition::evidence_unhealthy;
            return result;
        }
        result.disposition = EvidenceObservationDisposition::ingested;
        result.accepted_observations = accepted;
        result.rejected_observations = rejected;
        return result;
    }

    const auto key = fact_key(observation);
    if (state.deduplication.count(key) != 0)
    {
        if (state.duplicate_observations ==
            std::numeric_limits<std::uint64_t>::max())
        {
            state.fail_closed();
            result.disposition =
                EvidenceObservationDisposition::evidence_unhealthy;
            return result;
        }
        ++state.duplicate_observations;
        result.disposition =
            EvidenceObservationDisposition::duplicate_quarantined;
        return result;
    }

    const auto reporter_records =
        reporter == state.reporter_queues.end()
        ? std::size_t{0}
        : reporter->second.size();
    if (reporter_records >=
        state.limits.maximum_quarantined_records_per_reporter)
    {
        if (state.quarantine_quota_rejections ==
            std::numeric_limits<std::uint64_t>::max())
        {
            state.fail_closed();
            result.disposition =
                EvidenceObservationDisposition::evidence_unhealthy;
            return result;
        }
        ++state.quarantine_quota_rejections;
        result.disposition = EvidenceObservationDisposition::
            rejected_quarantine_capacity;
        result.rejected_observations = 1;
        return result;
    }

    const auto cost = retention_cost(observation);
    const auto accounting = state.accounting.stats();
    const bool new_reporter =
        reporter == state.reporter_queues.end();
    if (!cost.has_value() ||
        accounting.retained_records >=
            state.limits.maximum_quarantined_records ||
        !checked_add(
            accounting.retained_canonical_bytes,
            cost ? cost->canonical_bytes : 0,
            state.limits.maximum_quarantined_bytes) ||
        !checked_add(
            accounting.retained_signer_entries,
            cost ? cost->signer_entries : 0,
            state.limits.maximum_signer_entries) ||
        state.deduplication.size() >=
            state.limits.maximum_deduplication_entries ||
        (new_reporter &&
         state.reporter_queues.size() >=
             state.limits.maximum_reporter_queues))
    {
        state.fail_closed();
        result.disposition =
            EvidenceObservationDisposition::evidence_unhealthy;
        return result;
    }

    try
    {
        auto staged_queues = state.reporter_queues;
        auto staged_deduplication = state.deduplication;
        auto staged_accounting = state.accounting;

        if (!staged_accounting.try_retain(*cost))
        {
            state.fail_closed();
            result.disposition =
                EvidenceObservationDisposition::evidence_unhealthy;
            return result;
        }
        staged_deduplication.insert(key);
        staged_queues[authenticated_reporter.replica_id].push_back(
            State::RetainedObservation{
                authenticated_reporter,
                observation,
                *cost,
                key});

        state.reporter_queues.swap(staged_queues);
        state.deduplication.swap(staged_deduplication);
        static_assert(
            std::is_nothrow_move_assignable<
                EvidenceLifecycleAccounting>::value,
            "accounting commit must not split staged quarantine state");
        state.accounting = std::move(staged_accounting);
    }
    catch (...)
    {
        state.fail_closed();
        result.disposition =
            EvidenceObservationDisposition::evidence_unhealthy;
        return result;
    }

    result.disposition = new_reporter
        ? EvidenceObservationDisposition::quarantined_unknown
        : EvidenceObservationDisposition::quarantined_behind_unknown;
    result.quarantined_observations = state.retained_records();
    return result;
}

EvidenceLifecycleStats
ProposalLifecycleEvidenceCoordinator::stats() const noexcept
{
    const auto &state = *state_;
    const auto accounting = state.accounting.stats();
    const auto index = state.proposal_index.stats();
    return EvidenceLifecycleStats{
        accounting.retained_records,
        accounting.retained_canonical_bytes,
        state.reporter_queues.size(),
        accounting.retained_signer_entries,
        state.deduplication.size(),
        state.lifecycle_sources.size(),
        state.duplicate_observations,
        state.applied_lifecycle_notices,
        state.quarantine_quota_rejections,
        state.capacity_failures,
        state.healthy && state.proposal_index.healthy() &&
            !index.stopped && state.ledger.healthy(),
        state.stopped};
}

const EvidenceLifecycleAccounting &
ProposalLifecycleEvidenceCoordinator::accounting() const noexcept
{
    return state_->accounting;
}

std::vector<QuarantinedEvidenceObservation>
ProposalLifecycleEvidenceCoordinator::quarantined_observations() const
{
    const auto &state = *state_;
    std::vector<QuarantinedEvidenceObservation> observations;
    observations.reserve(state.retained_records());
    for (const auto &reporter : state.reporter_queues)
    {
        bool head = true;
        for (const auto &retained : reporter.second)
        {
            observations.push_back(
                {retained.authenticated_reporter,
                 retained.observation,
                 head});
            head = false;
        }
    }
    return observations;
}

bool ProposalLifecycleEvidenceCoordinator::healthy() const noexcept
{
    return stats().healthy;
}

void ProposalLifecycleEvidenceCoordinator::shutdown() noexcept
{
    state_->stopped = true;
}

} // namespace hotstuff
