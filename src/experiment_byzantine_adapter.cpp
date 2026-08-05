#include "hotstuff/experiment_byzantine_adapter.h"

#include <algorithm>
#include <cstdint>
#include <limits>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>
#include <utility>

namespace hotstuff
{
namespace
{

struct ContextLess
{
    bool operator()(
        const ExperimentByzantineContext &left,
        const ExperimentByzantineContext &right) const noexcept
    {
        if (left.proposal < right.proposal)
            return true;
        if (right.proposal < left.proposal)
            return false;
        return left.diagnostic_window < right.diagnostic_window;
    }
};

bool exact_context(
    const ExperimentByzantineOptions &options,
    const ExperimentByzantineContext &context) noexcept
{
    return options.enabled &&
           context.proposal.configuration == options.configuration &&
           context.diagnostic_window == options.diagnostic_window;
}

bool exact_omission_context(
    const ExperimentByzantineOptions &options,
    const ExperimentByzantineContext &context) noexcept
{
    return options.enabled &&
           context.diagnostic_window == options.diagnostic_window &&
           (context.proposal.configuration == options.configuration ||
            (options.additional_omission_configuration.has_value() &&
             context.proposal.configuration ==
                 *options.additional_omission_configuration));
}

constexpr const char *kRotatingOmissionMode =
    "rotating_intermittent_omission_v1";
constexpr const char *kPersistentOmissionMode =
    "persistent_selected_omission_v1";
constexpr const char *kTieredOmissionMode =
    "tiered_persistent_responsive_omission_v1";

bool is_rotating_omission_mode(const std::string &mode) noexcept
{
    return mode == kRotatingOmissionMode;
}

bool is_persistent_omission_mode(const std::string &mode) noexcept
{
    return mode == kPersistentOmissionMode;
}

bool is_tiered_omission_mode(const std::string &mode) noexcept
{
    return mode == kTieredOmissionMode;
}

bool is_scheduled_omission_mode(const std::string &mode) noexcept
{
    return is_rotating_omission_mode(mode) ||
           is_persistent_omission_mode(mode) ||
           is_tiered_omission_mode(mode);
}

const char *omission_cohort_name(ExperimentOmissionCohort cohort) noexcept
{
    switch (cohort)
    {
    case ExperimentOmissionCohort::none:
        return "none";
    case ExperimentOmissionCohort::hard:
        return "hard";
    case ExperimentOmissionCohort::responsive_degraded:
        return "responsive_degraded";
    }
    return "unknown";
}

const char *omission_action_name(ExperimentOmissionAction action) noexcept
{
    switch (action)
    {
    case ExperimentOmissionAction::forward:
        return "forward";
    case ExperimentOmissionAction::omit_aggregate:
        return "omit_aggregate";
    case ExperimentOmissionAction::omit_direct_vote:
        return "omit_direct_vote";
    case ExperimentOmissionAction::capacity_exhausted:
        return "capacity_exhausted";
    }
    return "unknown";
}

void fnv1a_byte(std::uint64_t &hash, std::uint8_t byte) noexcept
{
    hash ^= byte;
    hash *= UINT64_C(1099511628211);
}

void fnv1a_u32(std::uint64_t &hash, std::uint32_t value) noexcept
{
    for (int shift = 24; shift >= 0; shift -= 8)
        fnv1a_byte(hash, static_cast<std::uint8_t>(value >> shift));
}

void fnv1a_uint256(std::uint64_t &hash, const uint256_t &value)
{
    const bytearray_t bytes = value;
    for (const auto byte : bytes)
        fnv1a_byte(hash, byte);
}

std::size_t proposal_actor_index(
    const ProposalKey &proposal,
    std::size_t actor_count)
{
    std::uint64_t hash = UINT64_C(14695981039346656037);
    fnv1a_u32(hash, proposal.configuration.epoch_number);
    fnv1a_u32(hash, proposal.configuration.tree_id);
    fnv1a_uint256(hash, proposal.configuration.epoch_digest);
    fnv1a_uint256(hash, proposal.block_hash);
    return static_cast<std::size_t>(hash % actor_count);
}

} // namespace

std::string format_experiment_omission_marker(
    const ExperimentOmissionMarker &marker)
{
    std::ostringstream encoded;
    encoded << "KAURI_FAULT"
            << " fault=" << marker.fault_mode
            << " proposal_epoch="
            << marker.proposal.configuration.epoch_number
            << " proposal_tree="
            << marker.proposal.configuration.tree_id
            << " proposal_epoch_digest="
            << get_hex(marker.proposal.configuration.epoch_digest)
            << " proposal_block_hash=" << get_hex(marker.proposal.block_hash)
            << " window=" << marker.diagnostic_window
            << " window_start_monotonic_ns="
            << marker.window_start_monotonic_ns
            << " window_end_monotonic_ns="
            << marker.window_end_monotonic_ns
            << " actor=" << marker.actor
            << " action=" << omission_action_name(marker.action)
            << " monotonic_ns=" << marker.monotonic_ns;
    if (marker.fault_mode == kTieredOmissionMode)
    {
        encoded << " cohort=" << omission_cohort_name(marker.cohort)
                << " hard_actor_count=" << marker.hard_actor_count
                << " responsive_degraded_actor_count="
                << marker.responsive_degraded_actor_count
                << " fault_threshold=" << marker.fault_threshold
                << " max_omissions_per_proposal="
                << marker.max_omissions_per_proposal
                << " responsive_omission_period="
                << marker.responsive_omission_period
                << " contribution_ordinal="
                << marker.contribution_ordinal;
    }
    return encoded.str();
}

struct ExperimentByzantineAdapter::State
{
    struct FalseReportState
    {
        bool verified_response_observed{false};
        bool positive_marker_consumed{false};
        bool timeout_consumed{false};
    };

    struct ScheduledDecisionState
    {
        ExperimentOmissionAction action{ExperimentOmissionAction::forward};
        bool marker_emitted{false};
        bool direct_vote_omitted{false};
        ExperimentOmissionCohort cohort{ExperimentOmissionCohort::none};
        bool auditable{false};
        std::uint64_t contribution_ordinal{0};
    };

    explicit State(ExperimentByzantineOptions configured)
        : options(std::move(configured))
    {
        if (options.additional_omission_configuration.has_value() &&
            options.omit_outbound_direct_vote)
            throw std::invalid_argument(
                "direct-vote omission does not accept an additional "
                "configuration");
        if (options.additional_omission_configuration.has_value() &&
            (!options.enabled || !options.omit_outbound_aggregate))
            throw std::invalid_argument(
                "additional omission configuration requires enabled "
                "aggregate omission");
        if (!options.enabled)
            return;
        if (options.diagnostic_window.empty())
            throw std::invalid_argument(
                "diagnostic window must be non-empty");
        const auto fault_mode_count =
            static_cast<unsigned>(
                options.false_report_target.has_value()) +
            static_cast<unsigned>(options.omit_outbound_aggregate) +
            static_cast<unsigned>(options.omit_outbound_direct_vote) +
            static_cast<unsigned>(options.rotating_omission.has_value());
        if (fault_mode_count != 1)
            throw std::invalid_argument(
                "enabled Byzantine adapter requires exactly one fault mode");
        if (options.false_report_target.has_value() &&
            options.maximum_false_report_contexts == 0)
            throw std::invalid_argument(
                "false-report context bound must be positive");
        if (options.omit_outbound_aggregate &&
            options.maximum_omission_contexts == 0)
            throw std::invalid_argument(
                "omission context bound must be positive");
        if (options.omit_outbound_direct_vote &&
            options.maximum_direct_vote_omission_contexts == 0)
            throw std::invalid_argument(
                "direct-vote omission context bound must be positive");
        if (options.additional_omission_configuration.has_value())
        {
            const auto &additional =
                *options.additional_omission_configuration;
            if (additional.epoch_number !=
                    options.configuration.epoch_number ||
                additional.epoch_digest !=
                    options.configuration.epoch_digest)
                throw std::invalid_argument(
                    "additional omission configuration must share the "
                    "primary epoch and digest");
            if (additional.tree_id == options.configuration.tree_id)
                throw std::invalid_argument(
                    "additional omission configuration must use a "
                    "distinct tree");
        }
        if (options.rotating_omission.has_value())
        {
            auto &scheduled = *options.rotating_omission;
            if (!is_scheduled_omission_mode(scheduled.mode))
                throw std::invalid_argument(
                    "unsupported scheduled omission mode");
            if (options.additional_omission_configuration.has_value() ||
                options.maximum_false_report_contexts != 0 ||
                options.maximum_omission_contexts != 0 ||
                options.maximum_direct_vote_omission_contexts != 0)
                throw std::invalid_argument(
                    "scheduled omission cannot be combined with static "
                    "fault configuration");
            if (scheduled.replica_count == 0 ||
                scheduled.local_replica >= scheduled.replica_count)
                throw std::invalid_argument(
                    "scheduled omission requires an in-range local replica");
            const auto quorum =
                derive_byzantine_quorum(scheduled.replica_count);
            if (!quorum.has_value() || scheduled.expected_actor_count == 0 ||
                scheduled.expected_actor_count > quorum->fault_threshold ||
                scheduled.actor_ids.size() != scheduled.expected_actor_count)
                throw std::invalid_argument(
                    "scheduled omission actor count must be within the "
                    "derived fault threshold");
            std::sort(scheduled.actor_ids.begin(), scheduled.actor_ids.end());
            if (std::adjacent_find(
                    scheduled.actor_ids.begin(), scheduled.actor_ids.end()) !=
                scheduled.actor_ids.end())
                throw std::invalid_argument(
                    "scheduled omission actors must be unique");
            if (std::any_of(
                    scheduled.actor_ids.begin(),
                    scheduled.actor_ids.end(),
                    [&scheduled](ReplicaID actor)
                    { return actor >= scheduled.replica_count; }))
                throw std::invalid_argument(
                    "scheduled omission actor is outside membership");
            std::sort(
                scheduled.responsive_degraded_actor_ids.begin(),
                scheduled.responsive_degraded_actor_ids.end());
            if (is_tiered_omission_mode(scheduled.mode))
            {
                if (scheduled.responsive_degraded_actor_ids.empty() ||
                    scheduled.responsive_omission_period < 2)
                    throw std::invalid_argument(
                        "tiered omission requires responsive-degraded actors "
                        "and a period greater than one");
                if (std::adjacent_find(
                        scheduled.responsive_degraded_actor_ids.begin(),
                        scheduled.responsive_degraded_actor_ids.end()) !=
                    scheduled.responsive_degraded_actor_ids.end())
                    throw std::invalid_argument(
                        "responsive-degraded omission actors must be unique");
                if (std::any_of(
                        scheduled.responsive_degraded_actor_ids.begin(),
                        scheduled.responsive_degraded_actor_ids.end(),
                        [&scheduled](ReplicaID actor)
                        { return actor >= scheduled.replica_count; }))
                    throw std::invalid_argument(
                        "responsive-degraded omission actor is outside "
                        "membership");
                if (std::any_of(
                        scheduled.responsive_degraded_actor_ids.begin(),
                        scheduled.responsive_degraded_actor_ids.end(),
                        [&scheduled](ReplicaID actor)
                        {
                            return std::binary_search(
                                scheduled.actor_ids.begin(),
                                scheduled.actor_ids.end(),
                                actor);
                        }))
                    throw std::invalid_argument(
                        "tiered omission actor cohorts must be disjoint");
                const auto total_actor_count =
                    scheduled.actor_ids.size() +
                    scheduled.responsive_degraded_actor_ids.size();
                if (total_actor_count > quorum->fault_threshold)
                    throw std::invalid_argument(
                        "tiered omission cohort exceeds the derived fault "
                        "threshold");
            }
            else if (!scheduled.responsive_degraded_actor_ids.empty() ||
                     scheduled.responsive_omission_period != 0)
                throw std::invalid_argument(
                    "responsive-degraded omission configuration requires "
                    "the tiered mode");
            if (scheduled.window_start_monotonic_ns == 0 ||
                scheduled.window_end_monotonic_ns <=
                    scheduled.window_start_monotonic_ns)
                throw std::invalid_argument(
                    "scheduled omission window must be a non-empty future "
                    "monotonic interval");
            const auto expected_maximum_omissions =
                is_rotating_omission_mode(scheduled.mode)
                    ? std::size_t{1}
                    : is_tiered_omission_mode(scheduled.mode)
                          ? scheduled.actor_ids.size() +
                                scheduled.responsive_degraded_actor_ids.size()
                          : scheduled.expected_actor_count;
            if (scheduled.max_omissions_per_proposal !=
                expected_maximum_omissions)
                throw std::invalid_argument(
                    "scheduled omission maximum must match its actor "
                    "schedule");
            if (scheduled.maximum_contexts == 0)
                throw std::invalid_argument(
                    "scheduled omission context bound must be positive");
            scheduled_fault_threshold = quorum->fault_threshold;
        }
    }

    std::optional<ReplicaID> rotating_actor(
        const ProposalKey &proposal) const
    {
        if (!options.enabled || !options.rotating_omission.has_value())
            return std::nullopt;
        const auto &scheduled = *options.rotating_omission;
        if (!is_rotating_omission_mode(scheduled.mode))
            return std::nullopt;
        const auto &actors = scheduled.actor_ids;
        return actors[proposal_actor_index(proposal, actors.size())];
    }

    ExperimentOmissionCohort local_actor_cohort() const
    {
        const auto &scheduled = *options.rotating_omission;
        if ((is_persistent_omission_mode(scheduled.mode) ||
             is_tiered_omission_mode(scheduled.mode)) &&
            std::binary_search(
                scheduled.actor_ids.begin(),
                scheduled.actor_ids.end(),
                scheduled.local_replica))
            return ExperimentOmissionCohort::hard;
        if (is_tiered_omission_mode(scheduled.mode) &&
            std::binary_search(
                scheduled.responsive_degraded_actor_ids.begin(),
                scheduled.responsive_degraded_actor_ids.end(),
                scheduled.local_replica))
            return ExperimentOmissionCohort::responsive_degraded;
        return ExperimentOmissionCohort::none;
    }

    bool local_actor_selected(const ProposalKey &proposal) const
    {
        const auto &scheduled = *options.rotating_omission;
        if (is_persistent_omission_mode(scheduled.mode) ||
            is_tiered_omission_mode(scheduled.mode))
            return local_actor_cohort() != ExperimentOmissionCohort::none;
        return rotating_actor(proposal) ==
               std::optional<ReplicaID>{scheduled.local_replica};
    }

    ScheduledDecisionState *scheduled_decision(
        const ExperimentByzantineContext &context,
        ExperimentReplicaRole role,
        std::uint64_t monotonic_ns)
    {
        const auto found = scheduled_decisions.find(context.proposal);
        if (found != scheduled_decisions.end())
            return &found->second;

        const auto &scheduled = *options.rotating_omission;
        if (scheduled_decisions.size() >= scheduled.maximum_contexts)
        {
            emit_scheduled_capacity_marker(context, monotonic_ns);
            return nullptr;
        }

        ScheduledDecisionState decision;
        const bool inside_window =
            monotonic_ns >= scheduled.window_start_monotonic_ns &&
            monotonic_ns < scheduled.window_end_monotonic_ns;
        if (is_tiered_omission_mode(scheduled.mode))
        {
            decision.cohort = local_actor_cohort();
            decision.auditable =
                inside_window && role != ExperimentReplicaRole::root &&
                decision.cohort != ExperimentOmissionCohort::none;
            if (decision.auditable)
            {
                bool omit =
                    decision.cohort == ExperimentOmissionCohort::hard;
                if (decision.cohort ==
                    ExperimentOmissionCohort::responsive_degraded)
                {
                    if (responsive_contribution_ordinal ==
                        std::numeric_limits<std::uint64_t>::max())
                    {
                        emit_scheduled_capacity_marker(
                            context, monotonic_ns);
                        return nullptr;
                    }
                    decision.contribution_ordinal =
                        ++responsive_contribution_ordinal;
                    omit = decision.contribution_ordinal %
                               scheduled.responsive_omission_period ==
                           0;
                }
                if (omit && role == ExperimentReplicaRole::internal)
                    decision.action =
                        ExperimentOmissionAction::omit_aggregate;
                else if (omit && role == ExperimentReplicaRole::leaf)
                    decision.action =
                        ExperimentOmissionAction::omit_direct_vote;
            }
        }
        else if (inside_window && local_actor_selected(context.proposal))
        {
            if (role == ExperimentReplicaRole::internal)
                decision.action = ExperimentOmissionAction::omit_aggregate;
            else if (role == ExperimentReplicaRole::leaf)
                decision.action = ExperimentOmissionAction::omit_direct_vote;
        }
        return &scheduled_decisions
                    .emplace(
                        context.proposal,
                        decision)
                    .first->second;
    }

    void populate_tiered_marker(
        ExperimentOmissionMarker &marker,
        ExperimentOmissionCohort cohort,
        std::uint64_t contribution_ordinal) const
    {
        const auto &scheduled = *options.rotating_omission;
        if (!is_tiered_omission_mode(scheduled.mode))
            return;
        marker.cohort = cohort;
        marker.hard_actor_count = scheduled.actor_ids.size();
        marker.responsive_degraded_actor_count =
            scheduled.responsive_degraded_actor_ids.size();
        marker.fault_threshold = scheduled_fault_threshold;
        marker.max_omissions_per_proposal =
            scheduled.max_omissions_per_proposal;
        marker.responsive_omission_period =
            scheduled.responsive_omission_period;
        marker.contribution_ordinal = contribution_ordinal;
    }

    void emit_scheduled_capacity_marker(
        const ExperimentByzantineContext &context,
        std::uint64_t monotonic_ns)
    {
        if (scheduled_capacity_marker_emitted)
            return;
        scheduled_capacity_marker_emitted = true;
        if (!options.omission_marker_emitter)
            return;
        const auto &scheduled = *options.rotating_omission;
        ExperimentOmissionMarker marker{
            context.proposal,
            context.diagnostic_window,
            scheduled.mode,
            scheduled.local_replica,
            ExperimentOmissionAction::capacity_exhausted,
            scheduled.window_start_monotonic_ns,
            scheduled.window_end_monotonic_ns,
            monotonic_ns};
        populate_tiered_marker(
            marker, local_actor_cohort(), 0);
        options.omission_marker_emitter(marker);
    }

    void emit_scheduled_marker(
        const ExperimentByzantineContext &context,
        ScheduledDecisionState &decision,
        std::uint64_t monotonic_ns)
    {
        if (decision.marker_emitted)
            return;
        decision.marker_emitted = true;
        if (!options.omission_marker_emitter)
            return;
        const auto &scheduled = *options.rotating_omission;
        ExperimentOmissionMarker marker{
            context.proposal,
            context.diagnostic_window,
            scheduled.mode,
            scheduled.local_replica,
            decision.action,
            scheduled.window_start_monotonic_ns,
            scheduled.window_end_monotonic_ns,
            monotonic_ns};
        populate_tiered_marker(
            marker, decision.cohort, decision.contribution_ordinal);
        options.omission_marker_emitter(marker);
    }

    ExperimentByzantineOptions options;
    std::map<
        ExperimentByzantineContext,
        FalseReportState,
        ContextLess>
        false_reports;
    std::set<ExperimentByzantineContext, ContextLess> omissions;
    std::set<ExperimentByzantineContext, ContextLess> omission_markers;
    std::set<ExperimentByzantineContext, ContextLess>
        direct_vote_omissions;
    std::map<ProposalKey, ScheduledDecisionState> scheduled_decisions;
    std::uint64_t responsive_contribution_ordinal{0};
    std::size_t scheduled_fault_threshold{0};
    bool scheduled_capacity_marker_emitted{false};
};

ExperimentByzantineAdapter::ExperimentByzantineAdapter(
    ExperimentByzantineOptions options)
    : state_(std::make_unique<State>(std::move(options)))
{}

ExperimentByzantineAdapter::~ExperimentByzantineAdapter() = default;

bool ExperimentByzantineAdapter::arm_false_report(
    const ExperimentByzantineContext &context,
    ReplicaID target)
{
    if (!exact_context(state_->options, context) ||
        !state_->options.false_report_target.has_value() ||
        *state_->options.false_report_target != target)
        return false;

    const auto existing = state_->false_reports.find(context);
    if (existing != state_->false_reports.end())
        return true;
    if (state_->false_reports.size() >=
        state_->options.maximum_false_report_contexts)
        return false;
    return state_->false_reports
        .emplace(context, State::FalseReportState{})
        .second;
}

bool ExperimentByzantineAdapter::on_verified_response(
    const ExperimentByzantineContext &context,
    ReplicaID target) noexcept
{
    if (!state_->options.false_report_target.has_value() ||
        *state_->options.false_report_target != target)
        return false;
    const auto found = state_->false_reports.find(context);
    if (found == state_->false_reports.end())
        return false;
    found->second.verified_response_observed = true;
    return true;
}

bool ExperimentByzantineAdapter::consume_false_report_positive_marker(
    const ExperimentByzantineContext &context,
    ReplicaID target) noexcept
{
    if (!state_->options.false_report_target.has_value() ||
        *state_->options.false_report_target != target)
        return false;
    const auto found = state_->false_reports.find(context);
    if (found == state_->false_reports.end() ||
        !found->second.verified_response_observed ||
        found->second.positive_marker_consumed)
        return false;
    found->second.positive_marker_consumed = true;
    return true;
}

bool ExperimentByzantineAdapter::should_retain_response_evidence(
    const ExperimentByzantineContext &context) const noexcept
{
    const auto found = state_->false_reports.find(context);
    return found != state_->false_reports.end() &&
           found->second.verified_response_observed &&
           !found->second.timeout_consumed;
}

bool ExperimentByzantineAdapter::cancel_false_report(
    const ExperimentByzantineContext &context,
    ReplicaID target) noexcept
{
    if (!state_->options.false_report_target.has_value() ||
        *state_->options.false_report_target != target)
        return false;
    const auto found = state_->false_reports.find(context);
    if (found == state_->false_reports.end())
        return false;
    state_->false_reports.erase(found);
    return true;
}

bool ExperimentByzantineAdapter::consume_false_timeout(
    const ExperimentByzantineContext &context,
    ReplicaID target) noexcept
{
    if (!state_->options.false_report_target.has_value() ||
        *state_->options.false_report_target != target)
        return false;
    const auto found = state_->false_reports.find(context);
    if (found == state_->false_reports.end() ||
        !found->second.verified_response_observed ||
        found->second.timeout_consumed)
        return false;
    found->second.timeout_consumed = true;
    return true;
}

bool ExperimentByzantineAdapter::consume_outbound_aggregate(
    const ExperimentByzantineContext &context,
    ExperimentReplicaRole role,
    std::uint64_t monotonic_ns)
{
    if (state_->options.rotating_omission.has_value())
    {
        if (!state_->options.enabled ||
            context.diagnostic_window != state_->options.diagnostic_window)
            return false;
        auto *decision =
            state_->scheduled_decision(context, role, monotonic_ns);
        if (decision == nullptr)
            return false;
        const bool tiered_audit =
            is_tiered_omission_mode(
                state_->options.rotating_omission->mode) &&
            decision->auditable;
        if (tiered_audit)
            state_->emit_scheduled_marker(
                context, *decision, monotonic_ns);
        if (decision->action != ExperimentOmissionAction::omit_aggregate)
            return false;
        if (!tiered_audit)
            state_->emit_scheduled_marker(
                context, *decision, monotonic_ns);
        return true;
    }
    if (!state_->options.omit_outbound_aggregate ||
        role != ExperimentReplicaRole::internal ||
        !exact_omission_context(state_->options, context))
        return false;
    if (state_->omissions.find(context) != state_->omissions.end())
        return true;
    if (state_->omissions.size() >=
        state_->options.maximum_omission_contexts)
        return false;
    return state_->omissions.insert(context).second;
}

bool ExperimentByzantineAdapter::consume_outbound_aggregate_marker(
    const ExperimentByzantineContext &context) noexcept
{
    if (state_->omissions.find(context) == state_->omissions.end())
        return false;
    return state_->omission_markers.insert(context).second;
}

ExperimentDirectVoteDisposition
ExperimentByzantineAdapter::consume_outbound_direct_vote(
    const ExperimentByzantineContext &context,
    ExperimentReplicaRole role,
    std::uint64_t monotonic_ns)
{
    if (state_->options.rotating_omission.has_value())
    {
        if (!state_->options.enabled ||
            context.diagnostic_window != state_->options.diagnostic_window)
            return ExperimentDirectVoteDisposition::forward;
        auto *decision =
            state_->scheduled_decision(context, role, monotonic_ns);
        if (decision == nullptr)
            return ExperimentDirectVoteDisposition::forward;
        const bool tiered_audit =
            is_tiered_omission_mode(
                state_->options.rotating_omission->mode) &&
            decision->auditable;
        if (tiered_audit)
            state_->emit_scheduled_marker(
                context, *decision, monotonic_ns);
        if (decision->action != ExperimentOmissionAction::omit_direct_vote)
            return ExperimentDirectVoteDisposition::forward;
        if (decision->direct_vote_omitted)
            return ExperimentDirectVoteDisposition::omit_repeat;
        decision->direct_vote_omitted = true;
        if (!tiered_audit)
            state_->emit_scheduled_marker(
                context, *decision, monotonic_ns);
        return ExperimentDirectVoteDisposition::omit_first;
    }
    if (!state_->options.omit_outbound_direct_vote ||
        role != ExperimentReplicaRole::leaf ||
        !exact_context(state_->options, context))
        return ExperimentDirectVoteDisposition::forward;
    if (state_->direct_vote_omissions.find(context) !=
        state_->direct_vote_omissions.end())
        return ExperimentDirectVoteDisposition::omit_repeat;
    if (state_->direct_vote_omissions.size() >=
        state_->options.maximum_direct_vote_omission_contexts)
        return ExperimentDirectVoteDisposition::forward;
    if (!state_->direct_vote_omissions.insert(context).second)
        return ExperimentDirectVoteDisposition::forward;
    return ExperimentDirectVoteDisposition::omit_first;
}

bool ExperimentByzantineAdapter::outbound_direct_vote_omitted(
    const ExperimentByzantineContext &context) const noexcept
{
    const auto scheduled =
        state_->scheduled_decisions.find(context.proposal);
    if (scheduled != state_->scheduled_decisions.end() &&
        scheduled->second.direct_vote_omitted)
        return true;
    return state_->direct_vote_omissions.find(context) !=
           state_->direct_vote_omissions.end();
}

std::optional<ReplicaID>
ExperimentByzantineAdapter::rotating_omission_actor(
    const ProposalKey &proposal) const
{
    return state_->rotating_actor(proposal);
}

bool ExperimentByzantineAdapter::rotating_omission_enabled() const noexcept
{
    return state_->options.enabled &&
           state_->options.rotating_omission.has_value() &&
           is_rotating_omission_mode(
               state_->options.rotating_omission->mode);
}

bool ExperimentByzantineAdapter::scheduled_omission_enabled() const noexcept
{
    return state_->options.enabled &&
           state_->options.rotating_omission.has_value() &&
           is_scheduled_omission_mode(
               state_->options.rotating_omission->mode);
}

} // namespace hotstuff
