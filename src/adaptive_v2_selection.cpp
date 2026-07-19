#include "hotstuff/adaptive_v2_selection.h"

#include <algorithm>
#include <limits>
#include <map>
#include <set>
#include <stdexcept>
#include <utility>

namespace hotstuff
{
namespace
{

struct ValidatedSelectionInputs
{
    std::vector<ReplicaID> membership;
    ByzantineQuorum quorum;
};

ValidatedSelectionInputs validate_inputs(
    std::vector<ReplicaID> membership,
    const AdaptationEpochId &current_epoch,
    const AdaptiveV2SelectionConfig &config,
    const EvidenceReputationLimits &reputation_limits)
{
    if (config.schema_version != kAdaptiveV2SelectionSchemaVersion)
    {
        throw std::invalid_argument(
            "unsupported adaptive-v2 selection schema");
    }

    std::sort(membership.begin(), membership.end());
    if (membership.empty() ||
        membership.size() > kMaximumAdaptationMembers ||
        std::adjacent_find(membership.begin(), membership.end()) !=
            membership.end())
    {
        throw std::invalid_argument(
            "adaptive-v2 selection membership is invalid");
    }

    const auto quorum = derive_byzantine_quorum(membership.size());
    if (!quorum.has_value() || quorum->fault_threshold == 0)
    {
        throw std::invalid_argument(
            "adaptive-v2 selection requires exact N=3f+1 with f>0");
    }
    if (config.required_nonresponsive != quorum->fault_threshold)
    {
        throw std::invalid_argument(
            "adaptive-v2 selection must select exactly derived f");
    }
    if (config.minimum_score_drop == 0 ||
        config.minimum_timeouts_per_reporter == 0 ||
        config.maximum_post_baseline_timeout_attempts == 0 ||
        config.maximum_post_baseline_timeout_attempts >
            kMaximumAdaptationEvidenceRecords ||
        config.minimum_timeouts_per_reporter >
            config.maximum_post_baseline_timeout_attempts)
    {
        throw std::invalid_argument(
            "adaptive-v2 selection guard bounds are invalid");
    }
    if (reputation_limits.maximum_audit_updates == 0 ||
        reputation_limits.maximum_audit_updates >
            kMaximumAdaptationEvidenceRecords)
    {
        throw std::invalid_argument(
            "adaptive-v2 reputation bounds are invalid");
    }

    // Reuse the authoritative snapshot builder's membership, epoch, and
    // policy validation instead of maintaining a divergent validation copy.
    (void)build_adaptation_snapshot(
        membership,
        current_epoch,
        AcceptedEvidenceView{},
        0,
        config.responsiveness_policy,
        config.snapshot_seed);

    return {std::move(membership), *quorum};
}

bool is_member(
    const std::vector<ReplicaID> &membership,
    ReplicaID replica_id) noexcept
{
    return std::binary_search(
        membership.begin(), membership.end(), replica_id);
}

enum class PrefixStatus : std::uint8_t
{
    valid = 1,
    invalid_order,
    mixed_epoch,
    nonmember,
};

PrefixStatus validate_prefix(
    const std::vector<AcceptedEvidenceRecord> &accepted,
    const std::vector<ReplicaID> &membership,
    const AdaptationEpochId &epoch,
    std::uint64_t cutoff) noexcept
{
    std::uint64_t previous_sequence = 0;
    for (const auto &record : accepted)
    {
        if (record.ingestion_sequence == 0 ||
            record.ingestion_sequence <= previous_sequence)
        {
            return PrefixStatus::invalid_order;
        }
        previous_sequence = record.ingestion_sequence;
        if (record.ingestion_sequence > cutoff)
            continue;

        const auto &observation = record.observation;
        if (observation.configuration.epoch_number !=
                epoch.epoch_number ||
            observation.configuration.epoch_digest !=
                epoch.epoch_digest)
        {
            return PrefixStatus::mixed_epoch;
        }
        if (!is_member(membership, observation.reporter_id) ||
            !is_member(
                membership, observation.observed_replica_id))
        {
            return PrefixStatus::nonmember;
        }
        for (const auto signer : observation.signer_set)
        {
            if (!is_member(membership, signer))
                return PrefixStatus::nonmember;
        }
    }
    return PrefixStatus::valid;
}

AdaptiveV2SelectionStatus selection_status(
    PrefixStatus status) noexcept
{
    switch (status)
    {
    case PrefixStatus::valid:
        break;
    case PrefixStatus::mixed_epoch:
        return AdaptiveV2SelectionStatus::mixed_epoch;
    case PrefixStatus::nonmember:
        return AdaptiveV2SelectionStatus::nonmember_evidence;
    case PrefixStatus::invalid_order:
        return AdaptiveV2SelectionStatus::snapshot_failed;
    }
    return AdaptiveV2SelectionStatus::internal_failure;
}

struct OutstandingTimeout
{
    ReplicaID reporter_id{0};
    ReplicaID target_id{0};
};

using ReporterTimeoutCounts = std::map<ReplicaID, std::uint64_t>;
using TargetTimeoutCounts =
    std::map<ReplicaID, ReporterTimeoutCounts>;

enum class TimeoutReplayStatus : std::uint8_t
{
    replayed = 1,
    capacity_exceeded,
    invalid_transition,
};

TimeoutReplayStatus replay_post_baseline_timeouts(
    const std::vector<AcceptedEvidenceRecord> &accepted,
    std::uint64_t baseline_cutoff,
    std::uint64_t evidence_cutoff,
    std::size_t capacity,
    TargetTimeoutCounts &counts) noexcept
{
    try
    {
        std::map<uint256_t, OutstandingTimeout> outstanding;
        std::size_t total_timeout_attempts = 0;
        for (const auto &record : accepted)
        {
            if (record.ingestion_sequence <= baseline_cutoff ||
                record.ingestion_sequence > evidence_cutoff)
            {
                continue;
            }

            const auto &observation = record.observation;
            if (observation.outcome == ResponseOutcome::on_time)
                continue;
            if (observation.outcome == ResponseOutcome::late)
            {
                const auto found = outstanding.find(
                    observation.observation_id);
                if (found != outstanding.end())
                {
                    if (found->second.reporter_id !=
                            observation.reporter_id ||
                        found->second.target_id !=
                            observation.observed_replica_id)
                    {
                        return TimeoutReplayStatus::invalid_transition;
                    }
                    outstanding.erase(found);
                }
                // A timeout before the baseline may legally become late
                // afterwards. It never becomes a post-baseline attempt.
                continue;
            }
            if (observation.outcome != ResponseOutcome::timeout)
                return TimeoutReplayStatus::invalid_transition;
            if (total_timeout_attempts >= capacity)
                return TimeoutReplayStatus::capacity_exceeded;
            ++total_timeout_attempts;
            if (outstanding.find(observation.observation_id) !=
                outstanding.end())
            {
                return TimeoutReplayStatus::invalid_transition;
            }
            if (outstanding.size() >= capacity)
                return TimeoutReplayStatus::capacity_exceeded;
            outstanding.emplace(
                observation.observation_id,
                OutstandingTimeout{
                    observation.reporter_id,
                    observation.observed_replica_id});
        }

        for (const auto &entry : outstanding)
        {
            auto &count = counts[entry.second.target_id]
                                 [entry.second.reporter_id];
            if (count == std::numeric_limits<std::uint64_t>::max())
                return TimeoutReplayStatus::capacity_exceeded;
            ++count;
        }
    }
    catch (...)
    {
        return TimeoutReplayStatus::capacity_exceeded;
    }
    return TimeoutReplayStatus::replayed;
}

enum class DrawdownReplayStatus : std::uint8_t
{
    replayed = 1,
    invalid_cursor,
    invalid_update,
    capacity_exceeded,
};

DrawdownReplayStatus replay_drawdown_suffix(
    const std::vector<EvidenceReputationAuditUpdate> &updates,
    std::size_t cursor,
    const std::vector<ReplicaID> &membership,
    std::size_t maximum_timeout_attempts,
    std::vector<std::int64_t> &drawdowns,
    std::map<uint256_t, OutstandingTimeout> &outstanding_timeouts,
    std::size_t &next_cursor) noexcept
{
    if (cursor > updates.size() ||
        drawdowns.size() != membership.size() ||
        outstanding_timeouts.size() > maximum_timeout_attempts ||
        maximum_timeout_attempts == 0 ||
        maximum_timeout_attempts >
            static_cast<std::size_t>(
                std::numeric_limits<std::int64_t>::max()))
    {
        return DrawdownReplayStatus::invalid_cursor;
    }

    std::uint64_t previous_sequence =
        cursor == 0 ? 0 : updates[cursor - 1].ingestion_sequence;
    for (std::size_t index = cursor; index < updates.size(); ++index)
    {
        const auto &update = updates[index];
        if (update.ingestion_sequence == 0 ||
            update.ingestion_sequence <= previous_sequence)
        {
            return DrawdownReplayStatus::invalid_update;
        }
        previous_sequence = update.ingestion_sequence;

        const auto member = std::lower_bound(
            membership.begin(), membership.end(), update.target_id);
        if (member == membership.end() || *member != update.target_id)
            return DrawdownReplayStatus::invalid_update;
        const auto member_index = static_cast<std::size_t>(
            std::distance(membership.begin(), member));
        auto &drawdown = drawdowns[member_index];

        if (update.evidence_outcome == ResponseOutcome::timeout)
        {
            if (update.delta != -1 || drawdown > 0)
                return DrawdownReplayStatus::invalid_update;
            if (drawdown <= -static_cast<std::int64_t>(
                                maximum_timeout_attempts))
            {
                return DrawdownReplayStatus::capacity_exceeded;
            }
            if (outstanding_timeouts.size() >=
                maximum_timeout_attempts)
            {
                return DrawdownReplayStatus::capacity_exceeded;
            }
            bool inserted = false;
            try
            {
                inserted = outstanding_timeouts.emplace(
                    update.observation_id,
                    OutstandingTimeout{
                        update.reporter_id, update.target_id})
                               .second;
            }
            catch (...)
            {
                return DrawdownReplayStatus::capacity_exceeded;
            }
            if (!inserted)
                return DrawdownReplayStatus::invalid_update;
            --drawdown;
            continue;
        }
        if (update.evidence_outcome != ResponseOutcome::on_time &&
            update.evidence_outcome != ResponseOutcome::late)
        {
            return DrawdownReplayStatus::invalid_update;
        }
        if (update.delta != 1 || drawdown > 0)
            return DrawdownReplayStatus::invalid_update;
        if (update.evidence_outcome == ResponseOutcome::on_time)
        {
            if (drawdown < 0)
                ++drawdown;
            continue;
        }

        const auto timed_out = outstanding_timeouts.find(
            update.observation_id);
        if (timed_out == outstanding_timeouts.end())
        {
            // The accepted ledger guarantees a prior correlated timeout.
            // If it is outside this bounded set, it preceded the baseline.
            continue;
        }
        if (timed_out->second.reporter_id != update.reporter_id ||
            timed_out->second.target_id != update.target_id)
        {
            return DrawdownReplayStatus::invalid_update;
        }
        outstanding_timeouts.erase(timed_out);
        if (drawdown < 0)
            ++drawdown;
    }

    next_cursor = updates.size();
    return DrawdownReplayStatus::replayed;
}

bool projection_applied(
    EvidenceReputationApplyStatus status) noexcept
{
    return status == EvidenceReputationApplyStatus::applied ||
           status == EvidenceReputationApplyStatus::no_updates;
}

bool candidate_ranks_before(
    const AdaptiveV2CandidateAudit &left,
    const AdaptiveV2CandidateAudit &right) noexcept
{
    if (left.qualifying_reporters.size() !=
        right.qualifying_reporters.size())
    {
        return left.qualifying_reporters.size() >
               right.qualifying_reporters.size();
    }
    if (left.total_uncompensated_timeouts !=
        right.total_uncompensated_timeouts)
    {
        return left.total_uncompensated_timeouts >
               right.total_uncompensated_timeouts;
    }
    if (left.guard_drawdown != right.guard_drawdown)
    {
        return left.guard_drawdown < right.guard_drawdown;
    }
    return left.replica_id < right.replica_id;
}

} // namespace

struct AdaptiveV2ByzantineSelection::State
{
    State(const EvidenceLedger &ledger_,
          ValidatedSelectionInputs inputs,
          AdaptationEpochId current_epoch_,
          AdaptiveV2SelectionConfig config_,
          EvidenceReputationLimits reputation_limits)
        : ledger(ledger_),
          membership(std::move(inputs.membership)),
          quorum(inputs.quorum),
          current_epoch(std::move(current_epoch_)),
          config(std::move(config_)),
          reputation(membership),
          projection(ledger, reputation, reputation_limits),
          guard_drawdowns(membership.size(), 0)
    {
        baseline_scores.reserve(membership.size());
        for (const auto replica_id : membership)
            baseline_scores.push_back({replica_id, 0});
        healthy = ledger.healthy() && projection.healthy();
    }

    AdaptiveV2SelectionMetadata metadata(
        std::uint64_t evidence_cutoff) const noexcept
    {
        return {
            quorum.replica_count,
            quorum.fault_threshold,
            quorum.quorum,
            config.required_nonresponsive,
            static_cast<std::uint32_t>(
                quorum.fault_threshold + 1U),
            config.minimum_timeouts_per_reporter,
            config.minimum_score_drop,
            baseline_cutoff,
            evidence_cutoff};
    }

    AdaptiveV2SelectionResult result(
        AdaptiveV2SelectionStatus status,
        std::uint64_t evidence_cutoff) const
    {
        AdaptiveV2SelectionResult output;
        output.status = status;
        output.metadata = metadata(evidence_cutoff);
        return output;
    }

    const EvidenceLedger &ledger;
    std::vector<ReplicaID> membership;
    ByzantineQuorum quorum;
    AdaptationEpochId current_epoch;
    AdaptiveV2SelectionConfig config;
    SimpleReputation reputation;
    EvidenceReputationProjection projection;
    std::vector<AdaptiveV2ReplicaScore> baseline_scores;
    std::vector<std::int64_t> guard_drawdowns;
    std::map<uint256_t, OutstandingTimeout>
        guard_outstanding_timeouts;
    std::size_t guard_audit_cursor{0};
    std::uint64_t baseline_cutoff{0};
    std::uint64_t current_cutoff{0};
    bool baseline_frozen{false};
    bool healthy{true};
};

AdaptiveV2ByzantineSelection::AdaptiveV2ByzantineSelection(
    const EvidenceLedger &ledger,
    std::vector<ReplicaID> membership,
    AdaptationEpochId current_epoch,
    AdaptiveV2SelectionConfig config,
    EvidenceReputationLimits reputation_limits)
    : state_([&]() {
          auto inputs = validate_inputs(
              membership,
              current_epoch,
              config,
              reputation_limits);
          return std::make_unique<State>(
              ledger,
              std::move(inputs),
              std::move(current_epoch),
              std::move(config),
              reputation_limits);
      }())
{}

AdaptiveV2ByzantineSelection::~AdaptiveV2ByzantineSelection() = default;

AdaptiveV2SelectionStatus
AdaptiveV2ByzantineSelection::freeze_baseline(
    std::uint64_t evidence_cutoff) noexcept
{
    auto &state = *state_;
    if (!state.healthy)
        return AdaptiveV2SelectionStatus::invalid_state;
    if (state.baseline_frozen)
        return AdaptiveV2SelectionStatus::invalid_state;
    if (!state.ledger.healthy())
    {
        state.healthy = false;
        return AdaptiveV2SelectionStatus::ledger_unhealthy;
    }
    if (evidence_cutoff > state.ledger.high_watermark())
        return AdaptiveV2SelectionStatus::invalid_cutoff;

    const auto prefix = validate_prefix(
        state.ledger.accepted(),
        state.membership,
        state.current_epoch,
        evidence_cutoff);
    if (prefix != PrefixStatus::valid)
        return selection_status(prefix);

    const auto applied = state.projection.apply_through(evidence_cutoff);
    if (!projection_applied(applied.status))
    {
        state.healthy = false;
        return applied.status ==
                       EvidenceReputationApplyStatus::ledger_unhealthy
                   ? AdaptiveV2SelectionStatus::ledger_unhealthy
                   : AdaptiveV2SelectionStatus::projection_failed;
    }

    try
    {
        for (auto &entry : state.baseline_scores)
            entry.score = state.reputation.score(entry.replica_id);
    }
    catch (...)
    {
        state.healthy = false;
        return AdaptiveV2SelectionStatus::internal_failure;
    }

    state.baseline_cutoff = evidence_cutoff;
    state.current_cutoff = evidence_cutoff;
    state.guard_audit_cursor =
        state.projection.audit_updates().size();
    state.baseline_frozen = true;
    return AdaptiveV2SelectionStatus::baseline_frozen;
}

AdaptiveV2SelectionResult
AdaptiveV2ByzantineSelection::select_through(
    std::uint64_t evidence_cutoff) noexcept
{
    auto &state = *state_;
    if (!state.healthy || !state.baseline_frozen)
        return state.result(
            AdaptiveV2SelectionStatus::invalid_state,
            evidence_cutoff);
    if (!state.ledger.healthy())
    {
        state.healthy = false;
        return state.result(
            AdaptiveV2SelectionStatus::ledger_unhealthy,
            evidence_cutoff);
    }
    if (evidence_cutoff <= state.current_cutoff ||
        evidence_cutoff > state.ledger.high_watermark())
    {
        return state.result(
            AdaptiveV2SelectionStatus::invalid_cutoff,
            evidence_cutoff);
    }

    const auto &accepted = state.ledger.accepted();
    const auto prefix = validate_prefix(
        accepted,
        state.membership,
        state.current_epoch,
        evidence_cutoff);
    if (prefix != PrefixStatus::valid)
        return state.result(selection_status(prefix), evidence_cutoff);

    TargetTimeoutCounts timeout_counts;
    const auto replay = replay_post_baseline_timeouts(
        accepted,
        state.baseline_cutoff,
        evidence_cutoff,
        state.config.maximum_post_baseline_timeout_attempts,
        timeout_counts);
    if (replay != TimeoutReplayStatus::replayed)
    {
        state.healthy = false;
        return state.result(
            replay == TimeoutReplayStatus::capacity_exceeded
                ? AdaptiveV2SelectionStatus::capacity_exceeded
                : AdaptiveV2SelectionStatus::snapshot_failed,
            evidence_cutoff);
    }

    std::unique_ptr<AdaptationSnapshot> snapshot;
    try
    {
        std::size_t accepted_prefix_size = 0;
        while (accepted_prefix_size < accepted.size() &&
               accepted[accepted_prefix_size].ingestion_sequence <=
                   evidence_cutoff)
        {
            ++accepted_prefix_size;
        }
        snapshot = std::make_unique<AdaptationSnapshot>(
            build_adaptation_snapshot(
                state.membership,
                state.current_epoch,
                AcceptedEvidenceView{
                    accepted.empty() ? nullptr : accepted.data(),
                    accepted_prefix_size},
                evidence_cutoff,
                state.config.responsiveness_policy,
                state.config.snapshot_seed));
    }
    catch (...)
    {
        state.healthy = false;
        return state.result(
            AdaptiveV2SelectionStatus::snapshot_failed,
            evidence_cutoff);
    }

    const auto applied = state.projection.apply_through(evidence_cutoff);
    if (!projection_applied(applied.status))
    {
        state.healthy = false;
        return state.result(
            applied.status ==
                       EvidenceReputationApplyStatus::ledger_unhealthy
                   ? AdaptiveV2SelectionStatus::ledger_unhealthy
                   : AdaptiveV2SelectionStatus::projection_failed,
            evidence_cutoff);
    }
    std::vector<std::int64_t> planned_drawdowns;
    std::map<uint256_t, OutstandingTimeout>
        planned_outstanding_timeouts;
    try
    {
        planned_drawdowns = state.guard_drawdowns;
        planned_outstanding_timeouts =
            state.guard_outstanding_timeouts;
    }
    catch (...)
    {
        state.healthy = false;
        return state.result(
            AdaptiveV2SelectionStatus::internal_failure,
            evidence_cutoff);
    }
    std::size_t planned_audit_cursor = state.guard_audit_cursor;
    const auto drawdown_replay = replay_drawdown_suffix(
        state.projection.audit_updates(),
        state.guard_audit_cursor,
        state.membership,
        state.config.maximum_post_baseline_timeout_attempts,
        planned_drawdowns,
        planned_outstanding_timeouts,
        planned_audit_cursor);
    if (drawdown_replay != DrawdownReplayStatus::replayed)
    {
        state.healthy = false;
        return state.result(
            drawdown_replay ==
                    DrawdownReplayStatus::capacity_exceeded
                ? AdaptiveV2SelectionStatus::capacity_exceeded
                : AdaptiveV2SelectionStatus::projection_failed,
            evidence_cutoff);
    }

    state.guard_drawdowns.swap(planned_drawdowns);
    state.guard_outstanding_timeouts.swap(
        planned_outstanding_timeouts);
    state.guard_audit_cursor = planned_audit_cursor;
    state.current_cutoff = evidence_cutoff;

    auto output = state.result(
        AdaptiveV2SelectionStatus::insufficient_guarded_candidates,
        evidence_cutoff);
    output.snapshot = std::move(snapshot);
    try
    {
        std::map<ReplicaID, const ReplicaAdaptationResult *>
            snapshot_by_replica;
        for (const auto &entry : output.snapshot->ranking())
            snapshot_by_replica.emplace(entry.replica_id, &entry);

        output.eligible_candidates.reserve(state.membership.size());
        for (std::size_t index = 0;
             index < state.membership.size();
             ++index)
        {
            const auto replica_id = state.membership[index];
            const auto snapshot_found =
                snapshot_by_replica.find(replica_id);
            if (snapshot_found == snapshot_by_replica.end())
            {
                state.healthy = false;
                output.status =
                    AdaptiveV2SelectionStatus::snapshot_failed;
                output.eligible_candidates.clear();
                return output;
            }

            AdaptiveV2CandidateAudit audit;
            audit.replica_id = replica_id;
            audit.snapshot_classification =
                snapshot_found->second->classification;
            audit.baseline_score =
                state.baseline_scores[index].score;
            audit.current_score =
                state.reputation.score(replica_id);
            audit.baseline_score_delta =
                static_cast<std::int64_t>(audit.current_score) -
                static_cast<std::int64_t>(audit.baseline_score);
            audit.guard_drawdown = state.guard_drawdowns[index];

            const auto target_found = timeout_counts.find(replica_id);
            if (target_found != timeout_counts.end())
            {
                for (const auto &reporter : target_found->second)
                {
                    if (std::numeric_limits<std::uint64_t>::max() -
                            audit.total_uncompensated_timeouts <
                        reporter.second)
                    {
                        state.healthy = false;
                        output.status =
                            AdaptiveV2SelectionStatus::capacity_exceeded;
                        output.eligible_candidates.clear();
                        return output;
                    }
                    audit.total_uncompensated_timeouts +=
                        reporter.second;
                    if (reporter.second >=
                        state.config.minimum_timeouts_per_reporter)
                    {
                        audit.qualifying_reporters.push_back(
                            reporter.first);
                    }
                }
            }

            audit.snapshot_nonresponsive =
                audit.snapshot_classification ==
                ResponsivenessClass::nonresponsive;
            audit.score_drop_satisfied =
                audit.guard_drawdown <=
                -static_cast<std::int64_t>(
                    state.config.minimum_score_drop);
            audit.reporter_guard_satisfied =
                audit.qualifying_reporters.size() >=
                static_cast<std::size_t>(
                    state.quorum.fault_threshold + 1U);
            audit.guarded_eligible =
                audit.snapshot_nonresponsive &&
                audit.score_drop_satisfied &&
                audit.reporter_guard_satisfied;
            if (audit.guarded_eligible)
            {
                output.eligible_candidates.push_back(
                    std::move(audit));
            }
        }

        std::sort(
            output.eligible_candidates.begin(),
            output.eligible_candidates.end(),
            candidate_ranks_before);
        if (output.eligible_candidates.size() <
            state.config.required_nonresponsive)
        {
            return output;
        }

        output.selected_replicas.reserve(
            state.config.required_nonresponsive);
        for (std::size_t index = 0;
             index < state.config.required_nonresponsive;
             ++index)
        {
            output.selected_replicas.push_back(
                output.eligible_candidates[index].replica_id);
        }

        const std::set<ReplicaID> selected(
            output.selected_replicas.begin(),
            output.selected_replicas.end());
        output.eligible_roots.reserve(state.quorum.quorum);
        for (const auto &entry : output.snapshot->ranking())
        {
            if (entry.classification ==
                    ResponsivenessClass::responsive &&
                entry.eligible &&
                selected.find(entry.replica_id) == selected.end())
            {
                output.eligible_roots.push_back(entry.replica_id);
            }
        }
        if (output.eligible_roots.size() < state.quorum.quorum)
        {
            output.status =
                AdaptiveV2SelectionStatus::insufficient_eligible_roots;
            output.selected_replicas.clear();
            output.eligible_roots.clear();
            return output;
        }
    }
    catch (...)
    {
        state.healthy = false;
        output.status = AdaptiveV2SelectionStatus::internal_failure;
        output.eligible_candidates.clear();
        output.selected_replicas.clear();
        output.eligible_roots.clear();
        return output;
    }

    output.status = AdaptiveV2SelectionStatus::selected;
    return output;
}

const std::vector<AdaptiveV2ReplicaScore> &
AdaptiveV2ByzantineSelection::baseline_scores() const noexcept
{
    return state_->baseline_scores;
}

const std::vector<EvidenceReputationAuditUpdate> &
AdaptiveV2ByzantineSelection::score_trajectory() const noexcept
{
    return state_->projection.audit_updates();
}

const std::vector<ReplicaID> &
AdaptiveV2ByzantineSelection::membership() const noexcept
{
    return state_->membership;
}

const ByzantineQuorum &
AdaptiveV2ByzantineSelection::quorum_metadata() const noexcept
{
    return state_->quorum;
}

const AdaptationEpochId &
AdaptiveV2ByzantineSelection::current_epoch() const noexcept
{
    return state_->current_epoch;
}

std::uint64_t AdaptiveV2ByzantineSelection::baseline_cutoff() const noexcept
{
    return state_->baseline_cutoff;
}

std::uint64_t AdaptiveV2ByzantineSelection::current_cutoff() const noexcept
{
    return state_->current_cutoff;
}

bool AdaptiveV2ByzantineSelection::baseline_frozen() const noexcept
{
    return state_->baseline_frozen;
}

bool AdaptiveV2ByzantineSelection::healthy() const noexcept
{
    return state_->healthy && state_->ledger.healthy() &&
           state_->projection.healthy();
}

} // namespace hotstuff
