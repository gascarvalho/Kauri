/**
 * Aggregation timeout policy and exact proposal-context coordination.
 */

#ifndef HOTSTUFF_AGGREGATION_H_INCLUDED
#define HOTSTUFF_AGGREGATION_H_INCLUDED

#include <chrono>
#include <cstdint>
#include <functional>
#include <memory>
#include <set>

#include "hotstuff/proposal_context.h"

namespace hotstuff
{

enum class AggregationForwardingObservation
{
    reserved,
    enqueued,
    committed,
    released,
};

class AggregationScheduler
{
public:
    using Duration = std::chrono::nanoseconds;
    using Callback = std::function<void()>;
    using Cancellation = std::function<void()>;

    virtual ~AggregationScheduler() = default;
    virtual Duration monotonic_now() const noexcept = 0;
    virtual Cancellation schedule_after(Duration delay,
                                        Callback callback) = 0;
};

class AggregationTimeoutEffects
{
public:
    // Returns whether transport accepted the enqueue. Production uses this
    // transactional callback; send_upward remains for legacy test adapters.
    std::function<bool(const ProposalContextLease &,
                       ProposalForwardingClaim)>
        try_send_upward;
    std::function<void(const ProposalContextLease &,
                       ProposalForwardingClaim)>
        send_upward;
    std::function<void(const ProposalContextLease &,
                       const std::set<ReplicaID> &)>
        record_timeout;
    // Neutral observation only. Wait-exempt absence cannot produce timeout,
    // reputation, conviction, or leader-suspicion effects.
    std::function<void(const ProposalContextLease &,
                       const std::set<ReplicaID> &)>
        record_optional_absence;
    std::function<void(const ProposalContextLease &,
                       const std::set<ReplicaID> &,
                       AggregationForwardingObservation)>
        record_initial_forwarding;
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

// A stranded descendant first waits for the root's all-member proposal
// retransmission and then waits the same full-tree deadline before sending its
// already-signed individual vote to that root.
AggregationTimeoutPolicy::Duration exact_fallback_recovery_horizon(
    AggregationTimeoutPolicy::Duration maximum_level_aware_deadline);

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
    class ScheduledCancellation;

    void schedule_until_deadline(
        const ProposalKey &key,
        std::uint64_t timer_generation,
        AggregationScheduler &scheduler,
        AggregationScheduler::Duration deadline,
        const std::shared_ptr<ScheduledCancellation> &cancellation);
    void apply_timeout(const ProposalContextLease &lease);

    ProposalContextLifecycle &contexts_;
    AggregationTimeoutPolicy policy_;
    AggregationTimeoutEffects effects_;
    VerifiedCandidateProvider verified_candidate_provider_;
};

} // namespace hotstuff

#endif
