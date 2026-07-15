#include "hotstuff/aggregation.h"

#include <algorithm>
#include <limits>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <utility>

namespace hotstuff
{
namespace
{

class ScheduledCancellation final
{
public:
    using Cancellation = AggregationScheduler::Cancellation;

    ~ScheduledCancellation()
    {
        cancel();
    }

    void install(Cancellation cancellation)
    {
        Cancellation cancel_now;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (cancel_requested_)
                cancel_now = std::move(cancellation);
            else
                cancellation_ = std::move(cancellation);
        }
        if (cancel_now)
            cancel_now();
    }

    void cancel()
    {
        Cancellation cancellation;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            cancel_requested_ = true;
            cancellation = std::move(cancellation_);
        }
        if (cancellation)
            cancellation();
    }

private:
    std::mutex mutex_;
    Cancellation cancellation_;
    bool cancel_requested_{false};
};

} // namespace

AggregationTimeoutPolicy::AggregationTimeoutPolicy(
    Duration per_remaining_level)
    : per_remaining_level_(per_remaining_level)
{
    if (per_remaining_level_ <= Duration::zero())
        throw std::invalid_argument(
            "aggregation timeout duration must be positive");
}

AggregationTimeoutPolicy::Duration
AggregationTimeoutPolicy::timeout_for(
    std::uint32_t level,
    std::uint32_t maximum_level) const
{
    const auto bounded_level = std::min(level, maximum_level);
    const auto remaining_levels =
        static_cast<std::uint64_t>(maximum_level - bounded_level) + 1;
    const auto per_level = per_remaining_level_.count();
    const auto maximum_count =
        std::numeric_limits<Duration::rep>::max();
    if (remaining_levels >
        static_cast<std::uint64_t>(maximum_count / per_level))
        throw std::overflow_error("aggregation timeout duration overflow");
    return Duration(
        per_level * static_cast<Duration::rep>(remaining_levels));
}

AggregationTimeoutCoordinator::AggregationTimeoutCoordinator(
    ProposalContextLifecycle &contexts,
    AggregationTimeoutPolicy policy,
    AggregationTimeoutEffects effects,
    VerifiedCandidateProvider verified_candidate_provider)
    : contexts_(contexts),
      policy_(std::move(policy)),
      effects_(std::move(effects)),
      verified_candidate_provider_(
          std::move(verified_candidate_provider))
{}

std::uint64_t AggregationTimeoutCoordinator::arm_timeout(
    const ProposalContextLease &lease,
    AggregationScheduler &scheduler,
    std::uint32_t level,
    std::uint32_t maximum_level)
{
    if (lease.tree().direct_children.empty())
        return 0;

    auto cancellation = std::make_shared<ScheduledCancellation>();
    const auto timer_generation = contexts_.arm_timer(
        lease, [cancellation]() { cancellation->cancel(); });
    if (timer_generation == 0)
        return 0;

    try
    {
        auto scheduled = scheduler.schedule_after(
            policy_.timeout_for(level, maximum_level),
            [this, key = lease.key(), timer_generation]() {
                static_cast<void>(
                    dispatch_timeout(key, timer_generation));
            });
        cancellation->install(std::move(scheduled));
    }
    catch (...)
    {
        static_cast<void>(contexts_.dispatch_timer(
            lease.key(),
            timer_generation,
            [](const ProposalContextLease &) {}));
        throw;
    }
    return timer_generation;
}

bool AggregationTimeoutCoordinator::dispatch_timeout(
    const ProposalKey &key,
    std::uint64_t timer_generation)
{
    return contexts_.dispatch_timer(
        key,
        timer_generation,
        [this](const ProposalContextLease &lease) {
            apply_timeout(lease);
        });
}

void AggregationTimeoutCoordinator::apply_timeout(
    const ProposalContextLease &lease)
{
    const auto missing_children = contexts_.pending_children(lease);
    if (!missing_children.has_value())
        return;

    if (lease.tree().parent.has_value() &&
        verified_candidate_provider_ && effects_.send_upward)
    {
        auto candidate = verified_candidate_provider_(lease);
        auto claim = contexts_.claim_unforwarded_certificate(
            lease, std::move(candidate));
        if (claim.has_value())
            effects_.send_upward(lease, std::move(*claim));
    }

    if (contexts_.transition(
            lease, ProposalContextEvent::aggregation_timeout) ==
        ProposalTransitionResult::stale_lease)
        return;

    if (effects_.record_timeout)
        effects_.record_timeout(lease, *missing_children);
}

} // namespace hotstuff
