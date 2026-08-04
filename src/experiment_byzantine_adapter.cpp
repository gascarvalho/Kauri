#include "hotstuff/experiment_byzantine_adapter.h"

#include <algorithm>
#include <cstdint>
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
            << " fault=" << kRotatingOmissionMode
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

    struct RotatingDecisionState
    {
        ExperimentOmissionAction action{ExperimentOmissionAction::forward};
        bool marker_emitted{false};
        bool direct_vote_omitted{false};
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
            auto &rotating = *options.rotating_omission;
            if (rotating.mode != kRotatingOmissionMode)
                throw std::invalid_argument(
                    "unsupported rotating omission mode");
            if (options.additional_omission_configuration.has_value() ||
                options.maximum_false_report_contexts != 0 ||
                options.maximum_omission_contexts != 0 ||
                options.maximum_direct_vote_omission_contexts != 0)
                throw std::invalid_argument(
                    "rotating omission cannot be combined with static "
                    "fault configuration");
            if (rotating.replica_count == 0 ||
                rotating.local_replica >= rotating.replica_count)
                throw std::invalid_argument(
                    "rotating omission requires an in-range local replica");
            const auto quorum =
                derive_byzantine_quorum(rotating.replica_count);
            if (!quorum.has_value() || rotating.expected_actor_count == 0 ||
                rotating.expected_actor_count > quorum->fault_threshold ||
                rotating.actor_ids.size() != rotating.expected_actor_count)
                throw std::invalid_argument(
                    "rotating omission actor count must be within the "
                    "derived fault threshold");
            std::sort(rotating.actor_ids.begin(), rotating.actor_ids.end());
            if (std::adjacent_find(
                    rotating.actor_ids.begin(), rotating.actor_ids.end()) !=
                rotating.actor_ids.end())
                throw std::invalid_argument(
                    "rotating omission actors must be unique");
            if (std::any_of(
                    rotating.actor_ids.begin(),
                    rotating.actor_ids.end(),
                    [&rotating](ReplicaID actor)
                    { return actor >= rotating.replica_count; }))
                throw std::invalid_argument(
                    "rotating omission actor is outside membership");
            if (rotating.window_start_monotonic_ns == 0 ||
                rotating.window_end_monotonic_ns <=
                    rotating.window_start_monotonic_ns)
                throw std::invalid_argument(
                    "rotating omission window must be a non-empty future "
                    "monotonic interval");
            if (rotating.max_omissions_per_proposal != 1)
                throw std::invalid_argument(
                    "rotating omission permits exactly one omission per "
                    "proposal");
            if (rotating.maximum_contexts == 0)
                throw std::invalid_argument(
                    "rotating omission context bound must be positive");
        }
    }

    std::optional<ReplicaID> rotating_actor(
        const ProposalKey &proposal) const
    {
        if (!options.enabled || !options.rotating_omission.has_value())
            return std::nullopt;
        const auto &actors = options.rotating_omission->actor_ids;
        return actors[proposal_actor_index(proposal, actors.size())];
    }

    RotatingDecisionState *rotating_decision(
        const ExperimentByzantineContext &context,
        ExperimentReplicaRole role,
        std::uint64_t monotonic_ns)
    {
        const auto found = rotating_decisions.find(context.proposal);
        if (found != rotating_decisions.end())
            return &found->second;

        const auto &rotating = *options.rotating_omission;
        if (rotating_decisions.size() >= rotating.maximum_contexts)
        {
            emit_rotating_capacity_marker(context, monotonic_ns);
            return nullptr;
        }

        ExperimentOmissionAction action = ExperimentOmissionAction::forward;
        const auto selected_actor = rotating_actor(context.proposal);
        if (monotonic_ns >= rotating.window_start_monotonic_ns &&
            monotonic_ns < rotating.window_end_monotonic_ns &&
            selected_actor ==
                std::optional<ReplicaID>{rotating.local_replica})
        {
            if (role == ExperimentReplicaRole::internal)
                action = ExperimentOmissionAction::omit_aggregate;
            else if (role == ExperimentReplicaRole::leaf)
                action = ExperimentOmissionAction::omit_direct_vote;
        }
        return &rotating_decisions
                    .emplace(
                        context.proposal,
                        RotatingDecisionState{action, false, false})
                    .first->second;
    }

    void emit_rotating_capacity_marker(
        const ExperimentByzantineContext &context,
        std::uint64_t monotonic_ns)
    {
        if (rotating_capacity_marker_emitted)
            return;
        rotating_capacity_marker_emitted = true;
        if (!options.omission_marker_emitter)
            return;
        const auto &rotating = *options.rotating_omission;
        options.omission_marker_emitter(ExperimentOmissionMarker{
            context.proposal,
            context.diagnostic_window,
            rotating.local_replica,
            ExperimentOmissionAction::capacity_exhausted,
            rotating.window_start_monotonic_ns,
            rotating.window_end_monotonic_ns,
            monotonic_ns});
    }

    void emit_rotating_marker(
        const ExperimentByzantineContext &context,
        RotatingDecisionState &decision,
        std::uint64_t monotonic_ns)
    {
        if (decision.marker_emitted)
            return;
        decision.marker_emitted = true;
        if (!options.omission_marker_emitter)
            return;
        const auto &rotating = *options.rotating_omission;
        options.omission_marker_emitter(ExperimentOmissionMarker{
            context.proposal,
            context.diagnostic_window,
            rotating.local_replica,
            decision.action,
            rotating.window_start_monotonic_ns,
            rotating.window_end_monotonic_ns,
            monotonic_ns});
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
    std::map<ProposalKey, RotatingDecisionState> rotating_decisions;
    bool rotating_capacity_marker_emitted{false};
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
            state_->rotating_decision(context, role, monotonic_ns);
        if (decision == nullptr ||
            decision->action != ExperimentOmissionAction::omit_aggregate)
            return false;
        state_->emit_rotating_marker(context, *decision, monotonic_ns);
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
            state_->rotating_decision(context, role, monotonic_ns);
        if (decision == nullptr ||
            decision->action != ExperimentOmissionAction::omit_direct_vote)
            return ExperimentDirectVoteDisposition::forward;
        if (decision->direct_vote_omitted)
            return ExperimentDirectVoteDisposition::omit_repeat;
        decision->direct_vote_omitted = true;
        state_->emit_rotating_marker(context, *decision, monotonic_ns);
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
    const auto rotating = state_->rotating_decisions.find(context.proposal);
    if (rotating != state_->rotating_decisions.end() &&
        rotating->second.direct_vote_omitted)
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
           state_->options.rotating_omission.has_value();
}

} // namespace hotstuff
