/**
 * Aggregation timeout policy and exact proposal-context coordination.
 */

#ifndef HOTSTUFF_AGGREGATION_H_INCLUDED
#define HOTSTUFF_AGGREGATION_H_INCLUDED

#include <chrono>
#include <cstdint>
#include <functional>
#include <set>

#include "hotstuff/proposal_context.h"

namespace hotstuff
{

class AggregationScheduler
{
public:
    using Duration = std::chrono::nanoseconds;
    using Callback = std::function<void()>;
    using Cancellation = std::function<void()>;

    virtual ~AggregationScheduler() = default;
    virtual Cancellation schedule_after(Duration delay,
                                        Callback callback) = 0;
};

class AggregationTimeoutEffects
{
public:
    std::function<void(const ProposalContextLease &,
                       ProposalForwardingClaim)>
        send_upward;
    std::function<void(const ProposalContextLease &,
                       const std::set<ReplicaID> &)>
        record_timeout;
};

class AggregationTimeoutPolicy
{
public:
    using Duration = AggregationScheduler::Duration;

    explicit AggregationTimeoutPolicy(Duration per_remaining_level);

    Duration timeout_for(std::uint32_t level,
                         std::uint32_t maximum_level) const;

private:
    Duration per_remaining_level_;
};

class AggregationTimeoutCoordinator
{
public:
    using VerifiedCandidateProvider =
        std::function<quorum_cert_bt(const ProposalContextLease &)>;

    AggregationTimeoutCoordinator(
        ProposalContextLifecycle &contexts,
        AggregationTimeoutPolicy policy,
        AggregationTimeoutEffects effects,
        VerifiedCandidateProvider verified_candidate_provider);

    std::uint64_t arm_timeout(const ProposalContextLease &lease,
                              AggregationScheduler &scheduler,
                              std::uint32_t level,
                              std::uint32_t maximum_level);
    bool dispatch_timeout(const ProposalKey &key,
                          std::uint64_t timer_generation);

private:
    void apply_timeout(const ProposalContextLease &lease);

    ProposalContextLifecycle &contexts_;
    AggregationTimeoutPolicy policy_;
    AggregationTimeoutEffects effects_;
    VerifiedCandidateProvider verified_candidate_provider_;
};

} // namespace hotstuff

#endif
