#include "hotstuff/evidence_reputation.h"

#include <limits>
#include <map>
#include <utility>

namespace hotstuff
{

namespace
{

struct ScoreMapping
{
    SimpleReputationOutcome outcome;
    int delta;
};

struct PlannedUpdate
{
    const AcceptedEvidenceRecord *record{nullptr};
    ScoreMapping mapping{SimpleReputationOutcome::response, 0};
    int score{0};
};

bool score_mapping(
    ResponseOutcome outcome,
    ScoreMapping &mapping) noexcept
{
    switch (outcome)
    {
    case ResponseOutcome::on_time:
    case ResponseOutcome::late:
        mapping = {SimpleReputationOutcome::response, 1};
        return true;
    case ResponseOutcome::timeout:
        mapping = {SimpleReputationOutcome::timeout, -1};
        return true;
    }
    return false;
}

} // namespace

struct EvidenceReputationProjection::State
{
    State(const EvidenceLedger &ledger_,
          SimpleReputation &reputation_,
          EvidenceReputationLimits limits_) noexcept
        : ledger(ledger_), reputation(reputation_), limits(limits_)
    {
        healthy = limits.maximum_audit_updates != 0;
    }

    EvidenceReputationApplyResult result(
        EvidenceReputationApplyStatus status,
        std::uint64_t requested_cutoff,
        std::size_t applied_updates = 0,
        SimpleReputationDisposition reputation_disposition =
            SimpleReputationDisposition::applied) const noexcept
    {
        return {
            status,
            applied_updates,
            requested_cutoff,
            last_applied_ingestion_sequence,
            reputation_disposition};
    }

    EvidenceReputationApplyResult fail(
        EvidenceReputationApplyStatus status,
        std::uint64_t requested_cutoff,
        std::size_t applied_updates = 0,
        SimpleReputationDisposition reputation_disposition =
            SimpleReputationDisposition::applied) noexcept
    {
        healthy = false;
        return result(
            status,
            requested_cutoff,
            applied_updates,
            reputation_disposition);
    }

    const EvidenceLedger &ledger;
    SimpleReputation &reputation;
    EvidenceReputationLimits limits;
    std::vector<EvidenceReputationAuditUpdate> audit_updates;
    std::uint64_t last_cutoff{0};
    std::uint64_t last_applied_ingestion_sequence{0};
    bool healthy{true};
};

EvidenceReputationProjection::EvidenceReputationProjection(
    const EvidenceLedger &ledger,
    SimpleReputation &reputation,
    EvidenceReputationLimits limits)
    : state_(std::make_unique<State>(ledger, reputation, limits))
{}

EvidenceReputationProjection::~EvidenceReputationProjection() = default;

EvidenceReputationApplyResult
EvidenceReputationProjection::apply_through(
    std::uint64_t evidence_cutoff) noexcept
{
    auto &state = *state_;
    if (!state.healthy)
    {
        return state.result(
            EvidenceReputationApplyStatus::projection_unhealthy,
            evidence_cutoff);
    }
    if (!state.ledger.healthy())
    {
        return state.fail(
            EvidenceReputationApplyStatus::ledger_unhealthy,
            evidence_cutoff);
    }
    if (evidence_cutoff < state.last_cutoff ||
        evidence_cutoff > state.ledger.high_watermark())
    {
        return state.fail(
            EvidenceReputationApplyStatus::invalid_cutoff,
            evidence_cutoff);
    }

    const auto &accepted = state.ledger.accepted();
    std::uint64_t previous_ingestion_sequence = 0;
    bool cursor_seen =
        state.last_applied_ingestion_sequence == 0;
    std::size_t pending_updates = 0;

    for (const auto &record : accepted)
    {
        if (record.ingestion_sequence == 0 ||
            record.ingestion_sequence <= previous_ingestion_sequence)
        {
            return state.fail(
                EvidenceReputationApplyStatus::accepted_order_invalid,
                evidence_cutoff);
        }
        previous_ingestion_sequence = record.ingestion_sequence;

        if (record.ingestion_sequence ==
            state.last_applied_ingestion_sequence)
        {
            cursor_seen = true;
        }
        if (record.ingestion_sequence >
                state.last_applied_ingestion_sequence &&
            record.ingestion_sequence <= evidence_cutoff)
        {
            ++pending_updates;
        }
    }

    const bool audit_cursor_consistent =
        (state.last_applied_ingestion_sequence == 0 &&
         state.audit_updates.empty()) ||
        (!state.audit_updates.empty() &&
         state.audit_updates.back().ingestion_sequence ==
             state.last_applied_ingestion_sequence);
    if (!cursor_seen || !audit_cursor_consistent)
    {
        return state.fail(
            EvidenceReputationApplyStatus::accepted_order_invalid,
            evidence_cutoff);
    }

    if (state.audit_updates.size() >
            state.limits.maximum_audit_updates ||
        pending_updates >
            state.limits.maximum_audit_updates -
                state.audit_updates.size())
    {
        return state.fail(
            EvidenceReputationApplyStatus::audit_capacity_exceeded,
            evidence_cutoff);
    }

    std::vector<PlannedUpdate> planned_updates;
    std::map<ReplicaID, int> planned_scores;
    try
    {
        planned_updates.reserve(pending_updates);
        for (const auto &record : accepted)
        {
            if (record.ingestion_sequence <=
                state.last_applied_ingestion_sequence)
            {
                continue;
            }
            if (record.ingestion_sequence > evidence_cutoff)
                break;

            ScoreMapping mapping{
                SimpleReputationOutcome::response, 0};
            if (!score_mapping(record.observation.outcome, mapping))
            {
                return state.fail(
                    EvidenceReputationApplyStatus::
                        accepted_order_invalid,
                    evidence_cutoff);
            }

            if (!state.reputation.contains(
                    record.observation.reporter_id))
            {
                return state.fail(
                    EvidenceReputationApplyStatus::reputation_rejected,
                    evidence_cutoff,
                    0,
                    SimpleReputationDisposition::unknown_reporter);
            }
            if (!state.reputation.contains(
                    record.observation.observed_replica_id))
            {
                return state.fail(
                    EvidenceReputationApplyStatus::reputation_rejected,
                    evidence_cutoff,
                    0,
                    SimpleReputationDisposition::unknown_target);
            }
            if (record.observation.reporter_id ==
                record.observation.observed_replica_id)
            {
                return state.fail(
                    EvidenceReputationApplyStatus::reputation_rejected,
                    evidence_cutoff,
                    0,
                    SimpleReputationDisposition::self_observation);
            }

            const auto inserted = planned_scores.emplace(
                record.observation.observed_replica_id,
                state.reputation.score(
                    record.observation.observed_replica_id));
            const auto prior_score = inserted.first->second;
            const auto predicted_score =
                static_cast<long long>(prior_score) + mapping.delta;
            if (predicted_score < std::numeric_limits<int>::min() ||
                predicted_score > std::numeric_limits<int>::max())
            {
                return state.fail(
                    EvidenceReputationApplyStatus::reputation_rejected,
                    evidence_cutoff,
                    0,
                    SimpleReputationDisposition::score_overflow);
            }
            inserted.first->second = static_cast<int>(predicted_score);
            planned_updates.push_back(
                PlannedUpdate{
                    &record,
                    mapping,
                    static_cast<int>(predicted_score)});
        }

        state.audit_updates.reserve(
            state.audit_updates.size() + pending_updates);
    }
    catch (...)
    {
        return state.fail(
            EvidenceReputationApplyStatus::internal_failure,
            evidence_cutoff);
    }

    std::size_t applied_updates = 0;
    for (const auto &planned : planned_updates)
    {
        const auto &record = *planned.record;
        bool provisional_audit = false;
        try
        {
            state.audit_updates.push_back(
                EvidenceReputationAuditUpdate{
                    record.ingestion_sequence,
                    record.observation.observation_id,
                    record.observation.reporter_id,
                    record.observation.observed_replica_id,
                    record.observation.outcome,
                    planned.mapping.outcome,
                    planned.mapping.delta,
                    planned.score});
            provisional_audit = true;
        }
        catch (...)
        {
            return state.fail(
                EvidenceReputationApplyStatus::internal_failure,
                evidence_cutoff,
                applied_updates);
        }

        SimpleReputationUpdate score_update;
        try
        {
            score_update =
                planned.mapping.outcome ==
                        SimpleReputationOutcome::response
                    ? state.reputation.observe_response(
                          record.observation.reporter_id,
                          record.observation.observed_replica_id)
                    : state.reputation.observe_timeout(
                          record.observation.reporter_id,
                          record.observation.observed_replica_id);
        }
        catch (...)
        {
            if (provisional_audit)
                state.audit_updates.pop_back();
            return state.fail(
                EvidenceReputationApplyStatus::internal_failure,
                evidence_cutoff,
                applied_updates);
        }

        if (score_update.disposition !=
            SimpleReputationDisposition::applied)
        {
            if (provisional_audit)
                state.audit_updates.pop_back();
            return state.fail(
                EvidenceReputationApplyStatus::reputation_rejected,
                evidence_cutoff,
                applied_updates,
                score_update.disposition);
        }
        if (!provisional_audit ||
            score_update.delta != planned.mapping.delta ||
            score_update.score != planned.score)
        {
            if (provisional_audit)
                state.audit_updates.pop_back();
            return state.fail(
                EvidenceReputationApplyStatus::internal_failure,
                evidence_cutoff,
                applied_updates);
        }

        state.last_applied_ingestion_sequence =
            record.ingestion_sequence;
        ++applied_updates;
    }

    state.last_cutoff = evidence_cutoff;
    return state.result(
        applied_updates == 0
            ? EvidenceReputationApplyStatus::no_updates
            : EvidenceReputationApplyStatus::applied,
        evidence_cutoff,
        applied_updates);
}

const std::vector<EvidenceReputationAuditUpdate> &
EvidenceReputationProjection::audit_updates() const noexcept
{
    return state_->audit_updates;
}

std::uint64_t EvidenceReputationProjection::last_cutoff() const noexcept
{
    return state_->last_cutoff;
}

std::uint64_t EvidenceReputationProjection::
last_applied_ingestion_sequence() const noexcept
{
    return state_->last_applied_ingestion_sequence;
}

bool EvidenceReputationProjection::healthy() const noexcept
{
    return state_->healthy && state_->ledger.healthy();
}

} // namespace hotstuff
