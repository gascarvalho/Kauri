#include "hotstuff/aggregation.h"

#include <algorithm>
#include <iterator>
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

AggregationTimeoutPolicy::Duration exact_fallback_recovery_horizon(
    AggregationTimeoutPolicy::Duration maximum_level_aware_deadline)
{
    if (maximum_level_aware_deadline <=
        AggregationTimeoutPolicy::Duration::zero())
        throw std::invalid_argument(
            "fallback recovery deadline must be positive");
    if (maximum_level_aware_deadline >
        AggregationTimeoutPolicy::Duration::max() / 2)
        throw std::overflow_error(
            "fallback recovery deadline overflow");
    return maximum_level_aware_deadline * 2;
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
    const auto missing_required =
        contexts_.pending_required_child_branches(lease);
    const auto pending_observation =
        contexts_.pending_children(lease);
    const auto missing_optional =
        contexts_.missing_optional_signers(lease);
    if (!missing_required.has_value() ||
        !pending_observation.has_value() ||
        !missing_optional.has_value())
        return;

    std::set<ReplicaID> unobserved_required_children;
    std::set_intersection(
        missing_required->begin(),
        missing_required->end(),
        pending_observation->begin(),
        pending_observation->end(),
        std::inserter(
            unobserved_required_children,
            unobserved_required_children.end()));

    const auto record_optional_absence = [&]() {
        if (!missing_optional->empty() &&
            effects_.record_optional_absence)
            effects_.record_optional_absence(lease, *missing_optional);
    };

    // The first aggregate owns this exact generation across enqueue retries.
    // Do not create an overlapping aggregate, but retain neutral optional
    // observation at the fired deadline.
    if (contexts_.initial_forwarding_owned(lease))
    {
        record_optional_absence();
        return;
    }

    // An optional-only observation deadline is not an aggregation timeout.
    // It neither flushes nor changes the exact proposal lifecycle phase.
    if (contexts_.required_subtree_complete(lease))
    {
        record_optional_absence();
        return;
    }

    if (lease.tree().parent.has_value() &&
        verified_candidate_provider_ &&
        (effects_.try_send_upward || effects_.send_upward))
    {
        auto candidate = verified_candidate_provider_(lease);
        auto claim = contexts_.claim_initial_certificate_reservation(
            lease, std::move(candidate));
        if (claim.has_value())
        {
            const auto reservation_id = claim->reservation_id;
            std::set<ReplicaID> observed_signers;
            bool observation_ready = false;
            if (effects_.record_initial_forwarding)
                try
                {
                    observed_signers = claim->signers;
                    observation_ready = true;
                    effects_.record_initial_forwarding(
                        lease,
                        observed_signers,
                        AggregationForwardingObservation::reserved);
                }
                catch (...)
                {
                    observation_ready = false;
                }
            const auto observe = [&](AggregationForwardingObservation state) {
                if (!observation_ready ||
                    !effects_.record_initial_forwarding)
                    return;
                try
                {
                    effects_.record_initial_forwarding(
                        lease, observed_signers, state);
                }
                catch (...)
                {
                }
            };
            bool enqueued = false;
            try
            {
                if (effects_.try_send_upward)
                    enqueued = effects_.try_send_upward(
                        lease, std::move(*claim));
                else
                {
                    effects_.send_upward(lease, std::move(*claim));
                    enqueued = true;
                }
            }
            catch (...)
            {
                enqueued = false;
            }

            if (enqueued)
            {
                observe(AggregationForwardingObservation::enqueued);
                if (contexts_.commit_forwarding_claim(
                        lease, reservation_id))
                    observe(AggregationForwardingObservation::committed);
                else if (contexts_.release_forwarding_claim(
                             lease, reservation_id))
                    observe(AggregationForwardingObservation::released);
            }
            else
            {
                if (contexts_.release_forwarding_claim(
                        lease, reservation_id))
                    observe(AggregationForwardingObservation::released);
            }
        }
    }

    if (contexts_.initial_forwarding_owned(lease))
    {
        // This timeout created the first owner and its enqueue is retrying.
        // Preserve the required-missing observation, but do not open delta
        // forwarding until that exact aggregate is accepted.
        if (effects_.record_timeout)
            effects_.record_timeout(
                lease, unobserved_required_children);
        record_optional_absence();
        return;
    }

    if (contexts_.transition(
            lease, ProposalContextEvent::aggregation_timeout) ==
        ProposalTransitionResult::stale_lease)
        return;

    if (effects_.record_timeout)
        effects_.record_timeout(lease, unobserved_required_children);
    record_optional_absence();
}

} // namespace hotstuff
