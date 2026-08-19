/**
 * Adaptive-v2 live response-attempt evidence bridge.
 */

#ifndef HOTSTUFF_ADAPTIVE_V2_RESPONSE_EVIDENCE_H_INCLUDED
#define HOTSTUFF_ADAPTIVE_V2_RESPONSE_EVIDENCE_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <set>
#include <vector>

#include "hotstuff/adaptation.h"
#include "hotstuff/evidence_reporter.h"
#include "hotstuff/proposal_context.h"

namespace hotstuff
{

struct AdaptiveV2ResponseEvidenceLimits
{
    ResponseAttemptLimits attempts;
    EvidenceReporterLimits reporter;
    EvidenceWireLimits wire;
    std::size_t maximum_handles{4096};
    // Ordinary spillover behind the reporter FIFO. Late compensation has a
    // separate per-attempt reservation below.
    std::size_t maximum_retained_facts{4096};
    // One preallocated emergency slot per timeout-eligible active attempt.
    // A late response can therefore be retained even while both ordinary
    // outbox queues are saturated.
    std::size_t maximum_late_compensations{4096};
    bool exact_timeout_attempt_evidence_v3{false};
};

struct AdaptiveV2ResponseEvidenceDiagnostics
{
    std::size_t active_handles{0};
    std::size_t active_deadlines{0};
    std::size_t pending_reports{0};
    std::size_t retained_facts{0};
    std::size_t retention_capacity{0};
    std::size_t pending_late_compensations{0};
    std::size_t late_compensation_capacity{0};
    std::uint64_t armed_attempts{0};
    std::uint64_t armed_deadlines{0};
    std::uint64_t fired_deadlines{0};
    std::uint64_t completed_deadlines{0};
    std::uint64_t closed_contexts_retained{0};
    std::uint64_t deadline_schedule_failures{0};
    std::uint64_t deadline_callback_failures{0};
    std::uint64_t deadline_delivery_failures{0};
    std::uint64_t deadline_cancellations{0};
    std::uint64_t deadline_cancellation_failures{0};
    std::uint64_t response_facts{0};
    std::uint64_t idempotent_duplicate_responses{0};
    std::uint64_t timeout_facts{0};
    std::uint64_t timeout_missing_handles{0};
    std::uint64_t timeout_ineligible_attempts{0};
    std::uint64_t timeout_tracker_rejections{0};
    std::uint64_t timeout_exceptions{0};
    std::uint64_t retired_attempts{0};
    std::uint64_t rejected_operations{0};
    std::uint64_t capacity_failures{0};
    std::uint64_t retention_capacity_failures{0};
    std::uint64_t late_compensation_capacity_failures{0};
    std::uint64_t enqueue_failures{0};
    std::uint64_t retry_schedules{0};
    std::uint64_t retry_schedule_failures{0};
    bool transport_bound{false};
    bool retry_scheduler_bound{false};
    bool deadline_scheduler_bound{false};
    bool deadline_result_callback_bound{false};
    bool retry_scheduled{false};
    bool healthy{true};
};

using EvidenceRetryCallback = std::function<void()>;
using EvidenceRetryCancellation = std::function<void()>;
using EvidenceRetryScheduler = std::function<EvidenceRetryCancellation(
    EvidenceRetryCallback)>;
using EvidenceDeadlineCallback =
    std::function<void(std::uint64_t)>;
using EvidenceDeadlineFailureCallback = std::function<void()>;
using EvidenceDeadlineCancellation = std::function<void()>;
using EvidenceDeadlineScheduler =
    std::function<EvidenceDeadlineCancellation(
        const ProposalKey &,
        std::uint64_t,
        EvidenceDeadlineCallback,
        EvidenceDeadlineFailureCallback)>;
enum class EvidenceDeadlineResult : std::uint8_t
{
    evidence_accepted = 1,
    failed,
};
using EvidenceDeadlineResultCallback = std::function<void(
    const ProposalKey &,
    EvidenceDeadlineResult)>;

enum class AdaptiveV2CrossCommitRetentionAdmissionStatus : std::uint8_t
{
    ready = 1,
    incomplete,
    invalid,
};

enum class AdaptiveV2CrossCommitRetentionAdmissionPolicy : std::uint8_t
{
    one_per_actor_with_global_aggregate_v1 = 1,
    aggregate_relay_per_actor_v1,
};

struct AdaptiveV2CrossCommitRetentionAdmission
{
    AdaptiveV2CrossCommitRetentionAdmissionStatus status{
        AdaptiveV2CrossCommitRetentionAdmissionStatus::invalid};
    std::uint64_t evidence_cutoff{0};
    std::vector<ReplicaID> responsive_degraded_actor_ids;
    std::vector<uint256_t> admitted_observation_ids;
};

/**
 * Pure same-cutoff replay of a schema-v2 retention-readiness predicate.
 * One actor-sorted witness is selected per expected observed child/target.
 * Reporter authenticity remains an EvidenceLedger admission invariant.
 * Aggregate relay is preferred, then lower ingestion sequence, then ID.
 * The default preserves the historical repair-only admission contract.
 */
AdaptiveV2CrossCommitRetentionAdmission
select_adaptive_v2_cross_commit_retention_admission(
    const std::vector<AcceptedEvidenceRecord> &accepted,
    const AdaptationEpochId &current_epoch,
    std::uint64_t evidence_cutoff,
    const std::vector<ReplicaID> &responsive_degraded_actor_ids,
    AdaptiveV2CrossCommitRetentionAdmissionPolicy admission_policy =
        AdaptiveV2CrossCommitRetentionAdmissionPolicy::
            one_per_actor_with_global_aggregate_v1) noexcept;

/**
 * Event-loop-confined adapter from exact proposal callbacks to immutable
 * response evidence. An injected scheduler may retain only the immutable
 * attempt identity until its original observation deadline; it never retains
 * or reopens consensus context. The bridge owns no network, manager authority,
 * quorum state, or topology decisions. Callers provide explicit monotonic
 * times and only already authenticated, topology-verified signer sets.
 *
 * A transport result acknowledges only this local outbox delivery attempt.
 * It is not evidence that an adaptation manager accepted the observation.
 * Calls, including the injected callback, must be externally serialized and
 * non-reentrant.
 */
class AdaptiveV2ResponseEvidenceBridge final
{
public:
    explicit AdaptiveV2ResponseEvidenceBridge(
        ReplicaID reporter_id,
        AdaptiveV2ResponseEvidenceLimits limits = {});
    ~AdaptiveV2ResponseEvidenceBridge();

    AdaptiveV2ResponseEvidenceBridge(
        const AdaptiveV2ResponseEvidenceBridge &) = delete;
    AdaptiveV2ResponseEvidenceBridge &operator=(
        const AdaptiveV2ResponseEvidenceBridge &) = delete;
    AdaptiveV2ResponseEvidenceBridge(
        AdaptiveV2ResponseEvidenceBridge &&) = delete;
    AdaptiveV2ResponseEvidenceBridge &operator=(
        AdaptiveV2ResponseEvidenceBridge &&) = delete;

    /** Enable schema-v2 retained-commit observations before any arm. */
    bool enable_cross_commit_retention_v2() noexcept;
    bool enable_exact_timeout_attempt_evidence_v3() noexcept;

    bool arm(
        const ProposalKey &proposal,
        const ProposalTreeSnapshot &tree,
        std::uint64_t start_monotonic_ns,
        std::uint64_t deadline_duration_us) noexcept;

    /**
     * Arm attempts and their observation-only deadline transactionally. This
     * is the production entry point. Failure cancels and retires the exact
     * attempt without affecting consensus progress.
     */
    bool arm_with_deadline(
        const ProposalKey &proposal,
        const ProposalTreeSnapshot &tree,
        std::uint64_t start_monotonic_ns,
        std::uint64_t deadline_duration_us) noexcept;

    bool record_verified_response(
        const ProposalKey &proposal,
        ReplicaID authenticated_sender,
        ExpectedMessageType message_type,
        const std::set<ReplicaID> &canonical_verified_signers,
        std::uint64_t response_monotonic_ns) noexcept;

    std::size_t record_timeouts(
        const ProposalKey &proposal,
        const std::set<ReplicaID> &exact_missing_direct_children,
        std::uint64_t timeout_monotonic_ns) noexcept;

    /** Retain the first reporter-local authoritative commit timestamp. */
    bool record_reporter_local_commit(
        const ProposalKey &proposal,
        std::uint64_t commit_monotonic_ns) noexcept;

    /**
     * Mark an authoritative-commit cleanup without discarding its already
     * armed observations. A fired deadline remains an ordering fence until
     * this close and every retained evidence fact is accepted. A fully
     * answered pre-deadline attempt may still cancel and retire immediately.
     */
    bool close_consensus_context(const ProposalKey &proposal) noexcept;
    bool should_defer_commit_report(
        const ProposalKey &proposal) const noexcept;

    std::size_t retire(const ProposalKey &proposal) noexcept;
    void shutdown() noexcept;

    /** The scheduler must defer both callbacks and return a cancellation. */
    void bind_deadline_scheduler(EvidenceDeadlineScheduler scheduler);
    void unbind_deadline_scheduler() noexcept;
    void bind_deadline_result_callback(
        EvidenceDeadlineResultCallback callback);
    void unbind_deadline_result_callback() noexcept;

    /**
     * Bind a one-shot event-loop scheduler for retrying temporary transport
     * failures. The scheduler must defer the callback and return a callable
     * cancellation; it grants no manager or consensus authority.
     */
    void bind_retry_scheduler(EvidenceRetryScheduler scheduler);
    void unbind_retry_scheduler() noexcept;
    void bind_transport(EvidenceTransportCallback transport);
    void unbind_transport() noexcept;
    std::size_t flush() noexcept;

    const PendingEvidenceReport *front() const noexcept;
    AdaptiveV2ResponseEvidenceDiagnostics diagnostics() const noexcept;

private:
    bool schedule_deadline(
        const ProposalKey &proposal,
        std::uint64_t deadline_duration_us) noexcept;
    void dispatch_deadline(
        const ProposalKey &proposal,
        std::uint64_t generation,
        std::uint64_t timeout_monotonic_ns) noexcept;
    void fail_deadline(
        const ProposalKey &proposal,
        std::uint64_t generation) noexcept;
    std::size_t record_timeouts_impl(
        const ProposalKey &proposal,
        const std::set<ReplicaID> &exact_missing_direct_children,
        std::uint64_t timeout_monotonic_ns,
        bool produced_by_deadline) noexcept;
    void acknowledge_accepted_evidence(
        const ProposalKey &proposal) noexcept;
    void mark_deadline_delivery_failed(
        const ProposalKey &proposal) noexcept;
    void mark_all_deadline_deliveries_failed() noexcept;
    void finalize_ready_deadlines() noexcept;
    void complete_deadline(
        const ProposalKey &proposal,
        std::uint64_t generation) noexcept;
    void fail_deadline_delivery(
        const ProposalKey &proposal,
        std::uint64_t generation) noexcept;
    void notify_deadline_result(
        const ProposalKey &proposal,
        EvidenceDeadlineResult result) noexcept;
    void schedule_retry() noexcept;
    void cancel_retry() noexcept;
    void run_scheduled_retry() noexcept;

    struct State;
    std::unique_ptr<State> state_;
};

} // namespace hotstuff

#endif
