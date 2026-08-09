/**
 * Standalone, bounded structured evidence emission.
 */

#ifndef HOTSTUFF_STRUCTURED_EVENT_H_INCLUDED
#define HOTSTUFF_STRUCTURED_EVENT_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <variant>
#include <vector>

#include "hotstuff/adaptive_v2_convergence_wire.h"
#include "hotstuff/adaptive_v2_manager_session.h"
#include "hotstuff/configuration.h"
#include "hotstuff/evidence_reputation.h"
#include "hotstuff/experiment_byzantine_adapter.h"

namespace hotstuff
{

constexpr std::uint32_t kStructuredEventSchemaVersion = 1;
constexpr std::uint32_t kAdaptiveV2EvidenceSnapshotSchemaVersion = 2;

enum class StructuredEventSourceKind : std::uint8_t
{
    replica = 1,
    adaptation_manager,
    orchestrator,
    workload_client,
};

struct StructuredEventSource
{
    StructuredEventSourceKind kind{StructuredEventSourceKind::replica};
    std::string logical_id;
    std::string instance_id;
};

enum class ProcessLifecycleState : std::uint8_t
{
    started = 1,
    ready,
    stopping,
    stopped,
    forced_crash_requested,
    exited,
};

struct ProcessLifecycleEvent
{
    ProcessLifecycleState state{ProcessLifecycleState::started};
    std::optional<std::int32_t> exit_status;
};

enum class EpochLifecycleTransition : std::uint8_t
{
    generated = 1,
    staged,
    acknowledged,
    activation_armed,
    activated,
};

struct EpochLifecycleEvent
{
    EpochLifecycleTransition transition{EpochLifecycleTransition::generated};
    ConfigurationId configuration;
    std::uint64_t activation_height{0};
};

struct CommitStructuredEvent
{
    std::uint64_t block_height{0};
    uint256_t block_hash;
    std::optional<uint256_t> parent_hash;
    std::uint64_t transaction_count{0};
    ProposalKey decision_proof;
    std::optional<std::uint64_t> view_generation;
    std::uint64_t commit_batch_index{0};
};

/**
 * Replica-local witness that one block reached the commit callback.
 *
 * Unlike CommitStructuredEvent, this payload deliberately carries no exact
 * proposal or view identity. Every replica knows these fields at commit time,
 * including when an indirectly committed ancestor has no retained local
 * ProposalKey metadata.
 */
struct CommitObservedStructuredEvent
{
    std::uint64_t block_height{0};
    uint256_t block_hash;
    std::optional<uint256_t> parent_hash;
    std::uint64_t transaction_count{0};
    std::uint64_t commit_batch_index{0};
};

/**
 * Prospective ground truth for one cached experiment-only contribution
 * decision. This record is observational and grants no voting, transport,
 * fault-selection, or epoch authority.
 */
struct FaultContributionOpportunityStructuredEvent
{
    ReplicaID actor{0};
    ProposalKey proposal;
    std::uint64_t view_generation{0};
    ExperimentReplicaRole physical_role{ExperimentReplicaRole::root};
    ReplicaID parent_replica{0};
    ExpectedMessageType expected_message_type{
        ExpectedMessageType::direct_vote};
    ExperimentOmissionCohort cohort{ExperimentOmissionCohort::none};
    std::string diagnostic_window;
    std::uint64_t window_start_monotonic_ns{0};
    std::uint64_t window_end_monotonic_ns{0};
    std::uint64_t decision_monotonic_ns{0};
    std::uint64_t contribution_ordinal{0};
    std::uint64_t role_contribution_ordinal{0};
    ExperimentOmissionAction scheduled_action{
        ExperimentOmissionAction::forward};
    std::size_t responsive_omission_period{0};
    std::size_t fault_threshold{0};
    std::size_t hard_actor_count{0};
    std::size_t responsive_degraded_actor_count{0};
    std::string fault_mode;
};

/**
 * Exact replica-local witness that a fixed-quorum root QC was ready but its
 * later pipelined proposal could not publish through the current queue head.
 * This payload is observational and grants no consensus authority.
 */
struct RootQcQueueBlockedStructuredEvent
{
    ConfigurationId configuration;
    ReplicaID observer_replica{0};
    std::size_t global_quorum{0};
    std::size_t queue_head_position{0};
    std::size_t queued_candidate_position{0};
    std::uint64_t queue_head_context_generation{0};
    std::uint64_t queued_candidate_context_generation{0};
    std::uint64_t queue_head_block_height{0};
    uint256_t queue_head_block_hash;
    std::uint64_t queued_candidate_block_height{0};
    uint256_t queued_candidate_block_hash;
    uint256_t queued_candidate_parent_hash;
    std::size_t queue_head_signer_count{0};
    std::size_t queued_candidate_signer_count{0};
    bool queued_candidate_qc_ready{false};
    bool queued_candidate_qc_published{false};
};

/** Exact schedule installed after one adaptive-v2 command commits. */
struct EpochCommandCommittedStructuredEvent
{
    std::uint64_t command_block_height{0};
    uint256_t command_block_hash;
    std::uint32_t predecessor_epoch_number{0};
    uint256_t predecessor_epoch_digest;
    std::uint32_t successor_epoch_number{0};
    uint256_t successor_epoch_digest;
    uint256_t payload_digest;
    std::uint64_t activation_delay_blocks{0};
    std::uint64_t activation_height{0};
};

/** One accepted-ledger update already applied to the reputation projection. */
struct ReputationEvidenceAppliedStructuredEvent
{
    std::uint64_t evidence_cutoff{0};
    EvidenceReputationAuditUpdate update;
};

/** One complete record newly accepted by the manager evidence ledger. */
struct EvidenceObservationAcceptedStructuredEvent
{
    AcceptedEvidenceRecord record;
};

enum class AdaptiveV2ConvergenceTransition : std::uint8_t
{
    delivery_attempt = 1,
    commit_observed,
    activation_observed,
    converged,
    ready,
    failure,
};

/**
 * Manager-owned audit of delivery and activation-observation convergence.
 *
 * The optional identity is the complete commit-derived identity reported by a
 * replica. Delivery attempts occur before that identity is known. Counters are
 * observational and never replace the fixed quorum used by consensus or the
 * convergence state machine.
 */
struct AdaptiveV2ConvergenceStructuredEvent
{
    AdaptiveV2ConvergenceTransition transition{
        AdaptiveV2ConvergenceTransition::delivery_attempt};
    std::optional<ReplicaID> replica_id;
    std::uint32_t delivery_attempt{0};
    std::string disposition;
    std::optional<AdaptiveV2EpochChangeIdentity> identity;
    std::size_t accepted_commit_count{0};
    std::size_t accepted_activation_count{0};
    std::size_t required_activation_count{0};
    std::optional<uint256_t> canonical_payload_digest;
    std::string failure_reason;
};

/** Bounded commitment to the accepted evidence that caused one transition. */
struct AdaptiveV2EvidenceSnapshotStructuredEvent
{
    std::uint32_t schema_version{
        kAdaptiveV2EvidenceSnapshotSchemaVersion};
    std::uint64_t cycle_ordinal{0};
    TreePolicyKind policy_intent{
        TreePolicyKind::performance_optimization};
    std::string transition_artifact_id;
    std::uint32_t predecessor_epoch_number{0};
    uint256_t predecessor_epoch_digest;
    std::uint64_t activation_generation{0};
    std::uint64_t baseline_cutoff{0};
    std::uint64_t current_cutoff{0};
    /** Digest over every accepted predecessor record through current_cutoff. */
    uint256_t full_prefix_snapshot_id;
    /** Selection digest carried by the signed successor epoch definition. */
    uint256_t evidence_snapshot_id;
    std::uint64_t accepted_prefix_count{0};
    std::vector<ReplicaID> eligible_ranking;
};

/** Canonical manager-owned audit for one pure shape-v1 decision. */
struct AdaptiveV2ShapeDecisionStructuredEvent
{
    std::uint64_t cycle_ordinal{0};
    std::string transition_artifact_id;
    ShapeDecisionRecord decision;
};

/** Canonical JSON object containing every independently scored candidate. */
std::string serialize_adaptive_v2_shape_decision_payload(
    const AdaptiveV2ShapeDecisionStructuredEvent &event,
    std::size_t maximum_bytes);

/** Canonical JSON object shared by the audit event and immutable artifact. */
std::string serialize_adaptive_v2_evidence_snapshot_payload(
    const AdaptiveV2EvidenceSnapshotStructuredEvent &event,
    std::size_t maximum_bytes);

/** Immutable manager-session terminal audit for one explicit transition. */
struct AdaptiveV2ManagerSessionTerminalStructuredEvent
{
    std::uint64_t cycle_ordinal{0};
    TreePolicyKind policy_intent{
        TreePolicyKind::performance_optimization};
    AdaptiveV2ManagerCycleOutcome outcome{
        AdaptiveV2ManagerCycleOutcome::failed};
    AdaptiveV2ManagerCycleTerminalReason reason{
        AdaptiveV2ManagerCycleTerminalReason::caller_failed};
    std::string transition_artifact_id;
    std::uint32_t predecessor_epoch_number{0};
    uint256_t predecessor_epoch_digest;
    std::optional<std::uint32_t> successor_epoch_number;
    std::optional<uint256_t> successor_epoch_digest;
    std::optional<uint256_t> command_payload_digest;
    std::optional<AdaptiveV2EpochChangeIdentity> winning_activation;
    std::uint64_t evidence_window_activation_generation{0};
    std::uint64_t baseline_evidence_cutoff{0};
    std::uint64_t current_evidence_cutoff{0};
};

using StructuredEventPayload = std::variant<
    ProcessLifecycleEvent,
    EpochLifecycleEvent,
    CommitStructuredEvent,
    CommitObservedStructuredEvent>;

using AuditStructuredEventPayload = std::variant<
    EpochCommandCommittedStructuredEvent,
    ReputationEvidenceAppliedStructuredEvent,
    AdaptiveV2ConvergenceStructuredEvent,
    AdaptiveV2EvidenceSnapshotStructuredEvent,
    AdaptiveV2ManagerSessionTerminalStructuredEvent,
    EvidenceObservationAcceptedStructuredEvent,
    AdaptiveV2ShapeDecisionStructuredEvent,
    FaultContributionOpportunityStructuredEvent,
    RootQcQueueBlockedStructuredEvent>;

enum class AdaptiveAggregationTransition : std::uint8_t
{
    configuration_active = 1,
    required_set_ready,
    initial_reserved,
    initial_enqueued,
    initial_committed,
    initial_released,
    delta_reserved,
    delta_enqueued,
    delta_committed,
    delta_released,
    delta_rejected,
    required_branch_incomplete,
    wait_exempt_absent_at_observation_deadline,
    wait_exempt_late_accepted,
    retry_exhausted,
    proposal_aborted,
    root_quorum_progress,
    root_qc_published,
};

struct RequiredBranchSignerGap
{
    // The reporter observed this direct child. Missing descendants remain a
    // separate signer gap and are never promoted to a direct fault claim.
    ReplicaID direct_child{0};
    std::vector<ReplicaID> missing_required_signers;
};

/**
 * Observational adaptive-v2 evidence. These records cannot authorize a vote,
 * change readiness, mutate reputation, or alter the fixed quorum.
 */
struct AdaptiveAggregationStructuredEvent
{
    AdaptiveAggregationTransition transition{
        AdaptiveAggregationTransition::configuration_active};
    ConfigurationId configuration;
    std::optional<uint256_t> block_hash;
    std::optional<std::uint64_t> context_generation;
    ReplicaID observer_replica{0};
    std::vector<ReplicaID> wait_exempt_signers;
    std::vector<ReplicaID> accepted_signers;
    std::vector<ReplicaID> absent_direct_children;
    std::vector<ReplicaID> missing_optional_signers;
    std::vector<RequiredBranchSignerGap> required_branch_gaps;
    std::size_t root_signer_count{0};
    std::size_t global_quorum{0};
    std::string rejection_reason;
};

enum class StructuredEventType : std::uint8_t
{
    process_started = 1,
    process_ready,
    process_stopping,
    process_stopped,
    process_forced_crash_requested,
    process_exited,
    epoch_generated,
    epoch_staged,
    epoch_acknowledged,
    epoch_activation_armed,
    epoch_activated,
    block_committed,
    adaptive_configuration_active,
    aggregation_required_set_ready,
    aggregation_initial_reserved,
    aggregation_initial_enqueued,
    aggregation_initial_committed,
    aggregation_initial_released,
    aggregation_delta_reserved,
    aggregation_delta_enqueued,
    aggregation_delta_committed,
    aggregation_delta_released,
    aggregation_delta_rejected,
    aggregation_required_branch_incomplete,
    aggregation_wait_exempt_absent,
    aggregation_wait_exempt_late_accepted,
    aggregation_retry_exhausted,
    aggregation_proposal_aborted,
    aggregation_root_quorum_progress,
    aggregation_root_qc_published,
    epoch_command_committed,
    reputation_evidence_applied,
    block_commit_observed,
    adaptive_v2_delivery_attempt,
    adaptive_v2_commit_observed,
    adaptive_v2_activation_observed,
    adaptive_v2_converged,
    adaptive_v2_ready,
    adaptive_v2_convergence_failure,
    adaptive_v2_evidence_snapshot,
    adaptive_v2_session_terminal,
    evidence_observation_accepted,
    adaptive_v2_shape_decision,
    fault_contribution_opportunity,
    pipeline_root_qc_queue_blocked,
};

StructuredEventType structured_event_type(
    const StructuredEventPayload &payload) noexcept;

StructuredEventType structured_event_type(
    const AuditStructuredEventPayload &payload) noexcept;

const char *structured_event_type_name(StructuredEventType type) noexcept;

struct StructuredEventLimits
{
    std::size_t maximum_line_bytes{64 * 1024};
    std::size_t maximum_queued_events{1024};
    std::size_t maximum_queued_bytes{4 * 1024 * 1024};
    std::size_t maximum_identity_bytes{256};
    std::size_t maximum_total_identity_bytes{5 * 256};
};

struct StructuredEventConfig
{
    std::string run_id;
    StructuredEventSource source;
    std::optional<StructuredEventSource> designated_commit_observer;
    StructuredEventLimits limits;
};

/** Exact source identity bound to a persisted resume cursor. */
struct StructuredEventSourceToken
{
    std::string run_id;
    StructuredEventSource source;
};

struct StructuredEventCursor
{
    std::uint64_t last_source_sequence{0};
    bool has_last_monotonic_ns{false};
    std::uint64_t last_monotonic_ns{0};
    std::optional<StructuredEventSourceToken> source_token;
};

enum class StructuredEventFailure : std::uint8_t
{
    none = 0,
    invalid_configuration,
    identity_too_large,
    invalid_payload,
    reentrant_call,
    allocation_failure,
    line_too_large,
    queue_full,
    clock_regression,
    sequence_exhausted,
    write_failure,
    close_failure,
    clock_failure,
    sync_failure,
};

struct StructuredEventHealth
{
    bool healthy{true};
    bool stopped{false};
    StructuredEventFailure first_failure{StructuredEventFailure::none};
    std::uint64_t last_assigned_sequence{0};
    bool has_last_monotonic_ns{false};
    std::uint64_t last_monotonic_ns{0};
    std::size_t queued_events{0};
    std::size_t queued_bytes{0};
    std::uint64_t complete_records{0};
    std::uint64_t dropped_records{0};
    bool interrupted_tail{false};
};

class StructuredEventClock
{
public:
    virtual ~StructuredEventClock() = default;
    virtual std::uint64_t now_ns() noexcept = 0;
    virtual bool healthy() const noexcept
    {
        return true;
    }
};

/** Production clock in the same CLOCK_MONOTONIC_RAW domain as run markers. */
class MonotonicRawStructuredEventClock final : public StructuredEventClock
{
public:
    std::uint64_t now_ns() noexcept override;
    bool healthy() const noexcept override;

private:
    bool healthy_{true};
};

enum class StructuredEventWriteStatus : std::uint8_t
{
    progress = 1,
    interrupted,
    failure,
};

struct StructuredEventWriteResult
{
    StructuredEventWriteStatus status{StructuredEventWriteStatus::progress};
    std::size_t bytes_written{0};
};

class StructuredEventOutput
{
public:
    virtual ~StructuredEventOutput() = default;
    virtual StructuredEventWriteResult write_some(
        const std::uint8_t *data,
        std::size_t size) noexcept = 0;
    virtual bool sync() noexcept
    {
        return true;
    }
    virtual bool close() noexcept = 0;
};

/**
 * Exclusive-create raw JSONL output.
 *
 * Existing paths are rejected rather than truncated or appended. sync() uses
 * fsync so a successful sink shutdown makes every drained record durable.
 */
class ExclusiveFileStructuredEventOutput final : public StructuredEventOutput
{
public:
    explicit ExclusiveFileStructuredEventOutput(const std::string &path);
    ~ExclusiveFileStructuredEventOutput() noexcept override;

    ExclusiveFileStructuredEventOutput(
        const ExclusiveFileStructuredEventOutput &) = delete;
    ExclusiveFileStructuredEventOutput &operator=(
        const ExclusiveFileStructuredEventOutput &) = delete;
    ExclusiveFileStructuredEventOutput(
        ExclusiveFileStructuredEventOutput &&) = delete;
    ExclusiveFileStructuredEventOutput &operator=(
        ExclusiveFileStructuredEventOutput &&) = delete;

    StructuredEventWriteResult write_some(
        const std::uint8_t *data,
        std::size_t size) noexcept override;
    bool sync() noexcept override;
    bool close() noexcept override;

    bool is_open() const noexcept;
    bool healthy() const noexcept;

private:
    int descriptor_{-1};
    bool healthy_{true};
    bool closed_{false};
    bool close_result_{true};
};

/**
 * Non-owning protocol-facing capability: bounded enqueue only.
 *
 * The emitter must not outlive its sink. Every emitter and owner call must be
 * externally serialized; callback reentry fails the sink closed.
 */
class StructuredEventEmitter
{
public:
    virtual ~StructuredEventEmitter() = default;
    virtual void emit(const StructuredEventPayload &payload) noexcept = 0;
};

/**
 * Separate protocol-facing capability preserves the closed legacy payload
 * variant while sharing the same bounded sink, schema envelope, and writer.
 */
class AdaptiveStructuredEventEmitter
{
public:
    virtual ~AdaptiveStructuredEventEmitter() = default;
    virtual void emit_adaptive(
        const AdaptiveAggregationStructuredEvent &event) noexcept = 0;
};

/** Separate capability for consensus, reputation, and convergence audit. */
class AuditStructuredEventEmitter
{
public:
    virtual ~AuditStructuredEventEmitter() = default;
    virtual void emit_audit(
        const AuditStructuredEventPayload &event) noexcept = 0;
};

/**
 * Sole externally serialized writer-owner capability.
 *
 * The borrowed clock and output must outlive this owner and sink destruction.
 * Every owner and emitter call must be externally serialized; callback
 * reentry fails the sink closed.
 */
class StructuredEventDrainOwner
{
public:
    virtual ~StructuredEventDrainOwner() = default;
    virtual void drain() noexcept = 0;
    virtual void shutdown() noexcept = 0;
    virtual StructuredEventHealth health() const noexcept = 0;
};

class StructuredEventSink final : public StructuredEventEmitter,
                                  public AdaptiveStructuredEventEmitter,
                                  public AuditStructuredEventEmitter,
                                  public StructuredEventDrainOwner
{
public:
    StructuredEventSink(
        StructuredEventConfig config,
        StructuredEventClock &clock,
        StructuredEventOutput &output,
        StructuredEventCursor cursor = {});
    ~StructuredEventSink() noexcept;

    StructuredEventSink(const StructuredEventSink &) = delete;
    StructuredEventSink &operator=(const StructuredEventSink &) = delete;
    StructuredEventSink(StructuredEventSink &&) = delete;
    StructuredEventSink &operator=(StructuredEventSink &&) = delete;

    void emit(const StructuredEventPayload &payload) noexcept override;
    void emit_adaptive(
        const AdaptiveAggregationStructuredEvent &event) noexcept override;
    void emit_audit(
        const AuditStructuredEventPayload &event) noexcept override;
    void drain() noexcept override;
    void shutdown() noexcept override;
    StructuredEventHealth health() const noexcept override;

private:
    struct State;
    std::unique_ptr<State> state_;
};

enum class StructuredEventPrefixStatus : std::uint8_t
{
    complete = 1,
    interrupted_tail,
    malformed_record,
    allocation_failure,
};

struct StructuredEventPrefixResult
{
    StructuredEventPrefixStatus status{StructuredEventPrefixStatus::complete};
    std::size_t complete_records{0};
    std::size_t complete_bytes{0};
};

StructuredEventPrefixResult parse_structured_event_prefix(
    const bytearray_t &bytes) noexcept;

} // namespace hotstuff

#endif
