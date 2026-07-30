#include "hotstuff/experiment_byzantine_adapter.h"

#include <map>
#include <set>
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

} // namespace

struct ExperimentByzantineAdapter::State
{
    struct FalseReportState
    {
        bool verified_response_observed{false};
        bool timeout_consumed{false};
    };

    explicit State(ExperimentByzantineOptions configured)
        : options(std::move(configured))
    {
        if (!options.enabled)
            return;
        if (options.diagnostic_window.empty())
            throw std::invalid_argument(
                "diagnostic window must be non-empty");
        if (!options.false_report_target.has_value() &&
            !options.omit_outbound_aggregate)
            throw std::invalid_argument(
                "enabled Byzantine adapter requires a fault mode");
        if (options.false_report_target.has_value() &&
            options.maximum_false_report_contexts == 0)
            throw std::invalid_argument(
                "false-report context bound must be positive");
        if (options.omit_outbound_aggregate &&
            options.maximum_omission_contexts == 0)
            throw std::invalid_argument(
                "omission context bound must be positive");
    }

    ExperimentByzantineOptions options;
    std::map<
        ExperimentByzantineContext,
        FalseReportState,
        ContextLess>
        false_reports;
    std::set<ExperimentByzantineContext, ContextLess> omissions;
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
    const ExperimentByzantineContext &context)
{
    if (!state_->options.omit_outbound_aggregate ||
        !exact_context(state_->options, context))
        return false;
    if (state_->omissions.find(context) != state_->omissions.end())
        return true;
    if (state_->omissions.size() >=
        state_->options.maximum_omission_contexts)
        return false;
    return state_->omissions.insert(context).second;
}

} // namespace hotstuff
