#include <algorithm>
#include <array>
#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <initializer_list>
#include <limits>
#include <locale>
#include <memory>
#include <new>
#include <optional>
#include <stdexcept>
#include <string>
#include <system_error>
#include <sys/stat.h>
#include <type_traits>
#include <unistd.h>
#include <utility>
#include <variant>
#include <vector>

#include "catch.hpp"
#include "hotstuff/configuration.h"

namespace structured_event_allocation_failure
{

constexpr std::size_t disabled = std::numeric_limits<std::size_t>::max();
thread_local std::size_t allocations_before_failure = disabled;
thread_local bool injected_failure = false;

bool consume_failure() noexcept
{
    if (allocations_before_failure == disabled)
        return false;
    if (allocations_before_failure == 0)
    {
        allocations_before_failure = disabled;
        injected_failure = true;
        return true;
    }
    --allocations_before_failure;
    return false;
}

void disable() noexcept
{
    allocations_before_failure = disabled;
}

class OneShot final
{
public:
    explicit OneShot(std::size_t successful_allocations) noexcept
    {
        injected_failure = false;
        allocations_before_failure = successful_allocations;
    }

    ~OneShot()
    {
        disable();
    }

    OneShot(const OneShot &) = delete;
    OneShot &operator=(const OneShot &) = delete;

    bool triggered() const noexcept
    {
        return injected_failure;
    }
};

} // namespace structured_event_allocation_failure

void *operator new(std::size_t size)
{
    if (structured_event_allocation_failure::consume_failure())
        throw std::bad_alloc();
    if (size == 0)
        size = 1;
    if (auto *const allocation = std::malloc(size))
        return allocation;
    throw std::bad_alloc();
}

void *operator new[](std::size_t size)
{
    if (structured_event_allocation_failure::consume_failure())
        throw std::bad_alloc();
    if (size == 0)
        size = 1;
    if (auto *const allocation = std::malloc(size))
        return allocation;
    throw std::bad_alloc();
}

void *operator new(std::size_t size, const std::nothrow_t &) noexcept
{
    if (structured_event_allocation_failure::consume_failure())
        return nullptr;
    if (size == 0)
        size = 1;
    return std::malloc(size);
}

void *operator new[](std::size_t size, const std::nothrow_t &) noexcept
{
    if (structured_event_allocation_failure::consume_failure())
        return nullptr;
    if (size == 0)
        size = 1;
    return std::malloc(size);
}

void *operator new(std::size_t size, std::align_val_t alignment)
{
    if (structured_event_allocation_failure::consume_failure())
        throw std::bad_alloc();
    if (size == 0)
        size = 1;
    void *allocation = nullptr;
    if (posix_memalign(
            &allocation, static_cast<std::size_t>(alignment), size) == 0)
        return allocation;
    throw std::bad_alloc();
}

void *operator new[](std::size_t size, std::align_val_t alignment)
{
    return ::operator new(size, alignment);
}

void *operator new(std::size_t size,
                   std::align_val_t alignment,
                   const std::nothrow_t &) noexcept
{
    if (structured_event_allocation_failure::consume_failure())
        return nullptr;
    if (size == 0)
        size = 1;
    void *allocation = nullptr;
    if (posix_memalign(
            &allocation, static_cast<std::size_t>(alignment), size) != 0)
        return nullptr;
    return allocation;
}

void *operator new[](std::size_t size,
                     std::align_val_t alignment,
                     const std::nothrow_t &tag) noexcept
{
    return ::operator new(size, alignment, tag);
}

void operator delete(void *allocation) noexcept
{
    std::free(allocation);
}

void operator delete(void *allocation, std::size_t) noexcept
{
    std::free(allocation);
}

void operator delete[](void *allocation) noexcept
{
    std::free(allocation);
}

void operator delete[](void *allocation, std::size_t) noexcept
{
    std::free(allocation);
}

void operator delete(void *allocation, const std::nothrow_t &) noexcept
{
    std::free(allocation);
}

void operator delete[](void *allocation, const std::nothrow_t &) noexcept
{
    std::free(allocation);
}

void operator delete(void *allocation, std::align_val_t) noexcept
{
    std::free(allocation);
}

void operator delete[](void *allocation, std::align_val_t) noexcept
{
    std::free(allocation);
}

void operator delete(void *allocation,
                     std::size_t,
                     std::align_val_t) noexcept
{
    std::free(allocation);
}

void operator delete[](void *allocation,
                       std::size_t,
                       std::align_val_t) noexcept
{
    std::free(allocation);
}

void operator delete(void *allocation,
                     std::align_val_t,
                     const std::nothrow_t &) noexcept
{
    std::free(allocation);
}

void operator delete[](void *allocation,
                       std::align_val_t,
                       const std::nothrow_t &) noexcept
{
    std::free(allocation);
}

/*
 * V13 standalone structured-event contract
 * ----------------------------------------
 * Protocol code receives only StructuredEventEmitter: emit is a noexcept,
 * void, bounded enqueue and exposes neither health nor output progress. The
 * emitter is a non-owning view that cannot outlive its sink, and its calls are
 * externally serialized with every owner call. The externally serialized
 * owner retains StructuredEventSink, is the only caller of drain/shutdown,
 * and keeps the borrowed clock and output alive until sink destruction. This
 * The core sink has no HotStuffCore hook, background writer, or socket. Its
 * production clock and exclusive file output remain explicitly owner-created,
 * and the manager-facing audit capability remains behavior-neutral.
 * Sink construction is owner-only and may throw before publication; only emit,
 * drain, shutdown, health, output, clock, and prefix parsing are noexcept.
 *
 * Every accepted payload is closed and typed. The sink derives event_type,
 * envelope sequence, and monotonic time; callers cannot supply them. A single
 * writer emits deterministic, integer-only NDJSON. Any admission, allocation,
 * clock, or output failure is sticky. A hard error after a partial write leaves
 * at most one final truncated tail and permanently prevents later writes.
 */
#if __has_include("hotstuff/structured_event.h")
#include "hotstuff/structured_event.h"
#define KAURI_HAS_STRUCTURED_EVENT_API 1
#else
#define KAURI_HAS_STRUCTURED_EVENT_API 0

namespace hotstuff
{

constexpr std::uint32_t kStructuredEventSchemaVersion = 1;

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

struct CommitObservedStructuredEvent
{
    std::uint64_t block_height{0};
    uint256_t block_hash;
    std::optional<uint256_t> parent_hash;
    std::uint64_t transaction_count{0};
    std::uint64_t commit_batch_index{0};
};

enum class CommitIdentityUnavailableReason : std::uint8_t
{
    no_authenticated_exact_identity_source = 1,
};

struct CommitIdentityUnavailableStructuredEvent
{
    std::uint64_t block_height{0};
    uint256_t block_hash;
    std::optional<uint256_t> parent_hash;
    std::uint64_t transaction_count{0};
    std::uint64_t commit_batch_index{0};
    CommitIdentityUnavailableReason reason{
        CommitIdentityUnavailableReason::
            no_authenticated_exact_identity_source};
    bool convergence_identity_pending{false};
};

using StructuredEventPayload = std::variant<
    ProcessLifecycleEvent,
    EpochLifecycleEvent,
    CommitStructuredEvent,
    CommitObservedStructuredEvent,
    CommitIdentityUnavailableStructuredEvent>;

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
    block_commit_observed,
    block_commit_identity_unavailable,
};

StructuredEventType structured_event_type(
    const StructuredEventPayload &payload) noexcept;

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
    virtual bool close() noexcept = 0;
};

/**
 * Non-owning protocol view. It must not outlive its sink, and emit is
 * externally serialized with all emitter and owner calls.
 */
class StructuredEventEmitter
{
public:
    virtual ~StructuredEventEmitter() = default;
    virtual void emit(const StructuredEventPayload &payload) noexcept = 0;
};

/**
 * Sole sink owner. All owner and emitter calls are externally serialized, and
 * the borrowed clock and output must outlive this owner and sink destruction.
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
                                  public StructuredEventDrainOwner
{
public:
    StructuredEventSink(
        StructuredEventConfig config,
        StructuredEventClock &clock,
        StructuredEventOutput &output,
        StructuredEventCursor cursor = {});
    ~StructuredEventSink();

    StructuredEventSink(const StructuredEventSink &) = delete;
    StructuredEventSink &operator=(const StructuredEventSink &) = delete;
    StructuredEventSink(StructuredEventSink &&) = delete;
    StructuredEventSink &operator=(StructuredEventSink &&) = delete;

    void emit(const StructuredEventPayload &payload) noexcept override;
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

namespace
{

using hotstuff::AdaptiveAggregationStructuredEvent;
using hotstuff::AdaptiveAggregationTransition;
using hotstuff::AdaptiveStructuredEventEmitter;
using hotstuff::AdaptiveV2ConvergenceStructuredEvent;
using hotstuff::AdaptiveV2ConvergenceTransition;
using hotstuff::AdaptiveV2EvidenceSnapshotStructuredEvent;
using hotstuff::AdaptiveV2FaultContainmentCoverageReadyStructuredEvent;
using hotstuff::AdaptiveV2EpochChangeIdentity;
using hotstuff::AdaptiveV2ManagerCycleOutcome;
using hotstuff::AdaptiveV2ManagerCycleTerminalReason;
using hotstuff::AdaptiveV2ManagerSessionTerminalStructuredEvent;
using hotstuff::AdaptiveV2ShapeDecisionStructuredEvent;
using hotstuff::AcceptedEvidenceRecord;
using hotstuff::AuditStructuredEventEmitter;
using hotstuff::AuditStructuredEventPayload;
using hotstuff::CommitObservedStructuredEvent;
using hotstuff::CommitIdentityUnavailableReason;
using hotstuff::CommitIdentityUnavailableStructuredEvent;
using hotstuff::CommitStructuredEvent;
using hotstuff::ConfigurationId;
using hotstuff::DataStream;
using hotstuff::EpochCommandCommittedStructuredEvent;
using hotstuff::EpochLifecycleEvent;
using hotstuff::EpochLifecycleTransition;
using hotstuff::EvidenceObservationAcceptedStructuredEvent;
using hotstuff::EvidenceReputationAuditUpdate;
using hotstuff::ExperimentOmissionAction;
using hotstuff::ExperimentOmissionCohort;
using hotstuff::ExperimentReplicaRole;
using hotstuff::ExclusiveFileStructuredEventOutput;
using hotstuff::ExpectedMessageType;
using hotstuff::FaultContributionOpportunityStructuredEvent;
using hotstuff::MonotonicRawStructuredEventClock;
using hotstuff::ProcessLifecycleEvent;
using hotstuff::ProcessLifecycleState;
using hotstuff::ProposalKey;
using hotstuff::RootQcQueueBlockedStructuredEvent;
using hotstuff::RequiredBranchSignerGap;
using hotstuff::ReputationEvidenceAppliedStructuredEvent;
using hotstuff::ReplicaID;
using hotstuff::ResponseObservation;
using hotstuff::ResponseOutcome;
using hotstuff::SimpleReputationOutcome;
using hotstuff::ShapeV1CandidateRejection;
using hotstuff::ShapeV1CandidateScore;
using hotstuff::ShapeV1Status;
using hotstuff::StructuredEventClock;
using hotstuff::StructuredEventConfig;
using hotstuff::StructuredEventCursor;
using hotstuff::StructuredEventDrainOwner;
using hotstuff::StructuredEventEmitter;
using hotstuff::StructuredEventFailure;
using hotstuff::StructuredEventHealth;
using hotstuff::StructuredEventLimits;
using hotstuff::StructuredEventOutput;
using hotstuff::StructuredEventPayload;
using hotstuff::StructuredEventPrefixStatus;
using hotstuff::StructuredEventSink;
using hotstuff::StructuredEventSource;
using hotstuff::StructuredEventSourceKind;
using hotstuff::StructuredEventType;
using hotstuff::StructuredEventWriteResult;
using hotstuff::StructuredEventWriteStatus;
using hotstuff::TreePolicyKind;
using hotstuff::bytearray_t;
using hotstuff::uint256_t;

uint256_t digest(const std::string &label)
{
    return DataStream(label).get_hash();
}

ConfigurationId configuration(std::uint32_t epoch,
                              std::uint32_t tree,
                              const std::string &label)
{
    return ConfigurationId{epoch, tree, digest(label)};
}

StructuredEventConfig event_config()
{
    const StructuredEventSource source{
        StructuredEventSourceKind::replica,
        "replica-2",
        "spawn-9"};
    return StructuredEventConfig{
        "run-structured-event",
        source,
        source,
        StructuredEventLimits{}};
}

StructuredEventConfig compact_identity_config()
{
    StructuredEventConfig config{
        "rrrr",
        StructuredEventSource{
            StructuredEventSourceKind::replica, "ssss", "iiii"},
        StructuredEventSource{
            StructuredEventSourceKind::replica, "oooo", "pppp"},
        StructuredEventLimits{}};
    config.limits.maximum_identity_bytes = 4;
    config.limits.maximum_total_identity_bytes = 20;
    return config;
}

void set_identity_field(StructuredEventConfig &config,
                        std::size_t field,
                        std::string value)
{
    switch (field)
    {
        case 0:
            config.run_id = std::move(value);
            break;
        case 1:
            config.source.logical_id = std::move(value);
            break;
        case 2:
            config.source.instance_id = std::move(value);
            break;
        case 3:
            config.designated_commit_observer->logical_id = std::move(value);
            break;
        case 4:
            config.designated_commit_observer->instance_id = std::move(value);
            break;
        default:
            std::abort();
    }
}

StructuredEventPayload process_event(
    ProcessLifecycleState state = ProcessLifecycleState::started)
{
    return ProcessLifecycleEvent{state, std::nullopt};
}

CommitStructuredEvent commit_event()
{
    const auto proof_configuration = configuration(7, 3, "proof-epoch");
    return CommitStructuredEvent{
        1234,
        digest("committed-block"),
        digest("committed-parent"),
        7,
        ProposalKey{proof_configuration, digest("decision-proof-block")},
        19,
        2};
}

CommitObservedStructuredEvent commit_observed_event()
{
    return CommitObservedStructuredEvent{
        1234,
        digest("observed-committed-block"),
        digest("observed-committed-parent"),
        7,
        2};
}

CommitIdentityUnavailableStructuredEvent
commit_identity_unavailable_event()
{
    return CommitIdentityUnavailableStructuredEvent{
        1234,
        digest("unavailable-committed-block"),
        digest("unavailable-committed-parent"),
        7,
        2,
        CommitIdentityUnavailableReason::
            no_authenticated_exact_identity_source,
        false};
}

FaultContributionOpportunityStructuredEvent contribution_opportunity_event()
{
    FaultContributionOpportunityStructuredEvent event;
    event.actor = 2;
    event.proposal = ProposalKey{
        configuration(7, 3, "opportunity-epoch"),
        digest("opportunity-block")};
    event.view_generation = 19;
    event.physical_role = ExperimentReplicaRole::internal;
    event.parent_replica = 0;
    event.expected_message_type = ExpectedMessageType::aggregate_relay;
    event.cohort = ExperimentOmissionCohort::responsive_degraded;
    event.diagnostic_window = "factorial-window-1";
    event.window_start_monotonic_ns = 100;
    event.window_end_monotonic_ns = 200;
    event.decision_monotonic_ns = 150;
    event.contribution_ordinal = 81;
    event.role_contribution_ordinal = 41;
    event.scheduled_action = ExperimentOmissionAction::omit_aggregate;
    event.responsive_omission_period = 41;
    event.fault_threshold = 10;
    event.hard_actor_count = 3;
    event.responsive_degraded_actor_count = 7;
    event.fault_mode = "tiered_persistent_responsive_omission_v2";
    return event;
}

RootQcQueueBlockedStructuredEvent root_qc_queue_blocked_event()
{
    RootQcQueueBlockedStructuredEvent event;
    event.configuration = configuration(2, 0, "blocked-qc-epoch");
    event.observer_replica = 0;
    event.global_quorum = 21;
    event.queue_head_position = 0;
    event.queued_candidate_position = 1;
    event.queue_head_context_generation = 40;
    event.queued_candidate_context_generation = 41;
    event.queue_head_block_height = 100;
    event.queue_head_block_hash = digest("blocked-qc-head");
    event.queued_candidate_block_height = 101;
    event.queued_candidate_block_hash = digest("blocked-qc-candidate");
    event.queued_candidate_parent_hash = event.queue_head_block_hash;
    event.queue_head_signer_count = 20;
    event.queued_candidate_signer_count = 21;
    event.queued_candidate_qc_ready = true;
    event.queued_candidate_qc_published = false;
    return event;
}

EpochCommandCommittedStructuredEvent epoch_command_event()
{
    return EpochCommandCommittedStructuredEvent{
        2345,
        digest("epoch-command-block"),
        7,
        digest("epoch-command-predecessor"),
        8,
        digest("epoch-command-successor"),
        digest("epoch-command-payload"),
        20,
        2365};
}

ReputationEvidenceAppliedStructuredEvent reputation_event()
{
    return ReputationEvidenceAppliedStructuredEvent{
        44,
        EvidenceReputationAuditUpdate{
            42,
            digest("accepted-observation"),
            2,
            0,
            ResponseOutcome::timeout,
            SimpleReputationOutcome::timeout,
            -1,
            -3}};
}

EvidenceObservationAcceptedStructuredEvent observation_accepted_event()
{
    ResponseObservation observation;
    observation.reporter_id = 2;
    observation.observed_replica_id = 0;
    observation.configuration =
        configuration(7, 3, "accepted-observation-epoch");
    observation.block_hash =
        digest("accepted-observation-block");
    observation.expected_message_type =
        ExpectedMessageType::aggregate_relay;
    observation.outcome = ResponseOutcome::late;
    observation.response_duration_us = 150;
    observation.deadline_duration_us = 100;
    observation.reporter_monotonic_ns = 91'000;
    observation.reporter_sequence = 9;
    observation.signer_set = {0, 1};
    observation.observation_id =
        compute_response_observation_id(
            observation.attempt_identity());
    return {
        AcceptedEvidenceRecord{42, std::move(observation)}};
}

StructuredEventConfig manager_event_config()
{
    auto config = event_config();
    config.source = StructuredEventSource{
        StructuredEventSourceKind::adaptation_manager,
        "adaptive-manager",
        "manager-spawn-4"};
    config.designated_commit_observer.reset();
    return config;
}

AdaptiveV2EpochChangeIdentity convergence_event_identity()
{
    return {
        7,
        digest("convergence-audit-predecessor"),
        8,
        digest("convergence-audit-successor"),
        digest("convergence-audit-command-payload"),
        2345,
        digest("convergence-audit-command-block"),
        20,
        2365};
}

AdaptiveV2EvidenceSnapshotStructuredEvent evidence_snapshot_event()
{
    AdaptiveV2EvidenceSnapshotStructuredEvent event;
    event.cycle_ordinal = 3;
    event.policy_intent = TreePolicyKind::fault_containment;
    event.transition_artifact_id = "e7-to-e8-containment";
    event.predecessor_epoch_number = 7;
    event.predecessor_epoch_digest =
        digest("convergence-audit-predecessor");
    event.activation_generation =
        (std::uint64_t{7} << 32) | std::uint64_t{17};
    event.baseline_cutoff = 44;
    event.current_cutoff = 52;
    event.full_prefix_snapshot_id =
        digest("full-prefix-evidence-snapshot");
    event.evidence_snapshot_id = digest("selected-evidence-snapshot");
    event.accepted_prefix_count = 2;
    event.eligible_ranking = {2, 3, 4, 5, 6};
    return event;
}

AdaptiveV2FaultContainmentCoverageReadyStructuredEvent
fault_containment_coverage_ready_event()
{
    AdaptiveV2FaultContainmentCoverageReadyStructuredEvent event;
    event.cycle_ordinal = 0;
    event.transition_artifact_id = "e0-to-e1-containment";
    event.predecessor_epoch_number = 0;
    event.predecessor_epoch_digest =
        digest("fault-containment-coverage-epoch");
    event.fault_evidence_start_monotonic_ns = 900'000;
    event.evidence_cutoff = 313;
    event.required_tree_ids = {3, 7, 12, 29, 30};
    event.observed_tree_ids = {3, 7, 12, 29, 30};
    return event;
}

AdaptiveV2ShapeDecisionStructuredEvent shape_decision_event()
{
    hotstuff::ShapeDecisionRecord decision;
    decision.status = ShapeV1Status::selected;
    decision.selector_version = hotstuff::kShapeV1SelectorVersion;
    decision.tie_rule = hotstuff::kShapeV1TieRule;
    decision.epoch_number = 7;
    decision.epoch_digest = digest("shape-decision-epoch");
    decision.current_topology_digest =
        digest("shape-decision-topology");
    decision.evidence_cutoff = 52;
    decision.evidence_digest = digest("shape-decision-evidence");
    decision.predecessor_tree_count = 7;
    decision.tree_count = 5;
    decision.fixed_pipeline_stretch = 2;
    decision.deterministic_seed = 41'719;
    decision.current_fanout = 2;
    decision.selected_fanout = 5;
    decision.applied_fanout = 5;
    decision.reference_tree_rule =
        hotstuff::kShapeV1ReferenceTreeRule;
    decision.candidates = {
        ShapeV1CandidateScore{
            2, ShapeV1CandidateRejection::none,
            2, 900'000, 800, 0, false},
        ShapeV1CandidateScore{
            3, ShapeV1CandidateRejection::missing_influential_evidence,
            2, 0, 0, 0, false},
        ShapeV1CandidateScore{
            5, ShapeV1CandidateRejection::none,
            2, 600'000, 700, 20, true}};
    decision.decision_digest =
        hotstuff::compute_shape_decision_digest(decision);
    REQUIRE(hotstuff::valid_shape_decision_record(decision));
    return AdaptiveV2ShapeDecisionStructuredEvent{
        3, "e7-to-e8-shape", std::move(decision)};
}

AdaptiveV2ManagerSessionTerminalStructuredEvent manager_terminal_event()
{
    const auto identity = convergence_event_identity();
    AdaptiveV2ManagerSessionTerminalStructuredEvent event;
    event.cycle_ordinal = 3;
    event.policy_intent = TreePolicyKind::fault_containment;
    event.outcome = AdaptiveV2ManagerCycleOutcome::advanced;
    event.reason =
        AdaptiveV2ManagerCycleTerminalReason::successor_converged;
    event.transition_artifact_id = "e7-to-e8-containment";
    event.predecessor_epoch_number =
        identity.predecessor_epoch_number;
    event.predecessor_epoch_digest =
        identity.predecessor_epoch_digest;
    event.successor_epoch_number = identity.successor_epoch_number;
    event.successor_epoch_digest = identity.successor_epoch_digest;
    event.command_payload_digest = identity.command_payload_digest;
    event.winning_activation = identity;
    event.evidence_window_activation_generation =
        (std::uint64_t{7} << 32) | std::uint64_t{17};
    event.baseline_evidence_cutoff = 44;
    event.current_evidence_cutoff = 52;
    return event;
}

template<typename Event, typename = void>
struct has_convergence_disposition : std::false_type
{};

template<typename Event>
struct has_convergence_disposition<
    Event,
    std::void_t<decltype(std::declval<Event &>().disposition)>>
    : std::true_type
{};

template<typename Event>
Event convergence_event(
    AdaptiveV2ConvergenceTransition transition,
    std::string disposition = {})
{
    Event event;
    event.transition = transition;
    event.required_activation_count = 5;
    event.disposition = std::move(disposition);
    switch (transition)
    {
    case AdaptiveV2ConvergenceTransition::delivery_attempt:
        event.replica_id = 4;
        event.delivery_attempt = 3;
        event.canonical_payload_digest =
            digest("convergence-audit-canonical-payload");
        break;
    case AdaptiveV2ConvergenceTransition::commit_observed:
        event.replica_id = 4;
        event.identity = convergence_event_identity();
        event.accepted_commit_count = 1;
        event.canonical_payload_digest =
            digest("convergence-audit-canonical-payload");
        break;
    case AdaptiveV2ConvergenceTransition::activation_observed:
        event.replica_id = 4;
        event.identity = convergence_event_identity();
        event.accepted_commit_count = 1;
        event.accepted_activation_count = 1;
        event.canonical_payload_digest =
            digest("convergence-audit-canonical-payload");
        break;
    case AdaptiveV2ConvergenceTransition::converged:
    case AdaptiveV2ConvergenceTransition::ready:
        event.identity = convergence_event_identity();
        event.accepted_commit_count = 3;
        event.accepted_activation_count = 5;
        break;
    case AdaptiveV2ConvergenceTransition::failure:
        event.accepted_commit_count = 2;
        event.accepted_activation_count = 4;
        event.failure_reason = "activation_deadline_exceeded";
        break;
    }
    return event;
}

AdaptiveAggregationStructuredEvent adaptive_event(
    AdaptiveAggregationTransition transition =
        AdaptiveAggregationTransition::required_set_ready)
{
    AdaptiveAggregationStructuredEvent event;
    event.transition = transition;
    event.configuration = configuration(9, 2, "adaptive-epoch");
    event.block_hash = digest("adaptive-block");
    event.context_generation = 17;
    event.observer_replica = 4;
    event.wait_exempt_signers = {0, 1};
    event.accepted_signers = {2, 3, 4};
    event.absent_direct_children = {0, 1};
    event.missing_optional_signers = {0, 1};
    event.required_branch_gaps = {
        RequiredBranchSignerGap{2, {5, 6}},
        RequiredBranchSignerGap{3, {7}}};
    event.root_signer_count = event.accepted_signers.size();
    event.global_quorum = 5;

    if (transition == AdaptiveAggregationTransition::configuration_active)
    {
        event.block_hash.reset();
        event.context_generation.reset();
    }
    if (transition == AdaptiveAggregationTransition::delta_rejected ||
        transition == AdaptiveAggregationTransition::proposal_aborted)
        event.rejection_reason = "rejected";
    if (transition == AdaptiveAggregationTransition::root_qc_published)
        event.global_quorum = event.root_signer_count;
    return event;
}

class FakeClock final : public StructuredEventClock
{
public:
    explicit FakeClock(std::vector<std::uint64_t> values)
        : values_(std::move(values))
    {
    }

    std::uint64_t now_ns() noexcept override
    {
        ++calls_;
        if (values_.empty())
            return 0;
        const auto index = next_ < values_.size()
            ? next_++
            : values_.size() - 1;
        return values_[index];
    }

    std::size_t calls() const noexcept
    {
        return calls_;
    }

private:
    std::vector<std::uint64_t> values_;
    std::size_t next_{0};
    std::size_t calls_{0};
};

class FailingClock final : public StructuredEventClock
{
public:
    std::uint64_t now_ns() noexcept override
    {
        ++calls_;
        return 9002;
    }

    bool healthy() const noexcept override
    {
        return false;
    }

    std::size_t calls() const noexcept
    {
        return calls_;
    }

private:
    std::size_t calls_{0};
};

class ReentrantProducerClock final : public StructuredEventClock
{
public:
    explicit ReentrantProducerClock(std::vector<std::uint64_t> values)
        : values_(std::move(values))
    {
    }

    void arm(StructuredEventEmitter &producer,
             const StructuredEventPayload &payload) noexcept
    {
        producer_ = &producer;
        payload_ = &payload;
    }

    std::uint64_t now_ns() noexcept override
    {
        ++calls_;
        const auto index = next_ < values_.size()
            ? next_++
            : values_.empty() ? 0 : values_.size() - 1;
        const auto value = values_.empty() ? 0 : values_[index];
        if (!reentered_ && producer_ != nullptr && payload_ != nullptr)
        {
            reentered_ = true;
            producer_->emit(*payload_);
        }
        return value;
    }

    std::size_t calls() const noexcept
    {
        return calls_;
    }

    bool reentered() const noexcept
    {
        return reentered_;
    }

private:
    std::vector<std::uint64_t> values_;
    StructuredEventEmitter *producer_{nullptr};
    const StructuredEventPayload *payload_{nullptr};
    std::size_t next_{0};
    std::size_t calls_{0};
    bool reentered_{false};
};

struct WriteAction
{
    StructuredEventWriteStatus status{StructuredEventWriteStatus::progress};
    std::size_t maximum_bytes{std::numeric_limits<std::size_t>::max()};
};

class MemoryOutput final : public StructuredEventOutput
{
public:
    explicit MemoryOutput(
        std::vector<WriteAction> actions = {},
        bool close_result = true,
        bool sync_result = true)
        : actions_(std::move(actions)),
          close_result_(close_result),
          sync_result_(sync_result)
    {
    }

    StructuredEventWriteResult write_some(
        const std::uint8_t *data,
        std::size_t size) noexcept override
    {
        ++write_calls_;
        WriteAction action;
        if (next_action_ < actions_.size())
            action = actions_[next_action_++];

        if (action.status == StructuredEventWriteStatus::interrupted)
            return {StructuredEventWriteStatus::interrupted, 0};
        if (action.status == StructuredEventWriteStatus::failure)
            return {StructuredEventWriteStatus::failure, 0};

        const auto written = std::min(size, action.maximum_bytes);
        try
        {
            bytes_.insert(bytes_.end(), data, data + written);
        }
        catch (...)
        {
            return {StructuredEventWriteStatus::failure, 0};
        }
        return {StructuredEventWriteStatus::progress, written};
    }

    bool sync() noexcept override
    {
        ++sync_calls_;
        return sync_result_;
    }

    bool close() noexcept override
    {
        ++close_calls_;
        return close_result_;
    }

    const bytearray_t &bytes() const noexcept
    {
        return bytes_;
    }

    std::size_t write_calls() const noexcept
    {
        return write_calls_;
    }

    std::size_t close_calls() const noexcept
    {
        return close_calls_;
    }

    std::size_t sync_calls() const noexcept
    {
        return sync_calls_;
    }

    void reserve(std::size_t bytes)
    {
        bytes_.reserve(bytes);
    }

private:
    std::vector<WriteAction> actions_;
    bool close_result_{true};
    bool sync_result_{true};
    bytearray_t bytes_;
    std::size_t next_action_{0};
    std::size_t write_calls_{0};
    std::size_t sync_calls_{0};
    std::size_t close_calls_{0};
};

class ReentrantOwnerOutput final : public StructuredEventOutput
{
public:
    void arm(StructuredEventDrainOwner &owner) noexcept
    {
        owner_ = &owner;
    }

    StructuredEventWriteResult write_some(
        const std::uint8_t *data,
        std::size_t size) noexcept override
    {
        ++write_calls_;
        if (!reentered_ && owner_ != nullptr)
        {
            reentered_ = true;
            owner_->drain();
            return {StructuredEventWriteStatus::failure, 0};
        }
        return output_.write_some(data, size);
    }

    bool close() noexcept override
    {
        return output_.close();
    }

    const bytearray_t &bytes() const noexcept
    {
        return output_.bytes();
    }

    std::size_t close_calls() const noexcept
    {
        return output_.close_calls();
    }

    std::size_t write_calls() const noexcept
    {
        return write_calls_;
    }

    bool reentered() const noexcept
    {
        return reentered_;
    }

private:
    MemoryOutput output_;
    StructuredEventDrainOwner *owner_{nullptr};
    std::size_t write_calls_{0};
    bool reentered_{false};
};

std::string rendered(const MemoryOutput &output)
{
    return std::string(output.bytes().begin(), output.bytes().end());
}

std::size_t count_occurrences(
    const std::string &contents,
    const std::string &needle)
{
    std::size_t count = 0;
    for (std::size_t cursor = 0;
         (cursor = contents.find(needle, cursor)) != std::string::npos;
         cursor += needle.size())
        ++count;
    return count;
}

bytearray_t bytes_of(const std::string &text)
{
    return bytearray_t(text.begin(), text.end());
}

std::string raw_bytes(std::initializer_list<std::uint8_t> bytes)
{
    std::string result;
    result.reserve(bytes.size());
    for (const auto byte : bytes)
        result.push_back(static_cast<char>(byte));
    return result;
}

std::vector<std::string> complete_lines(const bytearray_t &bytes)
{
    std::vector<std::string> lines;
    std::size_t begin = 0;
    for (std::size_t index = 0; index < bytes.size(); ++index)
    {
        if (bytes[index] != static_cast<std::uint8_t>('\n'))
            continue;
        lines.emplace_back(
            bytes.begin() + static_cast<std::ptrdiff_t>(begin),
            bytes.begin() + static_cast<std::ptrdiff_t>(index + 1));
        begin = index + 1;
    }
    return lines;
}

bool same_health(const StructuredEventHealth &left,
                 const StructuredEventHealth &right) noexcept
{
    return left.healthy == right.healthy &&
           left.stopped == right.stopped &&
           left.first_failure == right.first_failure &&
           left.last_assigned_sequence == right.last_assigned_sequence &&
           left.has_last_monotonic_ns == right.has_last_monotonic_ns &&
           left.last_monotonic_ns == right.last_monotonic_ns &&
           left.queued_events == right.queued_events &&
           left.queued_bytes == right.queued_bytes &&
           left.complete_records == right.complete_records &&
           left.dropped_records == right.dropped_records &&
           left.interrupted_tail == right.interrupted_tail;
}

template<typename T, typename = void>
struct has_drain : std::false_type
{};

template<typename T>
struct has_drain<T, std::void_t<decltype(std::declval<T &>().drain())>>
    : std::true_type
{};

template<typename T, typename = void>
struct has_health : std::false_type
{};

template<typename T>
struct has_health<T, std::void_t<decltype(std::declval<const T &>().health())>>
    : std::true_type
{};

template<typename T, typename = void>
struct has_emit : std::false_type
{};

template<typename T>
struct has_emit<T, std::void_t<decltype(std::declval<T &>().emit(
                       std::declval<const StructuredEventPayload &>()))>>
    : std::true_type
{};

template<typename Cursor>
using cursor_source_token_optional_t = std::decay_t<decltype(
    std::declval<Cursor &>().source_token)>;

template<typename Cursor>
using cursor_source_token_t =
    typename cursor_source_token_optional_t<Cursor>::value_type;

template<typename Cursor, typename = void>
struct CursorSourceTokenContract
{
    static constexpr bool available = false;

    static void bind(Cursor &, const StructuredEventConfig &) noexcept
    {
    }
};

template<typename Cursor>
struct CursorSourceTokenContract<
    Cursor,
    std::void_t<
        decltype(std::declval<Cursor &>().source_token),
        typename cursor_source_token_optional_t<Cursor>::value_type,
        decltype(std::declval<cursor_source_token_t<Cursor> &>().run_id),
        decltype(std::declval<cursor_source_token_t<Cursor> &>().source.kind),
        decltype(
            std::declval<cursor_source_token_t<Cursor> &>().source.logical_id),
        decltype(
            std::declval<cursor_source_token_t<Cursor> &>().source.instance_id)>>
{
    static constexpr bool available = true;

    static void bind(Cursor &cursor, const StructuredEventConfig &config)
    {
        cursor_source_token_t<Cursor> token;
        token.run_id = config.run_id;
        token.source = config.source;
        cursor.source_token = std::move(token);
    }
};

template<typename Failure>
auto invalid_payload_failure(int) noexcept
    -> decltype(Failure::invalid_payload,
                std::optional<Failure>{Failure::invalid_payload})
{
    return Failure::invalid_payload;
}

template<typename Failure>
std::optional<Failure> invalid_payload_failure(long) noexcept
{
    return std::nullopt;
}

template<typename Failure>
auto reentrant_call_failure(int) noexcept
    -> decltype(Failure::reentrant_call,
                std::optional<Failure>{Failure::reentrant_call})
{
    return Failure::reentrant_call;
}

template<typename Failure>
std::optional<Failure> reentrant_call_failure(long) noexcept
{
    return std::nullopt;
}

class GroupedNumbers final : public std::numpunct<char>
{
protected:
    char do_thousands_sep() const override
    {
        return '_';
    }

    std::string do_grouping() const override
    {
        return "\3";
    }
};

class GlobalLocale final
{
public:
    explicit GlobalLocale(const std::locale &replacement)
        : previous_(std::locale::global(replacement))
    {
    }

    ~GlobalLocale()
    {
        try
        {
            std::locale::global(previous_);
        }
        catch (...)
        {
        }
    }

private:
    std::locale previous_;
};

std::string expected_commit_line(const CommitStructuredEvent &event,
                                 std::uint64_t sequence,
                                 std::uint64_t monotonic_ns,
                                 bool designated_observer = true)
{
    const auto &proof = event.decision_proof;
    return
        "{\"event_schema_version\":1,"
        "\"run_id\":\"run-\\\"\\\\\\n\\t\\u0001\","
        "\"source_kind\":\"replica\","
        "\"source_id\":\"replica-\\\"\\\\\\r\\b\\f\","
        "\"source_instance\":\"spawn-\\n\\t\\u0002\","
        "\"source_sequence\":" + std::to_string(sequence) + ","
        "\"source_monotonic_ns\":" + std::to_string(monotonic_ns) + ","
        "\"event_type\":\"block.committed\","
        "\"payload\":{"
        "\"block_height\":" + std::to_string(event.block_height) + ","
        "\"block_hash\":\"" + event.block_hash.to_hex() + "\","
        "\"parent_hash\":\"" + event.parent_hash->to_hex() + "\","
        "\"transaction_count\":" +
            std::to_string(event.transaction_count) + ","
        "\"designated_observer\":" +
            std::string(designated_observer ? "true" : "false") + ","
        "\"decision_proof\":{"
        "\"epoch_number\":" +
            std::to_string(proof.configuration.epoch_number) + ","
        "\"tree_id\":" +
            std::to_string(proof.configuration.tree_id) + ","
        "\"epoch_digest\":\"" +
            proof.configuration.epoch_digest.to_hex() + "\","
        "\"block_hash\":\"" + proof.block_hash.to_hex() + "\"},"
        "\"view_generation\":" +
            std::to_string(*event.view_generation) + ","
        "\"commit_batch_index\":" +
            std::to_string(event.commit_batch_index) + "}}\n";
}

std::string expected_commit_observed_line(
    const CommitObservedStructuredEvent &event,
    std::uint64_t sequence,
    std::uint64_t monotonic_ns)
{
    return
        "{\"event_schema_version\":1,"
        "\"run_id\":\"run-structured-event\","
        "\"source_kind\":\"replica\","
        "\"source_id\":\"replica-2\","
        "\"source_instance\":\"spawn-9\","
        "\"source_sequence\":" + std::to_string(sequence) + ","
        "\"source_monotonic_ns\":" + std::to_string(monotonic_ns) + ","
        "\"event_type\":\"block.commit_observed\","
        "\"payload\":{"
        "\"block_height\":" + std::to_string(event.block_height) + ","
        "\"block_hash\":\"" + event.block_hash.to_hex() + "\","
        "\"parent_hash\":\"" + event.parent_hash->to_hex() + "\","
        "\"transaction_count\":" +
            std::to_string(event.transaction_count) + ","
        "\"commit_batch_index\":" +
        std::to_string(event.commit_batch_index) + "}}\n";
}

std::string expected_commit_identity_unavailable_line(
    const CommitIdentityUnavailableStructuredEvent &event,
    std::uint64_t sequence,
    std::uint64_t monotonic_ns)
{
    return
        "{\"event_schema_version\":1,"
        "\"run_id\":\"run-structured-event\","
        "\"source_kind\":\"replica\","
        "\"source_id\":\"replica-2\","
        "\"source_instance\":\"spawn-9\","
        "\"source_sequence\":" + std::to_string(sequence) + ","
        "\"source_monotonic_ns\":" + std::to_string(monotonic_ns) + ","
        "\"event_type\":\"block.commit_identity_unavailable\","
        "\"payload\":{"
        "\"block_height\":" + std::to_string(event.block_height) + ","
        "\"block_hash\":\"" + event.block_hash.to_hex() + "\","
        "\"parent_hash\":\"" + event.parent_hash->to_hex() + "\","
        "\"transaction_count\":" +
            std::to_string(event.transaction_count) + ","
        "\"commit_batch_index\":" +
            std::to_string(event.commit_batch_index) + ","
        "\"reason\":\"no_authenticated_exact_identity_source\","
        "\"convergence_identity_pending\":false}}\n";
}

class TemporaryDirectory final
{
public:
    TemporaryDirectory()
    {
        constexpr char path_template[] =
            "/tmp/kauri-structured-event-XXXXXX";
        std::array<char, sizeof(path_template)> path_buffer{};
        std::copy(
            std::begin(path_template),
            std::end(path_template),
            path_buffer.begin());
        const auto *const created = ::mkdtemp(path_buffer.data());
        if (created == nullptr)
            throw std::system_error(errno, std::generic_category());
        path_ = created;
    }

    ~TemporaryDirectory()
    {
        for (const auto &file : files_)
            ::unlink(file.c_str());
        ::rmdir(path_.c_str());
    }

    TemporaryDirectory(const TemporaryDirectory &) = delete;
    TemporaryDirectory &operator=(const TemporaryDirectory &) = delete;

    std::string file(const std::string &name)
    {
        const auto value = path_ + "/" + name;
        files_.push_back(value);
        return value;
    }

private:
    std::string path_;
    std::vector<std::string> files_;
};

class ScopedUmask final
{
public:
    explicit ScopedUmask(mode_t value) noexcept
        : previous_(::umask(value))
    {
    }

    ~ScopedUmask()
    {
        ::umask(previous_);
    }

    ScopedUmask(const ScopedUmask &) = delete;
    ScopedUmask &operator=(const ScopedUmask &) = delete;

private:
    mode_t previous_;
};

bytearray_t read_file(const std::string &path)
{
    const auto descriptor = ::open(path.c_str(), O_RDONLY);
    if (descriptor < 0)
        throw std::system_error(errno, std::generic_category());

    bytearray_t result;
    std::array<std::uint8_t, 4096> buffer{};
    while (true)
    {
        const auto count = ::read(
            descriptor, buffer.data(), buffer.size());
        if (count > 0)
        {
            result.insert(
                result.end(), buffer.begin(), buffer.begin() + count);
            continue;
        }
        if (count == 0)
            break;
        if (errno == EINTR)
            continue;
        const auto error = errno;
        ::close(descriptor);
        throw std::system_error(error, std::generic_category());
    }
    if (::close(descriptor) != 0)
        throw std::system_error(errno, std::generic_category());
    return result;
}

} // namespace

TEST_CASE("V13 exposes a closed payload-only protocol emitter",
          "[v13][structured-event][contract][intentional-red]")
{
    CHECK(KAURI_HAS_STRUCTURED_EVENT_API == 1);
    CHECK(hotstuff::kStructuredEventSchemaVersion == 1);

    static_assert(std::variant_size<StructuredEventPayload>::value == 5,
                  "protocol evidence has lifecycle and three commit payloads");
    static_assert(std::is_final<StructuredEventSink>::value,
                  "one owner controls the queue and output path");
    static_assert(!std::is_copy_constructible<StructuredEventSink>::value,
                  "copying would fork sequence and writer ownership");
    static_assert(!std::is_move_constructible<StructuredEventSink>::value,
                  "moving would invalidate borrowed clock and output");
    static_assert(!std::is_nothrow_constructible<
                      StructuredEventSink,
                      StructuredEventConfig,
                      StructuredEventClock &,
                      StructuredEventOutput &,
                      StructuredEventCursor>::value,
                  "throwing owner construction is outside the protocol path");
    static_assert(!has_drain<StructuredEventEmitter>::value,
                  "protocol callers cannot drive the writer");
    static_assert(!has_health<StructuredEventEmitter>::value,
                  "protocol callers cannot branch on evidence health");
    static_assert(has_drain<StructuredEventDrainOwner>::value,
                  "the serialized writer owner controls output progress");
    static_assert(has_health<StructuredEventDrainOwner>::value,
                  "only the writer owner can inspect measurement health");
    static_assert(!has_emit<StructuredEventDrainOwner>::value,
                  "the writer capability cannot manufacture protocol events");

    using Emit = void (StructuredEventEmitter::*)(
        const StructuredEventPayload &) noexcept;
    static_assert(
        std::is_same<decltype(&StructuredEventEmitter::emit), Emit>::value,
        "emit is noexcept void and cannot influence protocol control flow");

    const StructuredEventLimits defaults;
    CHECK(defaults.maximum_line_bytes == 64 * 1024);
    CHECK(defaults.maximum_queued_events == 1024);
    CHECK(defaults.maximum_queued_bytes == 4 * 1024 * 1024);
    CHECK(defaults.maximum_identity_bytes == 256);
    CHECK(defaults.maximum_total_identity_bytes == 5 * 256);

    struct ProcessMapping
    {
        ProcessLifecycleState state;
        StructuredEventType type;
        const char *name;
    };
    const std::array<ProcessMapping, 6> process_mappings{{
        {ProcessLifecycleState::started,
         StructuredEventType::process_started,
         "process.started"},
        {ProcessLifecycleState::ready,
         StructuredEventType::process_ready,
         "process.ready"},
        {ProcessLifecycleState::stopping,
         StructuredEventType::process_stopping,
         "process.stopping"},
        {ProcessLifecycleState::stopped,
         StructuredEventType::process_stopped,
         "process.stopped"},
        {ProcessLifecycleState::forced_crash_requested,
         StructuredEventType::process_forced_crash_requested,
         "process.forced_crash_requested"},
        {ProcessLifecycleState::exited,
         StructuredEventType::process_exited,
         "process.exited"},
    }};

    std::vector<StructuredEventType> observed_types;
    std::vector<std::string> observed_names;
    for (const auto &mapping : process_mappings)
    {
        CAPTURE(mapping.name);
        const auto type = hotstuff::structured_event_type(
            process_event(mapping.state));
        CHECK(type == mapping.type);
        const std::string name{hotstuff::structured_event_type_name(type)};
        CHECK(name == mapping.name);
        CHECK(std::find(observed_types.begin(), observed_types.end(), type) ==
              observed_types.end());
        CHECK(std::find(observed_names.begin(), observed_names.end(), name) ==
              observed_names.end());
        observed_types.push_back(type);
        observed_names.push_back(name);
    }

    struct EpochMapping
    {
        EpochLifecycleTransition transition;
        StructuredEventType type;
        const char *name;
    };
    const std::array<EpochMapping, 5> epoch_mappings{{
        {EpochLifecycleTransition::generated,
         StructuredEventType::epoch_generated,
         "epoch.generated"},
        {EpochLifecycleTransition::staged,
         StructuredEventType::epoch_staged,
         "epoch.staged"},
        {EpochLifecycleTransition::acknowledged,
         StructuredEventType::epoch_acknowledged,
         "epoch.acknowledged"},
        {EpochLifecycleTransition::activation_armed,
         StructuredEventType::epoch_activation_armed,
         "epoch.activation_armed"},
        {EpochLifecycleTransition::activated,
         StructuredEventType::epoch_activated,
         "epoch.activated"},
    }};
    const auto epoch = configuration(8, 4, "epoch-event");
    for (const auto &mapping : epoch_mappings)
    {
        CAPTURE(mapping.name);
        const auto type = hotstuff::structured_event_type(
            StructuredEventPayload{EpochLifecycleEvent{
                mapping.transition, epoch, 400}});
        CHECK(type == mapping.type);
        const std::string name{hotstuff::structured_event_type_name(type)};
        CHECK(name == mapping.name);
        CHECK(std::find(observed_types.begin(), observed_types.end(), type) ==
              observed_types.end());
        CHECK(std::find(observed_names.begin(), observed_names.end(), name) ==
              observed_names.end());
        observed_types.push_back(type);
        observed_names.push_back(name);
    }

    const auto commit_type = hotstuff::structured_event_type(
        StructuredEventPayload{commit_event()});
    CHECK(commit_type == StructuredEventType::block_committed);
    CHECK(std::string(hotstuff::structured_event_type_name(commit_type)) ==
          "block.committed");
    CHECK(std::find(
              observed_types.begin(), observed_types.end(), commit_type) ==
          observed_types.end());
    observed_types.push_back(commit_type);
    observed_names.emplace_back("block.committed");

    const auto observed_type = hotstuff::structured_event_type(
        StructuredEventPayload{commit_observed_event()});
    CHECK(observed_type == StructuredEventType::block_commit_observed);
    CHECK(std::string(hotstuff::structured_event_type_name(observed_type)) ==
          "block.commit_observed");
    CHECK(std::find(
              observed_types.begin(), observed_types.end(), observed_type) ==
          observed_types.end());
    CHECK(std::find(
              observed_names.begin(),
              observed_names.end(),
              "block.commit_observed") == observed_names.end());
    observed_types.push_back(observed_type);
    observed_names.emplace_back("block.commit_observed");

    const auto unavailable_type = hotstuff::structured_event_type(
        StructuredEventPayload{commit_identity_unavailable_event()});
    CHECK(unavailable_type ==
          StructuredEventType::block_commit_identity_unavailable);
    CHECK(std::string(
              hotstuff::structured_event_type_name(unavailable_type)) ==
          "block.commit_identity_unavailable");
    CHECK(std::find(
              observed_types.begin(), observed_types.end(), unavailable_type) ==
          observed_types.end());
    CHECK(std::find(
              observed_names.begin(),
              observed_names.end(),
              "block.commit_identity_unavailable") == observed_names.end());

#if defined(HOTSTUFF_PROTO_LOG)
    INFO("the same structured contract is exercised with human logs enabled");
#else
    INFO("the structured contract remains active with human logs disabled");
#endif
}

TEST_CASE("WE06-C04 maps every adaptive transition to one canonical event",
          "[we06][c04][structured-event][adaptive][schema]")
{
    using AdaptiveEmit = void (AdaptiveStructuredEventEmitter::*)(
        const AdaptiveAggregationStructuredEvent &) noexcept;
    static_assert(
        std::is_same<
            decltype(&AdaptiveStructuredEventEmitter::emit_adaptive),
            AdaptiveEmit>::value,
        "adaptive emission is behavior-neutral noexcept evidence output");
    static_assert(
        std::is_base_of<
            AdaptiveStructuredEventEmitter,
            StructuredEventSink>::value,
        "the bounded sink implements the separate adaptive capability");
    static_assert(
        std::variant_size<StructuredEventPayload>::value == 5,
        "adaptive aggregation evidence stays outside protocol payloads");

    struct Mapping
    {
        AdaptiveAggregationTransition transition;
        StructuredEventType type;
        const char *name;
    };
    const std::array<Mapping, 18> mappings{{
        {AdaptiveAggregationTransition::configuration_active,
         StructuredEventType::adaptive_configuration_active,
         "adaptive.configuration_active"},
        {AdaptiveAggregationTransition::required_set_ready,
         StructuredEventType::aggregation_required_set_ready,
         "aggregation.required_set_ready"},
        {AdaptiveAggregationTransition::initial_reserved,
         StructuredEventType::aggregation_initial_reserved,
         "aggregation.initial_reserved"},
        {AdaptiveAggregationTransition::initial_enqueued,
         StructuredEventType::aggregation_initial_enqueued,
         "aggregation.initial_enqueued"},
        {AdaptiveAggregationTransition::initial_committed,
         StructuredEventType::aggregation_initial_committed,
         "aggregation.initial_committed"},
        {AdaptiveAggregationTransition::initial_released,
         StructuredEventType::aggregation_initial_released,
         "aggregation.initial_released"},
        {AdaptiveAggregationTransition::delta_reserved,
         StructuredEventType::aggregation_delta_reserved,
         "aggregation.delta_reserved"},
        {AdaptiveAggregationTransition::delta_enqueued,
         StructuredEventType::aggregation_delta_enqueued,
         "aggregation.delta_enqueued"},
        {AdaptiveAggregationTransition::delta_committed,
         StructuredEventType::aggregation_delta_committed,
         "aggregation.delta_committed"},
        {AdaptiveAggregationTransition::delta_released,
         StructuredEventType::aggregation_delta_released,
         "aggregation.delta_released"},
        {AdaptiveAggregationTransition::delta_rejected,
         StructuredEventType::aggregation_delta_rejected,
         "aggregation.delta_rejected"},
        {AdaptiveAggregationTransition::required_branch_incomplete,
         StructuredEventType::aggregation_required_branch_incomplete,
         "aggregation.required_branch_incomplete"},
        {AdaptiveAggregationTransition::
             wait_exempt_absent_at_observation_deadline,
         StructuredEventType::aggregation_wait_exempt_absent,
         "aggregation.wait_exempt_absent_at_observation_deadline"},
        {AdaptiveAggregationTransition::wait_exempt_late_accepted,
         StructuredEventType::aggregation_wait_exempt_late_accepted,
         "aggregation.wait_exempt_late_accepted"},
        {AdaptiveAggregationTransition::retry_exhausted,
         StructuredEventType::aggregation_retry_exhausted,
         "aggregation.retry_exhausted"},
        {AdaptiveAggregationTransition::proposal_aborted,
         StructuredEventType::aggregation_proposal_aborted,
         "aggregation.proposal_aborted"},
        {AdaptiveAggregationTransition::root_quorum_progress,
         StructuredEventType::aggregation_root_quorum_progress,
         "aggregation.root_quorum_progress"},
        {AdaptiveAggregationTransition::root_qc_published,
         StructuredEventType::aggregation_root_qc_published,
         "aggregation.root_qc_published"},
    }};

    std::vector<StructuredEventType> observed_types;
    std::vector<std::string> observed_names;
    for (const auto &mapping : mappings)
    {
        CAPTURE(mapping.name);
        CHECK(std::string(
                  hotstuff::structured_event_type_name(mapping.type)) ==
              mapping.name);
        CHECK(std::find(
                  observed_types.begin(), observed_types.end(),
                  mapping.type) == observed_types.end());
        CHECK(std::find(
                  observed_names.begin(), observed_names.end(),
                  mapping.name) == observed_names.end());

        FakeClock clock({1000});
        MemoryOutput output;
        StructuredEventSink sink(event_config(), clock, output);
        AdaptiveStructuredEventEmitter &protocol = sink;
        protocol.emit_adaptive(adaptive_event(mapping.transition));
        sink.shutdown();

        const auto health = sink.health();
        CHECK(health.healthy);
        CHECK(health.complete_records == 1);
        CHECK(complete_lines(output.bytes()).size() == 1);
        CHECK(rendered(output).find(
                  std::string{"\"event_type\":\""} +
                  mapping.name + "\"") != std::string::npos);
        observed_types.push_back(mapping.type);
        observed_names.emplace_back(mapping.name);
    }
}

TEST_CASE("AE01 maps exact command and accepted reputation audit events",
          "[adaptive-v2][structured-event][audit][schema]")
{
    using AuditEmit = void (AuditStructuredEventEmitter::*)(
        const AuditStructuredEventPayload &) noexcept;
    static_assert(
        std::is_same<
            decltype(&AuditStructuredEventEmitter::emit_audit),
            AuditEmit>::value,
        "audit emission cannot influence protocol or manager control flow");
    static_assert(
        std::variant_size<AuditStructuredEventPayload>::value == 10,
        "the audit capability appends containment coverage evidence");
    static_assert(
        std::is_same<
            std::variant_alternative_t<2, AuditStructuredEventPayload>,
            AdaptiveV2ConvergenceStructuredEvent>::value,
        "the third audit payload is the adaptive-v2 convergence event");
    static_assert(
        std::is_same<
            std::variant_alternative_t<3, AuditStructuredEventPayload>,
            AdaptiveV2EvidenceSnapshotStructuredEvent>::value,
        "the fourth audit payload is the manager evidence snapshot");
    static_assert(
        std::is_same<
            std::variant_alternative_t<4, AuditStructuredEventPayload>,
            AdaptiveV2ManagerSessionTerminalStructuredEvent>::value,
        "the fifth audit payload is the manager-session terminal event");
    static_assert(
        std::is_same<
            std::variant_alternative_t<5, AuditStructuredEventPayload>,
            EvidenceObservationAcceptedStructuredEvent>::value,
        "the sixth audit payload is one full accepted observation");
    static_assert(
        std::is_same<
            std::variant_alternative_t<6, AuditStructuredEventPayload>,
            AdaptiveV2ShapeDecisionStructuredEvent>::value,
        "the seventh audit payload is the pure shape decision");
    static_assert(
        std::is_same<
            std::variant_alternative_t<7, AuditStructuredEventPayload>,
            FaultContributionOpportunityStructuredEvent>::value,
        "the eighth audit payload is a prospective contribution");
    static_assert(
        std::is_same<
            std::variant_alternative_t<8, AuditStructuredEventPayload>,
            RootQcQueueBlockedStructuredEvent>::value,
        "the ninth audit payload is an exact blocked root QC");
    static_assert(
        std::is_same<
            std::variant_alternative_t<9, AuditStructuredEventPayload>,
            AdaptiveV2FaultContainmentCoverageReadyStructuredEvent>::value,
        "the tenth audit payload is exact containment tree coverage");
    static_assert(
        std::is_base_of<
            AuditStructuredEventEmitter,
            StructuredEventSink>::value,
        "the bounded sink shares one sequence and output across audit events");

    const auto command_type = hotstuff::structured_event_type(
        AuditStructuredEventPayload{epoch_command_event()});
    CHECK(command_type == StructuredEventType::epoch_command_committed);
    CHECK(std::string(hotstuff::structured_event_type_name(command_type)) ==
          "epoch.command_committed");

    const auto reputation_type = hotstuff::structured_event_type(
        AuditStructuredEventPayload{reputation_event()});
    CHECK(reputation_type ==
          StructuredEventType::reputation_evidence_applied);
    CHECK(std::string(
              hotstuff::structured_event_type_name(reputation_type)) ==
          "reputation.evidence_applied");

    const auto observation_type = hotstuff::structured_event_type(
        AuditStructuredEventPayload{observation_accepted_event()});
    CHECK(observation_type ==
          StructuredEventType::evidence_observation_accepted);
    CHECK(std::string(
              hotstuff::structured_event_type_name(observation_type)) ==
          "evidence.observation_accepted");

    const auto snapshot_type = hotstuff::structured_event_type(
        AuditStructuredEventPayload{evidence_snapshot_event()});
    CHECK(snapshot_type ==
          StructuredEventType::adaptive_v2_evidence_snapshot);
    CHECK(std::string(
              hotstuff::structured_event_type_name(snapshot_type)) ==
          "adaptive_v2_evidence_snapshot");

    const auto terminal_type = hotstuff::structured_event_type(
        AuditStructuredEventPayload{manager_terminal_event()});
    CHECK(terminal_type ==
          StructuredEventType::adaptive_v2_session_terminal);
    CHECK(std::string(
              hotstuff::structured_event_type_name(terminal_type)) ==
          "adaptive_v2_session_terminal");

    const auto shape_type = hotstuff::structured_event_type(
        AuditStructuredEventPayload{shape_decision_event()});
    CHECK(shape_type ==
          StructuredEventType::adaptive_v2_shape_decision);
    CHECK(std::string(
              hotstuff::structured_event_type_name(shape_type)) ==
          "adaptive_v2_shape_decision");

    const auto coverage_type = hotstuff::structured_event_type(
        AuditStructuredEventPayload{
            fault_containment_coverage_ready_event()});
    CHECK(coverage_type == StructuredEventType::
          adaptive_v2_fault_containment_coverage_ready);
    CHECK(std::string(
              hotstuff::structured_event_type_name(coverage_type)) ==
          "adaptive_v2.fault_containment_coverage_ready");
}

TEST_CASE("AE01 serializes exact command and accepted reputation identities",
          "[adaptive-v2][structured-event][audit][ndjson]")
{
    SECTION("shape decision contains the complete recomputable record")
    {
        const auto event = shape_decision_event();
        const auto &decision = event.decision;
        REQUIRE(decision.decision_digest ==
                hotstuff::compute_shape_decision_digest(decision));

        const auto payload =
            hotstuff::serialize_adaptive_v2_shape_decision_payload(
                event, 64 * 1024);
        const auto expected =
            "{\"cycle_ordinal\":3,"
            "\"transition_artifact_id\":\"e7-to-e8-shape\","
            "\"decision\":{\"schema_version\":1,"
            "\"status\":\"selected\","
            "\"selector_version\":\"shape-v1\","
            "\"tie_rule\":\"lower-latency-risk-churn-current-canonical-v1\","
            "\"epoch_number\":7,"
            "\"epoch_digest\":\"" +
            decision.epoch_digest.to_hex() + "\","
            "\"current_topology_digest\":\"" +
            decision.current_topology_digest.to_hex() + "\","
            "\"evidence_cutoff\":52,"
            "\"evidence_digest\":\"" +
            decision.evidence_digest.to_hex() + "\","
            "\"predecessor_tree_count\":7,"
            "\"tree_count\":5,"
            "\"fixed_pipeline_stretch\":2,"
            "\"deterministic_seed\":41719,"
            "\"current_fanout\":2,"
            "\"selected_fanout\":5,"
            "\"applied_fanout\":5,"
            "\"reference_tree_rule\":\"lowest-tree-id-prefix-q-v1\","
            "\"candidates\":[{\"fanout\":2,"
            "\"rejection\":\"none\","
            "\"depth\":2,"
            "\"risk\":900000,\"latency\":800,\"churn\":0,"
            "\"switch_threshold_satisfied\":false},{"
            "\"fanout\":3,"
            "\"rejection\":\"missing_influential_evidence\","
            "\"depth\":2,"
            "\"risk\":0,\"latency\":0,\"churn\":0,"
            "\"switch_threshold_satisfied\":false},{"
            "\"fanout\":5,\"rejection\":\"none\","
            "\"depth\":2,"
            "\"risk\":600000,\"latency\":700,\"churn\":20,"
            "\"switch_threshold_satisfied\":true}],"
            "\"decision_digest\":\"" +
            decision.decision_digest.to_hex() + "\"}}";
        CHECK(payload == expected);

        FakeClock clock({6999});
        MemoryOutput output;
        StructuredEventSink sink(
            manager_event_config(), clock, output);
        sink.emit_audit(AuditStructuredEventPayload{event});
        sink.shutdown();
        CHECK(sink.health().healthy);
        CHECK(rendered(output).find(
                  "\"event_type\":\"adaptive_v2_shape_decision\","
                  "\"payload\":" + expected) !=
              std::string::npos);
    }

    SECTION("committed command includes the consensus block and schedule")
    {
        const auto event = epoch_command_event();
        const auto expected =
            "{\"event_schema_version\":1,"
            "\"run_id\":\"run-structured-event\","
            "\"source_kind\":\"replica\","
            "\"source_id\":\"replica-2\","
            "\"source_instance\":\"spawn-9\","
            "\"source_sequence\":1,"
            "\"source_monotonic_ns\":7000,"
            "\"event_type\":\"epoch.command_committed\","
            "\"payload\":{"
            "\"command_block_height\":2345,"
            "\"command_block_hash\":\"" +
            event.command_block_hash.to_hex() + "\","
            "\"payload_digest\":\"" + event.payload_digest.to_hex() + "\","
            "\"predecessor_epoch_number\":7,"
            "\"predecessor_epoch_digest\":\"" +
            event.predecessor_epoch_digest.to_hex() + "\","
            "\"successor_epoch_number\":8,"
            "\"successor_epoch_digest\":\"" +
            event.successor_epoch_digest.to_hex() + "\","
            "\"activation_delay_blocks\":20,"
            "\"activation_height\":2365}}\n";

        FakeClock clock({7000});
        MemoryOutput output;
        StructuredEventSink sink(event_config(), clock, output);
        AuditStructuredEventEmitter &audit = sink;
        audit.emit_audit(AuditStructuredEventPayload{event});
        sink.shutdown();

        CHECK(sink.health().healthy);
        CHECK(sink.health().complete_records == 1);
        CHECK(rendered(output) == expected);

    }

    SECTION("applied reputation includes the accepted audit update and cutoff")
    {
        const auto event = reputation_event();
        const auto &audit_update = event.update;
        const auto expected =
            "{\"event_schema_version\":1,"
            "\"run_id\":\"run-structured-event\","
            "\"source_kind\":\"adaptation_manager\","
            "\"source_id\":\"adaptive-manager\","
            "\"source_instance\":\"manager-spawn-4\","
            "\"source_sequence\":1,"
            "\"source_monotonic_ns\":7001,"
            "\"event_type\":\"reputation.evidence_applied\","
            "\"payload\":{"
            "\"evidence_cutoff\":44,"
            "\"ingestion_sequence\":42,"
            "\"observation_id\":\"" +
            audit_update.observation_id.to_hex() + "\","
            "\"reporter_id\":2,"
            "\"target_id\":0,"
            "\"evidence_outcome\":\"timeout\","
            "\"reputation_outcome\":\"timeout\","
            "\"delta\":-1,"
            "\"resulting_score\":-3}}\n";

        FakeClock clock({7001});
        MemoryOutput output;
        StructuredEventSink sink(manager_event_config(), clock, output);
        AuditStructuredEventEmitter &audit = sink;
        audit.emit_audit(AuditStructuredEventPayload{event});
        sink.shutdown();

        CHECK(sink.health().healthy);
        CHECK(sink.health().complete_records == 1);
        CHECK(rendered(output) == expected);
    }

    SECTION("accepted observation preserves the complete ledger record")
    {
        const auto event = observation_accepted_event();
        const auto &observation = event.record.observation;
        const auto expected =
            "{\"event_schema_version\":1,"
            "\"run_id\":\"run-structured-event\","
            "\"source_kind\":\"adaptation_manager\","
            "\"source_id\":\"adaptive-manager\","
            "\"source_instance\":\"manager-spawn-4\","
            "\"source_sequence\":1,"
            "\"source_monotonic_ns\":7002,"
            "\"event_type\":\"evidence.observation_accepted\","
            "\"payload\":{\"ingestion_sequence\":42,"
            "\"observation\":{\"schema_version\":1,"
            "\"observation_id\":\"" +
            observation.observation_id.to_hex() + "\","
            "\"reporter_id\":2,"
            "\"observed_replica_id\":0,"
            "\"configuration\":{\"epoch_number\":7,"
            "\"tree_id\":3,"
            "\"epoch_digest\":\"" +
            observation.configuration.epoch_digest.to_hex() + "\"},"
            "\"block_hash\":\"" +
            observation.block_hash.to_hex() + "\","
            "\"expected_message_type\":\"aggregate_relay\","
            "\"outcome\":\"late\","
            "\"response_duration_us\":150,"
            "\"deadline_duration_us\":100,"
            "\"reporter_monotonic_ns\":91000,"
            "\"reporter_sequence\":9,"
            "\"signer_set\":[0,1]}}}\n";

        FakeClock clock({7002});
        MemoryOutput output;
        StructuredEventSink sink(
            manager_event_config(), clock, output);
        sink.emit_audit(AuditStructuredEventPayload{event});
        sink.shutdown();

        CHECK(sink.health().healthy);
        CHECK(sink.health().complete_records == 1);
        CHECK(rendered(output) == expected);
    }

    SECTION("fault containment coverage readiness has exact source-bound JSON")
    {
        const auto event = fault_containment_coverage_ready_event();
        const auto expected =
            "{\"event_schema_version\":1,"
            "\"run_id\":\"run-structured-event\","
            "\"source_kind\":\"adaptation_manager\","
            "\"source_id\":\"adaptive-manager\","
            "\"source_instance\":\"manager-spawn-4\","
            "\"source_sequence\":1,"
            "\"source_monotonic_ns\":7003,"
            "\"event_type\":\"adaptive_v2.fault_containment_coverage_ready\","
            "\"payload\":{"
            "\"cycle_ordinal\":0,"
            "\"transition_artifact_id\":\"e0-to-e1-containment\","
            "\"predecessor_epoch_number\":0,"
            "\"predecessor_epoch_digest\":\"" +
            event.predecessor_epoch_digest.to_hex() + "\","
            "\"fault_evidence_start_monotonic_ns\":900000,"
            "\"evidence_cutoff\":313,"
            "\"required_tree_ids\":[3,7,12,29,30],"
            "\"observed_tree_ids\":[3,7,12,29,30]}}\n";

        FakeClock clock({7003});
        MemoryOutput output;
        StructuredEventSink sink(
            manager_event_config(), clock, output);
        sink.emit_audit(AuditStructuredEventPayload{event});
        sink.shutdown();

        CHECK(sink.health().healthy);
        CHECK(sink.health().complete_records == 1);
        CHECK(rendered(output) == expected);
    }

    SECTION("evidence snapshot event and artifact share one canonical payload")
    {
        const auto event = evidence_snapshot_event();
        const auto payload =
            "{\"schema_version\":2,"
            "\"cycle_ordinal\":3,"
            "\"policy_intent\":\"fault_containment\","
            "\"transition_artifact_id\":\"e7-to-e8-containment\","
            "\"predecessor_epoch_number\":7,"
            "\"predecessor_epoch_digest\":\"" +
            event.predecessor_epoch_digest.to_hex() + "\","
            "\"activation_generation\":30064771089,"
            "\"baseline_cutoff\":44,"
            "\"current_cutoff\":52,"
            "\"full_prefix_snapshot_id\":\"" +
            event.full_prefix_snapshot_id.to_hex() + "\","
            "\"evidence_snapshot_id\":\"" +
            event.evidence_snapshot_id.to_hex() + "\","
            "\"accepted_prefix_count\":2,"
            "\"eligible_ranking\":[2,3,4,5,6]}";

        CHECK(hotstuff::
                  serialize_adaptive_v2_evidence_snapshot_payload(
                      event, 64 * 1024) == payload);

        const auto expected =
            "{\"event_schema_version\":1,"
            "\"run_id\":\"run-structured-event\","
            "\"source_kind\":\"adaptation_manager\","
            "\"source_id\":\"adaptive-manager\","
            "\"source_instance\":\"manager-spawn-4\","
            "\"source_sequence\":1,"
            "\"source_monotonic_ns\":7002,"
            "\"event_type\":\"adaptive_v2_evidence_snapshot\","
            "\"payload\":" + payload + "}\n";
        FakeClock clock({7002});
        MemoryOutput output;
        StructuredEventSink sink(
            manager_event_config(), clock, output);
        sink.emit_audit(AuditStructuredEventPayload{event});
        sink.shutdown();

        CHECK(sink.health().healthy);
        CHECK(sink.health().complete_records == 1);
        CHECK(rendered(output) == expected);

        auto tight_config = manager_event_config();
        tight_config.limits.maximum_line_bytes = payload.size() + 1;
        FakeClock tight_clock({7003});
        MemoryOutput tight_output;
        StructuredEventSink tight_sink(
            tight_config, tight_clock, tight_output);
        tight_sink.emit_audit(AuditStructuredEventPayload{event});
        const auto tight_health = tight_sink.health();
        CHECK_FALSE(tight_health.healthy);
        CHECK(tight_health.first_failure ==
              StructuredEventFailure::line_too_large);
        CHECK(tight_health.last_assigned_sequence == 0);
        CHECK(tight_health.queued_events == 0);
        CHECK(tight_output.bytes().empty());
    }

    SECTION("session terminal binds intent artifact evidence and winner")
    {
        const auto event = manager_terminal_event();
        const auto &identity = *event.winning_activation;
        const auto expected =
            "{\"event_schema_version\":1,"
            "\"run_id\":\"run-structured-event\","
            "\"source_kind\":\"adaptation_manager\","
            "\"source_id\":\"adaptive-manager\","
            "\"source_instance\":\"manager-spawn-4\","
            "\"source_sequence\":1,"
            "\"source_monotonic_ns\":7002,"
            "\"event_type\":\"adaptive_v2_session_terminal\","
            "\"payload\":{\"cycle_ordinal\":3,"
            "\"policy_intent\":\"fault_containment\","
            "\"outcome\":\"advanced\","
            "\"reason\":\"successor_converged\","
            "\"transition_artifact_id\":\"e7-to-e8-containment\","
            "\"predecessor_epoch_number\":7,"
            "\"predecessor_epoch_digest\":\"" +
            event.predecessor_epoch_digest.to_hex() + "\","
            "\"successor_epoch_number\":8,"
            "\"successor_epoch_digest\":\"" +
            event.successor_epoch_digest->to_hex() + "\","
            "\"command_payload_digest\":\"" +
            event.command_payload_digest->to_hex() + "\","
            "\"winning_activation\":{"
            "\"predecessor_epoch_number\":7,"
            "\"predecessor_epoch_digest\":\"" +
            identity.predecessor_epoch_digest.to_hex() + "\","
            "\"successor_epoch_number\":8,"
            "\"successor_epoch_digest\":\"" +
            identity.successor_epoch_digest.to_hex() + "\","
            "\"command_payload_digest\":\"" +
            identity.command_payload_digest.to_hex() + "\","
            "\"command_block_height\":2345,"
            "\"command_block_hash\":\"" +
            identity.command_block_hash.to_hex() + "\","
            "\"activation_delay_blocks\":20,"
            "\"activation_height\":2365},"
            "\"evidence_window_activation_generation\":30064771089,"
            "\"baseline_evidence_cutoff\":44,"
            "\"current_evidence_cutoff\":52}}\n";

        FakeClock clock({7002});
        MemoryOutput output;
        StructuredEventSink sink(manager_event_config(), clock, output);
        sink.emit_audit(AuditStructuredEventPayload{event});
        sink.shutdown();

        CHECK(sink.health().healthy);
        CHECK(sink.health().complete_records == 1);
        CHECK(rendered(output) == expected);
    }
}

TEST_CASE(
    "compact snapshot stays below the default line bound at large cutoffs",
    "[adaptive-v2][structured-event][audit][snapshot][capacity]")
{
    constexpr std::uint64_t kLargeExactPrefix = 1'048'576;
    const auto defaults = StructuredEventLimits{};
    auto event = evidence_snapshot_event();
    event.baseline_cutoff = kLargeExactPrefix - 1;
    event.current_cutoff = kLargeExactPrefix;
    event.accepted_prefix_count = kLargeExactPrefix;

    const auto payload =
        hotstuff::serialize_adaptive_v2_evidence_snapshot_payload(
            event, defaults.maximum_line_bytes);
    REQUIRE(payload.size() < defaults.maximum_line_bytes);
    CHECK(payload.size() < 1024);
    CHECK(count_occurrences(payload, "\"observation_id\":") == 0);
    CHECK(payload.find("\"accepted_prefix_count\":1048576") !=
          std::string::npos);

    auto config = manager_event_config();
    FakeClock clock({7004});
    MemoryOutput output;
    StructuredEventSink sink(config, clock, output);
    sink.emit_audit(AuditStructuredEventPayload{event});
    const auto queued = sink.health();
    REQUIRE(queued.healthy);
    CHECK(queued.queued_events == 1);
    CHECK(queued.queued_bytes > payload.size());
    CHECK(queued.queued_bytes <= defaults.maximum_line_bytes);
    sink.shutdown();

    const auto record = rendered(output);
    const auto payload_marker = record.find("\"payload\":");
    REQUIRE(payload_marker != std::string::npos);
    CHECK(record.compare(
              payload_marker + std::string("\"payload\":").size(),
              payload.size(),
              payload) == 0);
    CHECK(count_occurrences(record, "\"observation_id\":") == 0);
    CHECK(sink.health().healthy);
    CHECK(sink.health().complete_records == 1);
}

TEST_CASE("AE01 rejects incomplete or source-confused audit events atomically",
          "[adaptive-v2][structured-event][audit][validation]")
{
    const auto rejects = [](
                             StructuredEventConfig config,
                             AuditStructuredEventPayload payload) {
        FakeClock clock({8000});
        MemoryOutput output;
        StructuredEventSink sink(std::move(config), clock, output);
        sink.emit_audit(payload);
        const auto failed = sink.health();
        return !failed.healthy && failed.stopped &&
               failed.first_failure ==
                   StructuredEventFailure::invalid_payload &&
               failed.last_assigned_sequence == 0 &&
               failed.dropped_records == 1 && clock.calls() == 0 &&
               output.bytes().empty() && output.write_calls() == 0;
    };

    SECTION("command identity and derived activation must be exact")
    {
        auto invalid = epoch_command_event();
        invalid.command_block_height = 0;
        CHECK(rejects(event_config(), invalid));

        invalid = epoch_command_event();
        invalid.command_block_hash = uint256_t{};
        CHECK(rejects(event_config(), invalid));

        invalid = epoch_command_event();
        invalid.payload_digest = uint256_t{};
        CHECK(rejects(event_config(), invalid));

        invalid = epoch_command_event();
        invalid.predecessor_epoch_digest = uint256_t{};
        CHECK(rejects(event_config(), invalid));

        invalid = epoch_command_event();
        invalid.successor_epoch_digest = uint256_t{};
        CHECK(rejects(event_config(), invalid));

        invalid = epoch_command_event();
        invalid.successor_epoch_number = invalid.predecessor_epoch_number;
        CHECK(rejects(event_config(), invalid));

        invalid = epoch_command_event();
        ++invalid.activation_height;
        CHECK(rejects(event_config(), invalid));

        invalid = epoch_command_event();
        invalid.activation_delay_blocks = 0;
        invalid.activation_height = invalid.command_block_height;
        CHECK(rejects(event_config(), invalid));

        invalid = epoch_command_event();
        invalid.command_block_height =
            std::numeric_limits<std::uint64_t>::max();
        invalid.activation_delay_blocks = 1;
        invalid.activation_height =
            std::numeric_limits<std::uint64_t>::max();
        CHECK(rejects(event_config(), invalid));

        invalid = epoch_command_event();
        invalid.predecessor_epoch_number =
            std::numeric_limits<std::uint32_t>::max();
        invalid.successor_epoch_number = 0;
        CHECK(rejects(event_config(), invalid));

        CHECK(rejects(
            manager_event_config(), epoch_command_event()));
    }

    SECTION("reputation event must be one accepted projection update")
    {
        auto invalid = reputation_event();
        invalid.evidence_cutoff = 0;
        CHECK(rejects(manager_event_config(), invalid));

        invalid = reputation_event();
        invalid.update.ingestion_sequence = 0;
        CHECK(rejects(manager_event_config(), invalid));

        invalid = reputation_event();
        invalid.evidence_cutoff =
            invalid.update.ingestion_sequence - 1;
        CHECK(rejects(manager_event_config(), invalid));

        invalid = reputation_event();
        invalid.update.observation_id = uint256_t{};
        CHECK(rejects(manager_event_config(), invalid));

        invalid = reputation_event();
        invalid.update.target_id = invalid.update.reporter_id;
        CHECK(rejects(manager_event_config(), invalid));

        invalid = reputation_event();
        invalid.update.evidence_outcome = ResponseOutcome::on_time;
        CHECK(rejects(manager_event_config(), invalid));

        invalid = reputation_event();
        invalid.update.reputation_outcome =
            SimpleReputationOutcome::response;
        CHECK(rejects(manager_event_config(), invalid));

        invalid = reputation_event();
        invalid.update.delta = 1;
        CHECK(rejects(manager_event_config(), invalid));

        invalid = reputation_event();
        invalid.update.evidence_outcome =
            static_cast<ResponseOutcome>(0);
        CHECK(rejects(manager_event_config(), invalid));

        invalid = reputation_event();
        invalid.update.reputation_outcome =
            static_cast<SimpleReputationOutcome>(0);
        CHECK(rejects(manager_event_config(), invalid));

        CHECK(rejects(event_config(), reputation_event()));
    }

    SECTION("accepted observation must be one complete canonical ledger record")
    {
        auto invalid = observation_accepted_event();
        CHECK(rejects(event_config(), invalid));

        invalid = observation_accepted_event();
        invalid.record.ingestion_sequence = 0;
        CHECK(rejects(manager_event_config(), invalid));

        invalid = observation_accepted_event();
        invalid.record.observation.schema_version = 0;
        CHECK(rejects(manager_event_config(), invalid));

        invalid = observation_accepted_event();
        invalid.record.observation.observation_id = uint256_t{};
        CHECK(rejects(manager_event_config(), invalid));

        invalid = observation_accepted_event();
        ++invalid.record.observation.configuration.tree_id;
        CHECK(rejects(manager_event_config(), invalid));

        invalid = observation_accepted_event();
        invalid.record.observation.observed_replica_id =
            invalid.record.observation.reporter_id;
        CHECK(rejects(manager_event_config(), invalid));

        invalid = observation_accepted_event();
        invalid.record.observation.configuration.epoch_digest =
            uint256_t{};
        CHECK(rejects(manager_event_config(), invalid));

        invalid = observation_accepted_event();
        invalid.record.observation.block_hash = uint256_t{};
        CHECK(rejects(manager_event_config(), invalid));

        invalid = observation_accepted_event();
        invalid.record.observation.expected_message_type =
            static_cast<ExpectedMessageType>(0);
        CHECK(rejects(manager_event_config(), invalid));

        invalid = observation_accepted_event();
        invalid.record.observation.outcome =
            static_cast<ResponseOutcome>(0);
        CHECK(rejects(manager_event_config(), invalid));

        invalid = observation_accepted_event();
        invalid.record.observation.deadline_duration_us = 0;
        CHECK(rejects(manager_event_config(), invalid));

        invalid = observation_accepted_event();
        invalid.record.observation.reporter_sequence = 0;
        CHECK(rejects(manager_event_config(), invalid));

        invalid = observation_accepted_event();
        invalid.record.observation.signer_set = {1, 0};
        CHECK(rejects(manager_event_config(), invalid));

        invalid = observation_accepted_event();
        invalid.record.observation.signer_set.clear();
        CHECK(rejects(manager_event_config(), invalid));

        invalid = observation_accepted_event();
        invalid.record.observation.response_duration_us =
            invalid.record.observation.deadline_duration_us - 1;
        CHECK(rejects(manager_event_config(), invalid));

        invalid = observation_accepted_event();
        invalid.record.observation.outcome = ResponseOutcome::timeout;
        invalid.record.observation.response_duration_us = 0;
        CHECK(rejects(manager_event_config(), invalid));

        auto timeout = observation_accepted_event();
        timeout.record.observation.outcome = ResponseOutcome::timeout;
        timeout.record.observation.response_duration_us = 0;
        timeout.record.observation.signer_set.clear();
        FakeClock clock({8001});
        MemoryOutput output;
        StructuredEventSink sink(
            manager_event_config(), clock, output);
        sink.emit_audit(AuditStructuredEventPayload{timeout});
        sink.shutdown();
        CHECK(sink.health().healthy);
        CHECK(rendered(output).find(
                  "\"outcome\":\"timeout\","
                  "\"response_duration_us\":0") !=
              std::string::npos);
        CHECK(rendered(output).find("\"signer_set\":[]") !=
              std::string::npos);
    }

    SECTION("fault containment coverage event is exact complete and manager-owned")
    {
        CHECK(rejects(
            event_config(), fault_containment_coverage_ready_event()));

        auto invalid = fault_containment_coverage_ready_event();
        invalid.fault_evidence_start_monotonic_ns = 0;
        CHECK(rejects(manager_event_config(), invalid));

        invalid = fault_containment_coverage_ready_event();
        invalid.evidence_cutoff = 0;
        CHECK(rejects(manager_event_config(), invalid));

        invalid = fault_containment_coverage_ready_event();
        invalid.required_tree_ids.pop_back();
        CHECK(rejects(manager_event_config(), invalid));

        invalid = fault_containment_coverage_ready_event();
        invalid.observed_tree_ids[1] = invalid.observed_tree_ids[0];
        CHECK(rejects(manager_event_config(), invalid));

        invalid = fault_containment_coverage_ready_event();
        invalid.required_tree_ids = {3, 3, 12};
        invalid.observed_tree_ids = invalid.required_tree_ids;
        CHECK(rejects(manager_event_config(), invalid));
    }

    SECTION("evidence snapshot is a bounded signed prefix commitment")
    {
        auto invalid = evidence_snapshot_event();
        CHECK(rejects(event_config(), invalid));

        invalid = evidence_snapshot_event();
        invalid.activation_generation =
            (std::uint64_t{8} << 32) | std::uint64_t{1};
        CHECK(rejects(manager_event_config(), invalid));

        invalid = evidence_snapshot_event();
        invalid.schema_version = 1;
        CHECK(rejects(manager_event_config(), invalid));

        invalid = evidence_snapshot_event();
        invalid.full_prefix_snapshot_id = hotstuff::uint256_t{};
        CHECK(rejects(manager_event_config(), invalid));

        invalid = evidence_snapshot_event();
        invalid.evidence_snapshot_id = hotstuff::uint256_t{};
        CHECK(rejects(manager_event_config(), invalid));

        invalid = evidence_snapshot_event();
        invalid.accepted_prefix_count = 0;
        CHECK(rejects(manager_event_config(), invalid));

        invalid = evidence_snapshot_event();
        invalid.accepted_prefix_count = invalid.current_cutoff + 1;
        CHECK(rejects(manager_event_config(), invalid));

        invalid = evidence_snapshot_event();
        invalid.current_cutoff = invalid.baseline_cutoff;
        CHECK(rejects(manager_event_config(), invalid));

        invalid = evidence_snapshot_event();
        invalid.eligible_ranking[1] =
            invalid.eligible_ranking[0];
        CHECK(rejects(manager_event_config(), invalid));

        invalid = evidence_snapshot_event();
        invalid.eligible_ranking.clear();
        CHECK(rejects(manager_event_config(), invalid));

        const auto sparse = evidence_snapshot_event();
        REQUIRE(sparse.accepted_prefix_count < sparse.current_cutoff);
        CHECK_NOTHROW(
            hotstuff::serialize_adaptive_v2_evidence_snapshot_payload(
                sparse, StructuredEventLimits{}.maximum_line_bytes));

        CHECK_THROWS_AS(
            hotstuff::
                serialize_adaptive_v2_evidence_snapshot_payload(
                    evidence_snapshot_event(), 32),
            std::length_error);
    }

    SECTION("session terminal is exact and manager-owned")
    {
        auto invalid = manager_terminal_event();
        CHECK(rejects(event_config(), invalid));

        invalid = manager_terminal_event();
        invalid.transition_artifact_id.clear();
        CHECK(rejects(manager_event_config(), invalid));

        invalid = manager_terminal_event();
        invalid.evidence_window_activation_generation = 0;
        CHECK(rejects(manager_event_config(), invalid));

        invalid = manager_terminal_event();
        invalid.evidence_window_activation_generation = 17;
        CHECK(rejects(manager_event_config(), invalid));

        invalid = manager_terminal_event();
        invalid.baseline_evidence_cutoff =
            invalid.current_evidence_cutoff + 1;
        CHECK(rejects(manager_event_config(), invalid));

        invalid = manager_terminal_event();
        invalid.current_evidence_cutoff =
            invalid.baseline_evidence_cutoff;
        CHECK(rejects(manager_event_config(), invalid));

        invalid = manager_terminal_event();
        invalid.command_payload_digest.reset();
        CHECK(rejects(manager_event_config(), invalid));

        invalid = manager_terminal_event();
        invalid.winning_activation.reset();
        CHECK(rejects(manager_event_config(), invalid));

        invalid = manager_terminal_event();
        invalid.reason =
            AdaptiveV2ManagerCycleTerminalReason::caller_failed;
        CHECK(rejects(manager_event_config(), invalid));

        invalid = manager_terminal_event();
        invalid.winning_activation->successor_epoch_digest =
            digest("wrong-session-terminal-successor");
        CHECK(rejects(manager_event_config(), invalid));

        invalid = manager_terminal_event();
        invalid.predecessor_epoch_number =
            std::numeric_limits<std::uint32_t>::max();
        invalid.successor_epoch_number = 0;
        CHECK(rejects(manager_event_config(), invalid));
    }
}

template<typename Event>
std::string expected_convergence_record(
    const Event &event,
    const std::string &event_type,
    std::uint64_t monotonic_ns)
{
    std::string expected =
        "{\"event_schema_version\":1,"
        "\"run_id\":\"run-structured-event\","
        "\"source_kind\":\"adaptation_manager\","
        "\"source_id\":\"adaptive-manager\","
        "\"source_instance\":\"manager-spawn-4\","
        "\"source_sequence\":1,"
        "\"source_monotonic_ns\":" +
        std::to_string(monotonic_ns) +
        ",\"event_type\":\"" + event_type + "\","
        "\"payload\":{\"replica_id\":";
    expected += event.replica_id.has_value()
                    ? std::to_string(*event.replica_id)
                    : "null";
    expected += ",\"delivery_attempt\":";
    expected += event.delivery_attempt != 0
                    ? std::to_string(event.delivery_attempt)
                    : "null";
    expected += ",\"disposition\":";
    expected += event.disposition.empty()
                    ? "null"
                    : "\"" + event.disposition + "\"";
    expected += ",\"identity\":";
    if (!event.identity.has_value())
    {
        expected += "null";
    }
    else
    {
        const auto &identity = *event.identity;
        expected +=
            "{\"predecessor_epoch_number\":" +
            std::to_string(identity.predecessor_epoch_number) +
            ",\"predecessor_epoch_digest\":\"" +
            identity.predecessor_epoch_digest.to_hex() +
            "\",\"successor_epoch_number\":" +
            std::to_string(identity.successor_epoch_number) +
            ",\"successor_epoch_digest\":\"" +
            identity.successor_epoch_digest.to_hex() +
            "\",\"command_payload_digest\":\"" +
            identity.command_payload_digest.to_hex() +
            "\",\"command_block_height\":" +
            std::to_string(identity.command_block_height) +
            ",\"command_block_hash\":\"" +
            identity.command_block_hash.to_hex() +
            "\",\"activation_delay_blocks\":" +
            std::to_string(identity.activation_delay_blocks) +
            ",\"activation_height\":" +
            std::to_string(identity.activation_height) + "}";
    }
    expected +=
        ",\"accepted_commit_count\":" +
        std::to_string(event.accepted_commit_count) +
        ",\"accepted_activation_count\":" +
        std::to_string(event.accepted_activation_count) +
        ",\"required_activation_count\":" +
        std::to_string(event.required_activation_count) +
        ",\"canonical_payload_digest\":";
    expected += event.canonical_payload_digest.has_value()
                    ? "\"" +
                          event.canonical_payload_digest->to_hex() + "\""
                    : "null";
    expected += ",\"failure_reason\":";
    expected += event.failure_reason.empty()
                    ? "null"
                    : "\"" + event.failure_reason + "\"";
    expected += "}}\n";
    return expected;
}

template<typename Event>
void exercise_convergence_structured_event_contract()
{
    struct Mapping
    {
        AdaptiveV2ConvergenceTransition transition;
        StructuredEventType type;
        const char *name;
        const char *disposition;
    };
    const std::array<Mapping, 9> mappings{{
        {AdaptiveV2ConvergenceTransition::delivery_attempt,
         StructuredEventType::adaptive_v2_delivery_attempt,
         "adaptive_v2_delivery_attempt",
         "enqueued"},
        {AdaptiveV2ConvergenceTransition::delivery_attempt,
         StructuredEventType::adaptive_v2_delivery_attempt,
         "adaptive_v2_delivery_attempt",
         "injected_drop"},
        {AdaptiveV2ConvergenceTransition::commit_observed,
         StructuredEventType::adaptive_v2_commit_observed,
         "adaptive_v2_commit_observed",
         "accepted"},
        {AdaptiveV2ConvergenceTransition::activation_observed,
         StructuredEventType::adaptive_v2_activation_observed,
         "adaptive_v2_activation_observed",
         "accepted"},
        {AdaptiveV2ConvergenceTransition::activation_observed,
         StructuredEventType::adaptive_v2_activation_observed,
         "adaptive_v2_activation_observed",
         "ack_injected_drop"},
        {AdaptiveV2ConvergenceTransition::activation_observed,
         StructuredEventType::adaptive_v2_activation_observed,
         "adaptive_v2_activation_observed",
         "ack_sent"},
        {AdaptiveV2ConvergenceTransition::converged,
         StructuredEventType::adaptive_v2_converged,
         "adaptive_v2_converged",
         ""},
        {AdaptiveV2ConvergenceTransition::ready,
         StructuredEventType::adaptive_v2_ready,
         "adaptive_v2_ready",
         ""},
        {AdaptiveV2ConvergenceTransition::failure,
         StructuredEventType::adaptive_v2_convergence_failure,
         "adaptive_v2_convergence_failure",
         ""},
    }};

    std::uint64_t monotonic_ns = 8100;
    for (const auto &mapping : mappings)
    {
        CAPTURE(mapping.name);
        const auto event = convergence_event<Event>(
            mapping.transition, mapping.disposition);
        const auto type = hotstuff::structured_event_type(
            AuditStructuredEventPayload{event});
        CHECK(type == mapping.type);
        CHECK(std::string(hotstuff::structured_event_type_name(type)) ==
              mapping.name);

        FakeClock clock({monotonic_ns});
        MemoryOutput output;
        StructuredEventSink sink(manager_event_config(), clock, output);
        sink.emit_audit(AuditStructuredEventPayload{event});
        sink.shutdown();
        CHECK(sink.health().healthy);
        CHECK(sink.health().complete_records == 1);
        CHECK(rendered(output) == expected_convergence_record(
                  event, mapping.name, monotonic_ns));
        ++monotonic_ns;
    }

    const auto rejects = [](
                             StructuredEventConfig config,
                             Event event) {
        FakeClock clock({8200});
        MemoryOutput output;
        StructuredEventSink sink(std::move(config), clock, output);
        sink.emit_audit(AuditStructuredEventPayload{std::move(event)});
        const auto health = sink.health();
        return !health.healthy && health.stopped &&
               health.first_failure ==
                   StructuredEventFailure::invalid_payload &&
               health.last_assigned_sequence == 0 &&
               output.bytes().empty();
    };

    SECTION("delivery outcome distinguishes accepted and failed enqueue")
    {
        auto failed = convergence_event<Event>(
            AdaptiveV2ConvergenceTransition::delivery_attempt,
            "enqueue_failed");
        FakeClock clock({8300});
        MemoryOutput output;
        StructuredEventSink sink(manager_event_config(), clock, output);
        sink.emit_audit(AuditStructuredEventPayload{failed});
        sink.shutdown();
        REQUIRE(sink.health().healthy);
        CHECK(rendered(output).find(
                  "\"disposition\":\"enqueue_failed\"") !=
              std::string::npos);

        failed.disposition = "accepted";
        CHECK(rejects(manager_event_config(), failed));
    }

    SECTION("terminal success requires the exact Q-winning identity")
    {
        auto missing = convergence_event<Event>(
            AdaptiveV2ConvergenceTransition::converged);
        missing.identity.reset();
        CHECK(rejects(manager_event_config(), missing));

        missing = convergence_event<Event>(
            AdaptiveV2ConvergenceTransition::ready);
        missing.identity.reset();
        CHECK(rejects(manager_event_config(), missing));
    }

    SECTION("terminal success is manager-wide and rejects a replica id")
    {
        auto invalid = convergence_event<Event>(
            AdaptiveV2ConvergenceTransition::converged);
        invalid.replica_id = 4;
        CHECK(rejects(manager_event_config(), invalid));

        invalid = convergence_event<Event>(
            AdaptiveV2ConvergenceTransition::ready);
        invalid.replica_id = 4;
        CHECK(rejects(manager_event_config(), invalid));
    }

    SECTION("identity and failure reason validation fail closed")
    {
        auto invalid = convergence_event<Event>(
            AdaptiveV2ConvergenceTransition::activation_observed,
            "accepted");
        invalid.identity->command_block_hash = uint256_t{};
        CHECK(rejects(manager_event_config(), invalid));

        invalid = convergence_event<Event>(
            AdaptiveV2ConvergenceTransition::activation_observed,
            "accepted");
        ++invalid.identity->activation_height;
        CHECK(rejects(manager_event_config(), invalid));

        auto failure = convergence_event<Event>(
            AdaptiveV2ConvergenceTransition::failure);
        failure.failure_reason.clear();
        CHECK(rejects(manager_event_config(), failure));

        failure = convergence_event<Event>(
            AdaptiveV2ConvergenceTransition::failure);
        failure.failure_reason = raw_bytes({0xff});
        CHECK(rejects(manager_event_config(), failure));
    }

    SECTION("every ingress disposition is auditable without changing authority")
    {
        struct IngressCase
        {
            const char *disposition;
            bool authenticated;
            bool decoded;
        };
        const std::array<IngressCase, 9> cases{{
            {"rejected_unauthenticated_source", false, false},
            {"rejected_wire_decode", true, false},
            {"accepted", true, true},
            {"duplicate", true, true},
            {"rejected_nonmember", true, true},
            {"rejected_spoofed_source", true, true},
            {"rejected_stale", true, true},
            {"rejected_wrong_identity", true, true},
            {"conflicting_observation", true, true},
        }};
        for (const auto &item : cases)
        {
            CAPTURE(item.disposition);
            Event observation;
            observation.transition =
                AdaptiveV2ConvergenceTransition::activation_observed;
            observation.required_activation_count = 5;
            observation.disposition = item.disposition;
            if (item.authenticated)
                observation.replica_id = 4;
            if (item.decoded)
            {
                observation.identity = convergence_event_identity();
                observation.canonical_payload_digest =
                    digest("convergence-audit-canonical-payload");
            }

            FakeClock clock({8400});
            MemoryOutput output;
            StructuredEventSink sink(
                manager_event_config(), clock, output);
            sink.emit_audit(AuditStructuredEventPayload{observation});
            sink.shutdown();
            CHECK(sink.health().healthy);
            CHECK(sink.health().complete_records == 1);
            CHECK(rendered(output).find(
                      std::string("\"disposition\":\"") +
                      item.disposition + "\"") != std::string::npos);
        }
    }

    CHECK(rejects(
        event_config(),
        convergence_event<Event>(
            AdaptiveV2ConvergenceTransition::delivery_attempt,
            "enqueued")));
}

TEST_CASE(
    "adaptive-v2 convergence audit has exact semantic NDJSON for all six transitions",
    "[adaptive-v2][structured-event][convergence][semantic][ndjson]")
{
    if constexpr (has_convergence_disposition<
                      AdaptiveV2ConvergenceStructuredEvent>::value)
    {
        exercise_convergence_structured_event_contract<
            AdaptiveV2ConvergenceStructuredEvent>();
    }
    else
    {
        FAIL("convergence audit lacks an explicit delivery and observation disposition");
    }
}

TEST_CASE("AE01 accepts only the exact evidence-to-reputation projection",
          "[adaptive-v2][structured-event][audit][reputation]")
{
    struct Mapping
    {
        ResponseOutcome evidence;
        SimpleReputationOutcome reputation;
        int delta;
        const char *evidence_name;
        const char *reputation_name;
    };
    const std::array<Mapping, 3> mappings{{
        {ResponseOutcome::on_time,
         SimpleReputationOutcome::response,
         1,
         "on_time",
         "response"},
        {ResponseOutcome::timeout,
         SimpleReputationOutcome::timeout,
         -1,
         "timeout",
         "timeout"},
        {ResponseOutcome::late,
         SimpleReputationOutcome::response,
         1,
         "late",
         "response"},
    }};

    for (std::size_t index = 0; index < mappings.size(); ++index)
    {
        CAPTURE(index);
        const auto &mapping = mappings[index];
        auto event = reputation_event();
        event.update.evidence_outcome = mapping.evidence;
        event.update.reputation_outcome = mapping.reputation;
        event.update.delta = mapping.delta;

        FakeClock clock({8100 + index});
        MemoryOutput output;
        StructuredEventSink sink(manager_event_config(), clock, output);
        sink.emit_audit(AuditStructuredEventPayload{event});
        sink.shutdown();

        CHECK(sink.health().healthy);
        CHECK(rendered(output).find(
                  std::string{"\"evidence_outcome\":\""} +
                  mapping.evidence_name + "\"") != std::string::npos);
        CHECK(rendered(output).find(
                  std::string{"\"reputation_outcome\":\""} +
                  mapping.reputation_name + "\"") != std::string::npos);
        CHECK(rendered(output).find(
                  std::string{"\"delta\":"} +
                  std::to_string(mapping.delta)) != std::string::npos);
    }
}

TEST_CASE("WE06-C04 serializes exact adaptive identity in canonical order",
          "[we06][c04][structured-event][adaptive][ndjson]")
{
    auto event = adaptive_event(
        AdaptiveAggregationTransition::required_branch_incomplete);
    event.rejection_reason = "reason-\"\\\n";
    const auto expected =
        "{\"event_schema_version\":1,"
        "\"run_id\":\"run-structured-event\","
        "\"source_kind\":\"replica\","
        "\"source_id\":\"replica-2\","
        "\"source_instance\":\"spawn-9\","
        "\"source_sequence\":1,"
        "\"source_monotonic_ns\":4321,"
        "\"event_type\":\"aggregation.required_branch_incomplete\","
        "\"payload\":{"
        "\"epoch_number\":9,"
        "\"tree_id\":2,"
        "\"epoch_digest\":\"" +
        event.configuration.epoch_digest.to_hex() + "\","
        "\"block_hash\":\"" + event.block_hash->to_hex() + "\","
        "\"context_generation\":17,"
        "\"observer_replica\":4,"
        "\"wait_exempt_signers\":[0,1],"
        "\"accepted_signers\":[2,3,4],"
        "\"absent_direct_children\":[0,1],"
        "\"missing_optional_signers\":[0,1],"
        "\"required_branch_gaps\":["
        "{\"direct_child\":2,\"missing_required_signers\":[5,6]},"
        "{\"direct_child\":3,\"missing_required_signers\":[7]}],"
        "\"root_signer_count\":3,"
        "\"global_quorum\":5,"
        "\"rejection_reason\":\"reason-\\\"\\\\\\n\"}}\n";

    FakeClock clock({4321});
    MemoryOutput output;
    StructuredEventSink sink(event_config(), clock, output);
    sink.emit_adaptive(event);
    const auto queued = sink.health();
    CHECK(queued.healthy);
    CHECK(queued.last_assigned_sequence == 1);
    CHECK(queued.queued_events == 1);
    CHECK(queued.queued_bytes == expected.size());
    CHECK(output.write_calls() == 0);

    sink.drain();
    CHECK(rendered(output) == expected);
    const auto parsed = hotstuff::parse_structured_event_prefix(
        output.bytes());
    CHECK(parsed.status == StructuredEventPrefixStatus::complete);
    CHECK(parsed.complete_records == 1);
    CHECK(parsed.complete_bytes == output.bytes().size());
}

TEST_CASE("WE06-C04 rejects noncanonical or incomplete adaptive payloads",
          "[we06][c04][structured-event][adaptive][validation]")
{
    const auto rejects = [](AdaptiveAggregationStructuredEvent event) {
        FakeClock clock({1000});
        MemoryOutput output;
        StructuredEventSink sink(event_config(), clock, output);
        sink.emit_adaptive(event);
        const auto failed = sink.health();
        const bool rejected =
            !failed.healthy && failed.stopped &&
            failed.first_failure ==
                StructuredEventFailure::invalid_payload &&
            failed.last_assigned_sequence == 0 &&
            failed.dropped_records == 1 && clock.calls() == 0 &&
            output.bytes().empty() && output.write_calls() == 0;
        sink.emit_adaptive(adaptive_event());
        sink.drain();
        return rejected && same_health(sink.health(), failed) &&
               clock.calls() == 0 && output.bytes().empty();
    };

    SECTION("transition and exact identities are mandatory")
    {
        auto invalid = adaptive_event();
        invalid.transition =
            static_cast<AdaptiveAggregationTransition>(0);
        CHECK(rejects(invalid));

        invalid = adaptive_event();
        invalid.configuration.epoch_digest = uint256_t{};
        CHECK(rejects(invalid));

        invalid = adaptive_event();
        invalid.block_hash.reset();
        CHECK(rejects(invalid));

        invalid = adaptive_event();
        invalid.block_hash = uint256_t{};
        CHECK(rejects(invalid));

        invalid = adaptive_event();
        invalid.context_generation.reset();
        CHECK(rejects(invalid));

        invalid = adaptive_event();
        invalid.context_generation = 0;
        CHECK(rejects(invalid));
    }

    SECTION("configuration identity excludes proposal identity")
    {
        auto invalid = adaptive_event(
            AdaptiveAggregationTransition::configuration_active);
        invalid.block_hash = digest("unexpected-block");
        CHECK(rejects(invalid));

        invalid = adaptive_event(
            AdaptiveAggregationTransition::configuration_active);
        invalid.context_generation = 1;
        CHECK(rejects(invalid));
    }

    SECTION("rejections and quorum observations carry their context")
    {
        auto invalid = adaptive_event(
            AdaptiveAggregationTransition::delta_rejected);
        invalid.rejection_reason.clear();
        CHECK(rejects(invalid));

        invalid = adaptive_event(
            AdaptiveAggregationTransition::proposal_aborted);
        invalid.rejection_reason.clear();
        CHECK(rejects(invalid));

        invalid = adaptive_event(
            AdaptiveAggregationTransition::delta_rejected);
        invalid.rejection_reason.assign(
            1, static_cast<char>(0xff));
        CHECK(rejects(invalid));

        for (const auto transition : {
                 AdaptiveAggregationTransition::configuration_active,
                 AdaptiveAggregationTransition::root_quorum_progress,
                 AdaptiveAggregationTransition::root_qc_published})
        {
            invalid = adaptive_event(transition);
            invalid.global_quorum = 0;
            CHECK(rejects(invalid));
        }

        invalid = adaptive_event(
            AdaptiveAggregationTransition::root_quorum_progress);
        ++invalid.root_signer_count;
        CHECK(rejects(invalid));

        invalid = adaptive_event(
            AdaptiveAggregationTransition::root_qc_published);
        ++invalid.global_quorum;
        CHECK(rejects(invalid));
    }

    SECTION("every signer sequence uses canonical increasing order")
    {
        auto invalid = adaptive_event();
        invalid.wait_exempt_signers = {1, 0};
        CHECK(rejects(invalid));

        invalid = adaptive_event();
        invalid.accepted_signers = {2, 2};
        CHECK(rejects(invalid));

        invalid = adaptive_event();
        invalid.absent_direct_children = {1, 0};
        CHECK(rejects(invalid));

        invalid = adaptive_event();
        invalid.missing_optional_signers = {0, 0};
        CHECK(rejects(invalid));

        invalid = adaptive_event();
        invalid.required_branch_gaps[0].missing_required_signers = {6, 5};
        CHECK(rejects(invalid));

        invalid = adaptive_event();
        invalid.required_branch_gaps[0].missing_required_signers.clear();
        CHECK(rejects(invalid));

        invalid = adaptive_event();
        std::swap(
            invalid.required_branch_gaps[0],
            invalid.required_branch_gaps[1]);
        CHECK(rejects(invalid));
    }

    SECTION("adaptive records remain bounded by the shared sink")
    {
        auto config = event_config();
        config.limits.maximum_line_bytes = 256;
        auto oversized = adaptive_event(
            AdaptiveAggregationTransition::delta_rejected);
        oversized.rejection_reason.assign(512, 'x');
        FakeClock clock({1000});
        MemoryOutput output;
        StructuredEventSink sink(config, clock, output);

        sink.emit_adaptive(oversized);
        const auto failed = sink.health();
        CHECK_FALSE(failed.healthy);
        CHECK(failed.first_failure ==
              StructuredEventFailure::line_too_large);
        CHECK(failed.last_assigned_sequence == 0);
        CHECK(failed.dropped_records == 1);
        CHECK(clock.calls() == 1);
        CHECK(output.bytes().empty());
        CHECK(output.write_calls() == 0);
    }
}

TEST_CASE("V13 rejects every invalid closed payload discriminator",
          "[v13][structured-event][payload][validation][intentional-red]")
{
    const auto expected_failure =
        invalid_payload_failure<StructuredEventFailure>(0);
    CHECK(expected_failure.has_value());

    SECTION("every invalid process lifecycle value fails before mapping")
    {
        const std::array<std::uint8_t, 6> valid{{
            static_cast<std::uint8_t>(ProcessLifecycleState::started),
            static_cast<std::uint8_t>(ProcessLifecycleState::ready),
            static_cast<std::uint8_t>(ProcessLifecycleState::stopping),
            static_cast<std::uint8_t>(ProcessLifecycleState::stopped),
            static_cast<std::uint8_t>(
                ProcessLifecycleState::forced_crash_requested),
            static_cast<std::uint8_t>(ProcessLifecycleState::exited),
        }};
        std::size_t checked = 0;
        bool all_rejected = true;
        bool all_explicit = expected_failure.has_value();
        bool all_unserialized = true;
        bool all_atomic = true;
        bool all_sticky = true;

        for (unsigned raw = 0;
             raw <= std::numeric_limits<std::uint8_t>::max();
             ++raw)
        {
            const auto value = static_cast<std::uint8_t>(raw);
            if (std::find(valid.begin(), valid.end(), value) != valid.end())
                continue;
            ++checked;

            FakeClock clock({3000});
            MemoryOutput output;
            StructuredEventSink sink(event_config(), clock, output);
            sink.emit(StructuredEventPayload{ProcessLifecycleEvent{
                static_cast<ProcessLifecycleState>(value), std::nullopt}});
            sink.drain();
            const auto failed = sink.health();
            all_rejected = all_rejected &&
                !failed.healthy && failed.stopped;
            all_explicit = all_explicit && expected_failure &&
                failed.first_failure == *expected_failure;
            all_unserialized = all_unserialized &&
                output.bytes().empty() && output.write_calls() == 0;
            all_atomic = all_atomic &&
                failed.last_assigned_sequence == 0 &&
                !failed.has_last_monotonic_ns &&
                failed.queued_events == 0 && failed.queued_bytes == 0 &&
                failed.complete_records == 0 &&
                failed.dropped_records == 1 && clock.calls() == 0;

            sink.emit(process_event(ProcessLifecycleState::ready));
            sink.drain();
            all_sticky = all_sticky && same_health(sink.health(), failed) &&
                output.bytes().empty() && clock.calls() == 0;
        }

        CHECK(checked == 250);
        CHECK(all_rejected);
        CHECK(all_explicit);
        CHECK(all_unserialized);
        CHECK(all_atomic);
        CHECK(all_sticky);
    }

    SECTION("every invalid epoch transition value fails before mapping")
    {
        const std::array<std::uint8_t, 5> valid{{
            static_cast<std::uint8_t>(EpochLifecycleTransition::generated),
            static_cast<std::uint8_t>(EpochLifecycleTransition::staged),
            static_cast<std::uint8_t>(EpochLifecycleTransition::acknowledged),
            static_cast<std::uint8_t>(
                EpochLifecycleTransition::activation_armed),
            static_cast<std::uint8_t>(EpochLifecycleTransition::activated),
        }};
        const auto epoch = configuration(9, 2, "invalid-transition");
        std::size_t checked = 0;
        bool all_rejected = true;
        bool all_explicit = expected_failure.has_value();
        bool all_unserialized = true;
        bool all_atomic = true;
        bool all_sticky = true;

        for (unsigned raw = 0;
             raw <= std::numeric_limits<std::uint8_t>::max();
             ++raw)
        {
            const auto value = static_cast<std::uint8_t>(raw);
            if (std::find(valid.begin(), valid.end(), value) != valid.end())
                continue;
            ++checked;

            FakeClock clock({3001});
            MemoryOutput output;
            StructuredEventSink sink(event_config(), clock, output);
            sink.emit(StructuredEventPayload{EpochLifecycleEvent{
                static_cast<EpochLifecycleTransition>(value), epoch, 500}});
            sink.drain();
            const auto failed = sink.health();
            all_rejected = all_rejected &&
                !failed.healthy && failed.stopped;
            all_explicit = all_explicit && expected_failure &&
                failed.first_failure == *expected_failure;
            all_unserialized = all_unserialized &&
                output.bytes().empty() && output.write_calls() == 0;
            all_atomic = all_atomic &&
                failed.last_assigned_sequence == 0 &&
                !failed.has_last_monotonic_ns &&
                failed.queued_events == 0 && failed.queued_bytes == 0 &&
                failed.complete_records == 0 &&
                failed.dropped_records == 1 && clock.calls() == 0;

            sink.emit(process_event(ProcessLifecycleState::ready));
            sink.drain();
            all_sticky = all_sticky && same_health(sink.health(), failed) &&
                output.bytes().empty() && clock.calls() == 0;
        }

        CHECK(checked == 251);
        CHECK(all_rejected);
        CHECK(all_explicit);
        CHECK(all_unserialized);
        CHECK(all_atomic);
        CHECK(all_sticky);
    }
}

TEST_CASE("V13 emits deterministic escaped integer-only commit NDJSON",
          "[v13][structured-event][ndjson][commit][intentional-red]")
{
    auto config = event_config();
    config.run_id = "run-\"\\\n\t";
    config.run_id.push_back('\x01');
    config.source.logical_id = "replica-\"\\\r\b\f";
    config.source.instance_id = "spawn-\n\t";
    config.source.instance_id.push_back('\x02');
    config.designated_commit_observer = config.source;
    const auto event = commit_event();
    const auto expected = expected_commit_line(event, 1, 1000);

    GlobalLocale grouped(std::locale(
        std::locale::classic(), new GroupedNumbers));
    FakeClock clock({1000});
    MemoryOutput output;
    StructuredEventSink sink(config, clock, output);
    StructuredEventEmitter &protocol = sink;

    protocol.emit(StructuredEventPayload{event});
    const auto queued = sink.health();
    CHECK(queued.healthy);
    CHECK_FALSE(queued.stopped);
    CHECK(queued.last_assigned_sequence == 1);
    CHECK(queued.last_monotonic_ns == 1000);
    CHECK(queued.queued_events == 1);
    CHECK(queued.queued_bytes == expected.size());
    CHECK(output.write_calls() == 0);
    CHECK(output.bytes().empty());

    sink.drain();
    CHECK(rendered(output) == expected);
    CHECK(std::count(output.bytes().begin(), output.bytes().end(), '\n') == 1);
    CHECK(std::find(output.bytes().begin(), output.bytes().end(), '\t') ==
          output.bytes().end());
    CHECK(rendered(output).find("1_234") == std::string::npos);
    CHECK(rendered(output).find("\"transaction_count\":7") !=
          std::string::npos);
    CHECK(rendered(output).find("\"transaction_count\":400") ==
          std::string::npos);
    CHECK(rendered(output).find("\"designated_observer\":true") !=
          std::string::npos);
    CHECK(rendered(output).find("\"decision_proof\":{") !=
          std::string::npos);
    CHECK(rendered(output).find("proposal_configuration") ==
          std::string::npos);

    const auto drained = sink.health();
    CHECK(drained.healthy);
    CHECK(drained.complete_records == 1);
    CHECK(drained.queued_events == 0);
    CHECK(drained.queued_bytes == 0);
}

TEST_CASE("V13 derives designated commit observer from exact source config",
          "[v13][structured-event][commit][observer][intentional-red]")
{
    const auto event = commit_event();

    SECTION("an exact source match is designated")
    {
        auto config = event_config();
        FakeClock clock({1001});
        MemoryOutput output;
        StructuredEventSink sink(config, clock, output);
        sink.emit(StructuredEventPayload{event});
        sink.shutdown();
        CHECK(rendered(output).find("\"designated_observer\":true") !=
              std::string::npos);
    }

    SECTION("kind id instance and absence each prevent designation")
    {
        for (std::size_t mismatch = 0; mismatch < 4; ++mismatch)
        {
            CAPTURE(mismatch);
            auto config = event_config();
            if (mismatch == 0)
                config.designated_commit_observer->kind =
                    StructuredEventSourceKind::orchestrator;
            else if (mismatch == 1)
                config.designated_commit_observer->logical_id = "replica-3";
            else if (mismatch == 2)
                config.designated_commit_observer->instance_id = "spawn-10";
            else
                config.designated_commit_observer = std::nullopt;

            FakeClock clock({1002 + mismatch});
            MemoryOutput output;
            StructuredEventSink sink(config, clock, output);
            sink.emit(StructuredEventPayload{event});
            sink.shutdown();
            CHECK(rendered(output).find("\"designated_observer\":false") !=
                  std::string::npos);
            CHECK(rendered(output).find("\"designated_observer\":true") ==
                  std::string::npos);
        }
    }
}

TEST_CASE("adaptive commit witness serializes without proposal metadata",
          "[adaptive-v2][structured-event][commit-observed][schema]")
{
    auto event = commit_observed_event();
    const auto expected = expected_commit_observed_line(event, 1, 1100);
    FakeClock clock({1100});
    MemoryOutput output;
    StructuredEventSink sink(event_config(), clock, output);

    sink.emit(StructuredEventPayload{event});
    sink.shutdown();

    CHECK(rendered(output) == expected);
    CHECK(rendered(output).find("\"decision_proof\"") ==
          std::string::npos);
    CHECK(rendered(output).find("\"view_generation\"") ==
          std::string::npos);
    CHECK(rendered(output).find("\"designated_observer\"") ==
          std::string::npos);

    SECTION("a missing parent remains an explicit null")
    {
        event.parent_hash.reset();
        FakeClock null_clock({1101});
        MemoryOutput null_output;
        StructuredEventSink null_sink(event_config(), null_clock, null_output);
        null_sink.emit(StructuredEventPayload{event});
        null_sink.shutdown();
        CHECK(rendered(null_output).find("\"parent_hash\":null") !=
              std::string::npos);
    }
}

TEST_CASE(
    "unavailable commit identity is a closed observer-agnostic disposition",
    "[adaptive-v2][structured-event][commit-identity-unavailable][schema]")
{
    auto event = commit_identity_unavailable_event();
    const auto expected =
        expected_commit_identity_unavailable_line(event, 1, 1110);
    FakeClock clock({1110});
    MemoryOutput output;
    StructuredEventSink sink(event_config(), clock, output);

    sink.emit(StructuredEventPayload{event});
    sink.shutdown();

    CHECK(rendered(output) == expected);
    CHECK(rendered(output).find("\"decision_proof\"") ==
          std::string::npos);
    CHECK(rendered(output).find("\"view_generation\"") ==
          std::string::npos);
    CHECK(rendered(output).find("\"designated_observer\"") ==
          std::string::npos);
    CHECK(rendered(output).find("\"identity_source\"") ==
          std::string::npos);

    SECTION("designated observer configuration cannot change the payload")
    {
        auto config = event_config();
        config.designated_commit_observer = StructuredEventSource{
            StructuredEventSourceKind::replica,
            "another-replica",
            "another-spawn"};
        FakeClock mismatch_clock({1111});
        MemoryOutput mismatch_output;
        StructuredEventSink mismatch_sink(
            config, mismatch_clock, mismatch_output);
        mismatch_sink.emit(StructuredEventPayload{event});
        mismatch_sink.shutdown();
        CHECK(rendered(mismatch_output).find(
                  "\"event_type\":\"block.commit_identity_unavailable\"") !=
              std::string::npos);
        CHECK(rendered(mismatch_output).find(
                  "\"designated_observer\"") == std::string::npos);
    }

    SECTION("the sealed reason cannot drift")
    {
        event.reason = static_cast<CommitIdentityUnavailableReason>(2);
        FakeClock invalid_clock({1112});
        MemoryOutput invalid_output;
        StructuredEventSink invalid_sink(
            event_config(), invalid_clock, invalid_output);
        invalid_sink.emit(StructuredEventPayload{event});
        const auto health = invalid_sink.health();
        CHECK_FALSE(health.healthy);
        CHECK(health.first_failure ==
              StructuredEventFailure::invalid_payload);
        CHECK(invalid_output.bytes().empty());
    }

    SECTION("a convergence-pending unavailable disposition is forbidden")
    {
        event.convergence_identity_pending = true;
        FakeClock invalid_clock({1113});
        MemoryOutput invalid_output;
        StructuredEventSink invalid_sink(
            event_config(), invalid_clock, invalid_output);
        invalid_sink.emit(StructuredEventPayload{event});
        const auto health = invalid_sink.health();
        CHECK_FALSE(health.healthy);
        CHECK(health.first_failure ==
              StructuredEventFailure::invalid_payload);
        CHECK(invalid_output.bytes().empty());
    }
}

TEST_CASE("V13 validates per-field and aggregate identity bytes atomically",
          "[v13][structured-event][identity][bounds][intentional-red]")
{
    SECTION("zero per-field or aggregate capacity is invalid")
    {
        for (std::size_t zero_limit = 0; zero_limit < 2; ++zero_limit)
        {
            CAPTURE(zero_limit);
            auto config = compact_identity_config();
            if (zero_limit == 0)
                config.limits.maximum_identity_bytes = 0;
            else
                config.limits.maximum_total_identity_bytes = 0;
            FakeClock clock({2000});
            MemoryOutput output;
            StructuredEventSink sink(config, clock, output);
            const auto failed = sink.health();
            CHECK_FALSE(failed.healthy);
            CHECK(failed.stopped);
            CHECK(failed.first_failure ==
                  StructuredEventFailure::invalid_configuration);
            CHECK(failed.last_assigned_sequence == 0);
            CHECK(failed.queued_events == 0);
            sink.emit(process_event());
            CHECK(same_health(sink.health(), failed));
            CHECK(clock.calls() == 0);
            CHECK(output.write_calls() == 0);
        }
    }

    SECTION("raw identity byte limits are inclusive")
    {
        auto config = compact_identity_config();
        FakeClock clock({2001});
        MemoryOutput output;
        StructuredEventSink sink(config, clock, output);
        sink.emit(process_event());
        CHECK(sink.health().healthy);
        CHECK(sink.health().last_assigned_sequence == 1);
        sink.shutdown();
        CHECK(complete_lines(output.bytes()).size() == 1);
    }

    SECTION("each identity field is independently bounded")
    {
        for (std::size_t field = 0; field < 5; ++field)
        {
            CAPTURE(field);
            auto config = compact_identity_config();
            config.limits.maximum_total_identity_bytes = 100;
            if (field == 0)
                config.run_id = "rrrrr";
            else if (field == 1)
                config.source.logical_id = "sssss";
            else if (field == 2)
                config.source.instance_id = "iiiii";
            else if (field == 3)
                config.designated_commit_observer->logical_id = "ooooo";
            else
                config.designated_commit_observer->instance_id = "ppppp";

            FakeClock clock({2002});
            MemoryOutput output;
            StructuredEventSink sink(config, clock, output);
            const auto failed = sink.health();
            CHECK_FALSE(failed.healthy);
            CHECK(failed.stopped);
            CHECK(failed.first_failure ==
                  StructuredEventFailure::identity_too_large);
            CHECK(failed.last_assigned_sequence == 0);
            CHECK_FALSE(failed.has_last_monotonic_ns);
            CHECK(failed.queued_events == 0);
            CHECK(failed.queued_bytes == 0);
            sink.emit(process_event());
            CHECK(same_health(sink.health(), failed));
            CHECK(clock.calls() == 0);
            CHECK(output.write_calls() == 0);
        }
    }

    SECTION("aggregate identity bytes reject one byte over")
    {
        auto config = compact_identity_config();
        config.limits.maximum_total_identity_bytes = 19;
        FakeClock clock({2003});
        MemoryOutput output;
        StructuredEventSink sink(config, clock, output);
        const auto failed = sink.health();
        CHECK_FALSE(failed.healthy);
        CHECK(failed.stopped);
        CHECK(failed.first_failure ==
              StructuredEventFailure::identity_too_large);
        CHECK(failed.last_assigned_sequence == 0);
        CHECK(failed.queued_events == 0);
        CHECK(failed.queued_bytes == 0);
        sink.emit(process_event());
        CHECK(same_health(sink.health(), failed));
        CHECK(clock.calls() == 0);
        CHECK(output.write_calls() == 0);
    }
}

TEST_CASE("V13 validates canonical nonempty UTF-8 identities",
          "[v13][structured-event][identity][utf8][intentional-red]")
{
    SECTION("every required identity component is nonempty")
    {
        bool all_rejected = true;
        bool all_atomic = true;
        bool all_sticky = true;
        for (std::size_t field = 0; field < 5; ++field)
        {
            CAPTURE(field);
            auto config = event_config();
            set_identity_field(config, field, "");
            FakeClock clock({2100});
            MemoryOutput output;
            StructuredEventSink sink(config, clock, output);
            const auto failed = sink.health();
            all_rejected = all_rejected && !failed.healthy &&
                failed.stopped && failed.first_failure ==
                    StructuredEventFailure::invalid_configuration;
            all_atomic = all_atomic &&
                failed.last_assigned_sequence == 0 &&
                !failed.has_last_monotonic_ns &&
                failed.queued_events == 0 && failed.queued_bytes == 0 &&
                clock.calls() == 0 && output.write_calls() == 0;
            sink.emit(process_event());
            all_sticky = all_sticky && same_health(sink.health(), failed) &&
                clock.calls() == 0 && output.write_calls() == 0;
        }
        CHECK(all_rejected);
        CHECK(all_atomic);
        CHECK(all_sticky);
    }

    SECTION("valid two three and four byte code points round-trip unchanged")
    {
        auto config = event_config();
        config.run_id = u8"corrida-ação-東京-🙂";
        config.source.logical_id = u8"réplica-二";
        config.source.instance_id = u8"instância-λ-🚀";
        config.designated_commit_observer = config.source;
        FakeClock clock({2101});
        MemoryOutput output;
        StructuredEventSink sink(config, clock, output);
        sink.emit(process_event());
        sink.shutdown();

        const auto lines = complete_lines(output.bytes());
        REQUIRE(lines.size() == 1);
        const auto &line = lines.front();
        CHECK(line.find("\"run_id\":\"" + config.run_id + "\"") !=
              std::string::npos);
        CHECK(line.find(
                  "\"source_id\":\"" + config.source.logical_id + "\"") !=
              std::string::npos);
        CHECK(line.find("\"source_instance\":\"" +
                        config.source.instance_id + "\"") !=
              std::string::npos);
        CHECK(line.find("\\u00") == std::string::npos);
        const auto parsed = hotstuff::parse_structured_event_prefix(
            output.bytes());
        CHECK(parsed.status == StructuredEventPrefixStatus::complete);
        CHECK(parsed.complete_records == 1);
    }

    SECTION("JSON controls remain escaped next to unchanged UTF-8")
    {
        auto config = event_config();
        config.run_id = u8"ação";
        config.run_id += "\n\t\"\\";
        FakeClock clock({2102});
        MemoryOutput output;
        StructuredEventSink sink(config, clock, output);
        sink.emit(process_event());
        sink.shutdown();

        const auto lines = complete_lines(output.bytes());
        REQUIRE(lines.size() == 1);
        CHECK(lines.front().find(
                  std::string{"\"run_id\":\""} + u8"ação" +
                  "\\n\\t\\\"\\\\\"") != std::string::npos);
        CHECK(lines.front().find('\n') == lines.front().size() - 1);
        CHECK(lines.front().find('\t') == std::string::npos);
    }

    SECTION("malformed overlong surrogate and out-of-range UTF-8 are rejected")
    {
        const std::array<std::pair<const char *, std::string>, 8> invalid{{
            {"lone continuation", raw_bytes({0x80})},
            {"truncated sequence", raw_bytes({0xe2, 0x82})},
            {"bad continuation", raw_bytes({0xe2, 0x28, 0xa1})},
            {"overlong two byte", raw_bytes({0xc0, 0xaf})},
            {"overlong three byte", raw_bytes({0xe0, 0x80, 0xaf})},
            {"surrogate", raw_bytes({0xed, 0xa0, 0x80})},
            {"out of range", raw_bytes({0xf4, 0x90, 0x80, 0x80})},
            {"invalid lead", raw_bytes({0xf5, 0x80, 0x80, 0x80})},
        }};
        std::size_t checked = 0;
        bool all_rejected = true;
        bool all_atomic = true;
        bool all_sticky = true;
        for (std::size_t field = 0; field < 5; ++field)
        {
            for (const auto &sample : invalid)
            {
                CAPTURE(field);
                CAPTURE(sample.first);
                ++checked;
                auto config = event_config();
                set_identity_field(config, field, "bad-" + sample.second);
                FakeClock clock({2103});
                MemoryOutput output;
                StructuredEventSink sink(config, clock, output);
                const auto failed = sink.health();
                all_rejected = all_rejected && !failed.healthy &&
                    failed.stopped && failed.first_failure ==
                        StructuredEventFailure::invalid_configuration;
                all_atomic = all_atomic &&
                    failed.last_assigned_sequence == 0 &&
                    !failed.has_last_monotonic_ns &&
                    failed.queued_events == 0 &&
                    failed.queued_bytes == 0 && clock.calls() == 0 &&
                    output.write_calls() == 0;
                sink.emit(process_event());
                all_sticky = all_sticky &&
                    same_health(sink.health(), failed) &&
                    clock.calls() == 0 && output.write_calls() == 0;
            }
        }
        CHECK(checked == 40);
        CHECK(all_rejected);
        CHECK(all_atomic);
        CHECK(all_sticky);
    }
}

TEST_CASE("V13 assigns strict source sequence and nondecreasing injected time",
          "[v13][structured-event][ordering][intentional-red]")
{
    SECTION("fresh sources start at one and equal timestamps are valid")
    {
        FakeClock clock({100, 100, 101});
        MemoryOutput output;
        StructuredEventSink sink(event_config(), clock, output);
        StructuredEventEmitter &protocol = sink;

        protocol.emit(process_event(ProcessLifecycleState::started));
        protocol.emit(process_event(ProcessLifecycleState::ready));
        protocol.emit(process_event(ProcessLifecycleState::stopping));
        sink.drain();

        const auto lines = complete_lines(output.bytes());
        REQUIRE(lines.size() == 3);
        CHECK(lines[0].find("\"source_sequence\":1") != std::string::npos);
        CHECK(lines[1].find("\"source_sequence\":2") != std::string::npos);
        CHECK(lines[2].find("\"source_sequence\":3") != std::string::npos);
        CHECK(lines[0].find("\"source_monotonic_ns\":100") !=
              std::string::npos);
        CHECK(lines[1].find("\"source_monotonic_ns\":100") !=
              std::string::npos);
        CHECK(lines[2].find("\"source_monotonic_ns\":101") !=
              std::string::npos);
        CHECK(sink.health().last_assigned_sequence == 3);
        CHECK(sink.health().complete_records == 3);
    }

    SECTION("clock regression fails closed without consuming sequence")
    {
        FakeClock clock({100, 99, 101});
        MemoryOutput output;
        StructuredEventSink sink(event_config(), clock, output);
        StructuredEventEmitter &protocol = sink;

        protocol.emit(process_event());
        protocol.emit(process_event(ProcessLifecycleState::ready));
        const auto failed = sink.health();
        REQUIRE_FALSE(failed.healthy);
        CHECK(failed.stopped);
        CHECK(failed.first_failure == StructuredEventFailure::clock_regression);
        CHECK(failed.last_assigned_sequence == 1);
        CHECK(failed.last_monotonic_ns == 100);
        CHECK(failed.queued_events == 1);
        CHECK(failed.dropped_records == 1);

        protocol.emit(process_event(ProcessLifecycleState::stopped));
        CHECK(clock.calls() == 2);
        CHECK(same_health(sink.health(), failed));
        sink.shutdown();
        CHECK(complete_lines(output.bytes()).size() == 1);
    }

    SECTION("a persisted cursor continues sequence and equal time")
    {
        const auto config = event_config();
        StructuredEventCursor cursor;
        cursor.last_source_sequence = 41;
        cursor.has_last_monotonic_ns = true;
        cursor.last_monotonic_ns = 900;
        CursorSourceTokenContract<StructuredEventCursor>::bind(cursor, config);
        FakeClock clock({900, 901});
        MemoryOutput output;
        StructuredEventSink sink(config, clock, output, cursor);

        sink.emit(process_event());
        sink.emit(process_event(ProcessLifecycleState::ready));
        sink.shutdown();
        const auto lines = complete_lines(output.bytes());
        REQUIRE(lines.size() == 2);
        CHECK(lines[0].find("\"source_sequence\":42") != std::string::npos);
        CHECK(lines[1].find("\"source_sequence\":43") != std::string::npos);
        CHECK(lines[0].find("\"source_monotonic_ns\":900") !=
              std::string::npos);
        CHECK(lines[1].find("\"source_monotonic_ns\":901") !=
              std::string::npos);
    }

    SECTION("a persisted timestamp rejects the first regressing event")
    {
        const auto config = event_config();
        StructuredEventCursor cursor;
        cursor.last_source_sequence = 41;
        cursor.has_last_monotonic_ns = true;
        cursor.last_monotonic_ns = 900;
        CursorSourceTokenContract<StructuredEventCursor>::bind(cursor, config);
        FakeClock clock({899});
        MemoryOutput output;
        StructuredEventSink sink(config, clock, output, cursor);

        sink.emit(process_event());
        const auto failed = sink.health();
        CHECK_FALSE(failed.healthy);
        CHECK(failed.stopped);
        CHECK(failed.first_failure == StructuredEventFailure::clock_regression);
        CHECK(failed.last_assigned_sequence == 41);
        CHECK(failed.has_last_monotonic_ns);
        CHECK(failed.last_monotonic_ns == 900);
        CHECK(failed.queued_events == 0);
        CHECK(failed.queued_bytes == 0);
        CHECK(clock.calls() == 1);
        CHECK(output.write_calls() == 0);
    }

    SECTION("sequence exhaustion is detected before clock or serialization")
    {
        const auto config = event_config();
        FakeClock clock({500});
        MemoryOutput output;
        StructuredEventCursor cursor;
        cursor.last_source_sequence =
            std::numeric_limits<std::uint64_t>::max();
        cursor.has_last_monotonic_ns = true;
        cursor.last_monotonic_ns = 500;
        CursorSourceTokenContract<StructuredEventCursor>::bind(cursor, config);
        StructuredEventSink sink(config, clock, output, cursor);

        sink.emit(process_event());
        const auto health = sink.health();
        CHECK_FALSE(health.healthy);
        CHECK(health.stopped);
        CHECK(health.first_failure ==
              StructuredEventFailure::sequence_exhausted);
        CHECK(health.last_assigned_sequence ==
              std::numeric_limits<std::uint64_t>::max());
        CHECK(health.queued_events == 0);
        CHECK(clock.calls() == 0);
        CHECK(output.write_calls() == 0);
    }
}

TEST_CASE("V13 accepts only coherent source-bound resume cursors",
          "[v13][structured-event][cursor][identity][intentional-red]")
{
    CHECK(CursorSourceTokenContract<StructuredEventCursor>::available);

    SECTION("resume history requires coherent time and an exact source token")
    {
        for (std::size_t mismatch = 0; mismatch < 3; ++mismatch)
        {
            CAPTURE(mismatch);
            const auto config = event_config();
            StructuredEventCursor cursor;
            if (mismatch != 2)
            {
                CursorSourceTokenContract<StructuredEventCursor>::bind(
                    cursor, config);
            }
            if (mismatch == 0)
            {
                cursor.last_source_sequence = 41;
            }
            else if (mismatch == 1)
            {
                cursor.has_last_monotonic_ns = true;
                cursor.last_monotonic_ns = 900;
            }
            else
            {
                cursor.last_source_sequence = 41;
                cursor.has_last_monotonic_ns = true;
                cursor.last_monotonic_ns = 900;
            }

            FakeClock clock({900});
            MemoryOutput output;
            StructuredEventSink sink(config, clock, output, cursor);
            const auto failed = sink.health();
            CHECK_FALSE(failed.healthy);
            CHECK(failed.stopped);
            CHECK(failed.first_failure ==
                  StructuredEventFailure::invalid_configuration);
            CHECK(failed.last_assigned_sequence == 0);
            CHECK_FALSE(failed.has_last_monotonic_ns);
            CHECK(failed.last_monotonic_ns == 0);
            CHECK(failed.queued_events == 0);
            CHECK(failed.queued_bytes == 0);
            CHECK(clock.calls() == 0);
            CHECK(output.write_calls() == 0);

            sink.emit(process_event());
            sink.drain();
            CHECK(same_health(sink.health(), failed));
            CHECK(clock.calls() == 0);
            CHECK(output.write_calls() == 0);
        }
    }

    SECTION("a token for the exact run source and instance resumes")
    {
        const auto config = event_config();
        StructuredEventCursor cursor;
        cursor.last_source_sequence = 41;
        cursor.has_last_monotonic_ns = true;
        cursor.last_monotonic_ns = 900;
        CursorSourceTokenContract<StructuredEventCursor>::bind(cursor, config);
        FakeClock clock({900});
        MemoryOutput output;
        StructuredEventSink sink(config, clock, output, cursor);
        CHECK(sink.health().healthy);
        CHECK(sink.health().last_assigned_sequence == 41);
        CHECK(sink.health().has_last_monotonic_ns);
        CHECK(sink.health().last_monotonic_ns == 900);
        sink.emit(process_event());
        sink.shutdown();
        const auto lines = complete_lines(output.bytes());
        REQUIRE(lines.size() == 1);
        CHECK(lines.front().find("\"source_sequence\":42") !=
              std::string::npos);
    }

    SECTION("a token cannot be reused across any source identity component")
    {
        for (std::size_t mismatch = 0; mismatch < 4; ++mismatch)
        {
            CAPTURE(mismatch);
            const auto original = event_config();
            StructuredEventCursor cursor;
            cursor.last_source_sequence = 41;
            cursor.has_last_monotonic_ns = true;
            cursor.last_monotonic_ns = 900;
            CursorSourceTokenContract<StructuredEventCursor>::bind(
                cursor, original);

            auto resumed = original;
            if (mismatch == 0)
                resumed.run_id = "another-run";
            else if (mismatch == 1)
                resumed.source.kind =
                    StructuredEventSourceKind::adaptation_manager;
            else if (mismatch == 2)
                resumed.source.logical_id = "replica-3";
            else
                resumed.source.instance_id = "spawn-10";

            FakeClock clock({900});
            MemoryOutput output;
            StructuredEventSink sink(resumed, clock, output, cursor);
            const auto failed = sink.health();
            CHECK_FALSE(failed.healthy);
            CHECK(failed.stopped);
            CHECK(failed.first_failure ==
                  StructuredEventFailure::invalid_configuration);
            CHECK(failed.last_assigned_sequence == 0);
            CHECK_FALSE(failed.has_last_monotonic_ns);
            CHECK(failed.last_monotonic_ns == 0);
            CHECK(failed.queued_events == 0);
            CHECK(failed.queued_bytes == 0);
            CHECK(clock.calls() == 0);
            CHECK(output.write_calls() == 0);

            sink.emit(process_event());
            sink.drain();
            CHECK(same_health(sink.health(), failed));
            CHECK(clock.calls() == 0);
            CHECK(output.write_calls() == 0);
        }
    }
}

TEST_CASE("V13 enforces independent event byte and line bounds atomically",
          "[v13][structured-event][bounds][intentional-red]")
{
    const auto payload = process_event();
    std::size_t line_bytes = 0;
    {
        FakeClock clock({10});
        MemoryOutput output;
        StructuredEventSink probe(event_config(), clock, output);
        probe.emit(payload);
        line_bytes = probe.health().queued_bytes;
        REQUIRE(line_bytes > 1);
        probe.shutdown();
    }

    SECTION("zero capacity is invalid before the first event")
    {
        auto config = event_config();
        config.limits.maximum_queued_events = 0;
        FakeClock clock({10});
        MemoryOutput output;
        StructuredEventSink sink(config, clock, output);
        const auto before = sink.health();
        CHECK_FALSE(before.healthy);
        CHECK(before.stopped);
        CHECK(before.first_failure ==
              StructuredEventFailure::invalid_configuration);
        sink.emit(payload);
        CHECK(same_health(sink.health(), before));
        CHECK(clock.calls() == 0);
    }

    SECTION("line limit rejects the whole record")
    {
        auto config = event_config();
        config.limits.maximum_line_bytes = line_bytes - 1;
        FakeClock clock({10});
        MemoryOutput output;
        StructuredEventSink sink(config, clock, output);
        sink.emit(payload);
        const auto health = sink.health();
        CHECK_FALSE(health.healthy);
        CHECK(health.first_failure == StructuredEventFailure::line_too_large);
        CHECK(health.last_assigned_sequence == 0);
        CHECK(health.queued_events == 0);
        CHECK(health.queued_bytes == 0);
        CHECK(output.bytes().empty());
    }

    SECTION("event capacity preserves only the accepted prefix")
    {
        auto config = event_config();
        config.limits.maximum_queued_events = 1;
        FakeClock clock({10, 11});
        MemoryOutput output;
        StructuredEventSink sink(config, clock, output);
        sink.emit(payload);
        sink.emit(process_event(ProcessLifecycleState::ready));
        const auto failed = sink.health();
        CHECK_FALSE(failed.healthy);
        CHECK(failed.first_failure == StructuredEventFailure::queue_full);
        CHECK(failed.last_assigned_sequence == 1);
        CHECK(failed.queued_events == 1);
        CHECK(failed.queued_bytes == line_bytes);
        sink.shutdown();
        CHECK(complete_lines(output.bytes()).size() == 1);
    }

    SECTION("queued byte maximum is inclusive and one byte less fails")
    {
        auto exact_config = event_config();
        exact_config.limits.maximum_queued_bytes = line_bytes;
        FakeClock exact_clock({10});
        MemoryOutput exact_output;
        StructuredEventSink exact(
            exact_config, exact_clock, exact_output);
        exact.emit(payload);
        CHECK(exact.health().healthy);
        CHECK(exact.health().queued_bytes == line_bytes);

        auto short_config = event_config();
        short_config.limits.maximum_queued_bytes = line_bytes - 1;
        FakeClock short_clock({10});
        MemoryOutput short_output;
        StructuredEventSink short_sink(
            short_config, short_clock, short_output);
        short_sink.emit(payload);
        const auto failed = short_sink.health();
        CHECK_FALSE(failed.healthy);
        CHECK(failed.first_failure == StructuredEventFailure::queue_full);
        CHECK(failed.last_assigned_sequence == 0);
        CHECK(failed.queued_events == 0);
        CHECK(failed.queued_bytes == 0);
    }
}

TEST_CASE("V13 retries EINTR and short writes without duplicating a record",
          "[v13][structured-event][output][intentional-red]")
{
    FakeClock expected_clock({20});
    MemoryOutput expected_output;
    StructuredEventSink expected_sink(
        event_config(), expected_clock, expected_output);
    expected_sink.emit(process_event());
    expected_sink.shutdown();
    const auto expected = expected_output.bytes();

    FakeClock clock({20});
    MemoryOutput output({
        {StructuredEventWriteStatus::interrupted, 0},
        {StructuredEventWriteStatus::progress, 3},
        {StructuredEventWriteStatus::progress, 5},
    });
    StructuredEventSink sink(event_config(), clock, output);
    sink.emit(process_event());
    CHECK(output.write_calls() == 0);
    sink.drain();

    CHECK(output.bytes() == expected);
    CHECK(output.write_calls() >= 4);
    CHECK(sink.health().healthy);
    CHECK(sink.health().complete_records == 1);
    CHECK_FALSE(sink.health().interrupted_tail);
}

TEST_CASE("AE01 production monotonic clock uses the raw clock domain",
          "[adaptive-v2][structured-event][clock][production]")
{
    MonotonicRawStructuredEventClock clock;
    const auto first = clock.now_ns();
    const auto second = clock.now_ns();

    CHECK(clock.healthy());
    CHECK(first > 0);
    CHECK(second >= first);
}

TEST_CASE("AE01 sink rejects a production clock health failure atomically",
          "[adaptive-v2][structured-event][clock][failure]")
{
    FailingClock clock;
    MemoryOutput output;
    StructuredEventSink sink(event_config(), clock, output);
    sink.emit(process_event());

    const auto failed = sink.health();
    CHECK_FALSE(failed.healthy);
    CHECK(failed.stopped);
    CHECK(failed.first_failure == StructuredEventFailure::clock_failure);
    CHECK(failed.last_assigned_sequence == 0);
    CHECK_FALSE(failed.has_last_monotonic_ns);
    CHECK(failed.dropped_records == 1);
    CHECK(failed.queued_events == 0);
    CHECK(failed.queued_bytes == 0);
    CHECK(clock.calls() == 1);
    CHECK(output.write_calls() == 0);
}

TEST_CASE("AE01 file output creates one durable raw JSONL artifact",
          "[adaptive-v2][structured-event][output][file]")
{
    TemporaryDirectory temporary;
    const auto path = temporary.file("replica-2.events.jsonl");
    bytearray_t original;

    {
        ScopedUmask restrictive_umask{0777};
        ExclusiveFileStructuredEventOutput output(path);
        CHECK(output.is_open());
        FakeClock clock({9000});
        {
            StructuredEventSink sink(event_config(), clock, output);
            sink.emit(process_event());
        }
        CHECK_FALSE(output.is_open());
        CHECK(output.healthy());
    }

    original = read_file(path);
    REQUIRE_FALSE(original.empty());
    const auto parsed = hotstuff::parse_structured_event_prefix(original);
    CHECK(parsed.status == StructuredEventPrefixStatus::complete);
    CHECK(parsed.complete_records == 1);
    CHECK(parsed.complete_bytes == original.size());

    struct stat metadata{};
    REQUIRE(::stat(path.c_str(), &metadata) == 0);
    CHECK((metadata.st_mode & 0777) == 0600);

    CHECK_THROWS_AS(
        ExclusiveFileStructuredEventOutput(path),
        std::system_error);
    CHECK(read_file(path) == original);
}

TEST_CASE("AE01 output sync failure invalidates the run before close",
          "[adaptive-v2][structured-event][output][sync][failure]")
{
    FakeClock clock({9001});
    MemoryOutput output({}, true, false);
    StructuredEventSink sink(event_config(), clock, output);
    sink.emit(process_event());
    sink.shutdown();

    const auto failed = sink.health();
    CHECK_FALSE(failed.healthy);
    CHECK(failed.stopped);
    CHECK(failed.first_failure == StructuredEventFailure::sync_failure);
    CHECK(failed.complete_records == 1);
    CHECK(output.sync_calls() == 1);
    CHECK(output.close_calls() == 1);

    sink.shutdown();
    CHECK(output.sync_calls() == 1);
    CHECK(output.close_calls() == 1);
}

TEST_CASE("V13 output failure is permanent and leaves only a final tail",
          "[v13][structured-event][output][failure][intentional-red]")
{
    SECTION("hard failure before progress produces no bytes")
    {
        FakeClock clock({30});
        MemoryOutput output({
            {StructuredEventWriteStatus::failure, 0},
        });
        StructuredEventSink sink(event_config(), clock, output);
        sink.emit(process_event());
        sink.drain();
        const auto failed = sink.health();
        CHECK_FALSE(failed.healthy);
        CHECK(failed.stopped);
        CHECK(failed.first_failure == StructuredEventFailure::write_failure);
        CHECK_FALSE(failed.interrupted_tail);
        CHECK(failed.complete_records == 0);
        CHECK(failed.queued_events == 0);
        CHECK(output.bytes().empty());

        const auto calls = output.write_calls();
        sink.emit(process_event(ProcessLifecycleState::ready));
        sink.drain();
        CHECK(output.write_calls() == calls);
        CHECK(rendered(output).empty());
        sink.shutdown();
        CHECK(output.close_calls() == 1);
        sink.shutdown();
        CHECK(output.close_calls() == 1);
    }

    SECTION("partial second record is the permanent final tail")
    {
        std::size_t first_line_bytes = 0;
        {
            FakeClock probe_clock({40});
            MemoryOutput probe_output;
            StructuredEventSink probe(
                event_config(), probe_clock, probe_output);
            probe.emit(process_event());
            first_line_bytes = probe.health().queued_bytes;
            probe.shutdown();
        }

        FakeClock clock({40, 41, 42});
        MemoryOutput output({
            {StructuredEventWriteStatus::progress, first_line_bytes},
            {StructuredEventWriteStatus::progress, 5},
            {StructuredEventWriteStatus::failure, 0},
        });
        StructuredEventSink sink(event_config(), clock, output);
        sink.emit(process_event());
        sink.emit(process_event(ProcessLifecycleState::ready));
        sink.drain();

        const auto failed = sink.health();
        CHECK_FALSE(failed.healthy);
        CHECK(failed.stopped);
        CHECK(failed.first_failure == StructuredEventFailure::write_failure);
        CHECK(failed.interrupted_tail);
        CHECK(failed.complete_records == 1);
        CHECK(failed.queued_events == 0);
        CHECK(std::count(
                  output.bytes().begin(), output.bytes().end(), '\n') == 1);

        const auto prefix = hotstuff::parse_structured_event_prefix(
            output.bytes());
        CHECK(prefix.status ==
              StructuredEventPrefixStatus::interrupted_tail);
        CHECK(prefix.complete_records == 1);
        CHECK(prefix.complete_bytes == first_line_bytes);

        const auto before = output.bytes();
        const auto writes = output.write_calls();
        sink.emit(process_event(ProcessLifecycleState::stopped));
        sink.drain();
        sink.shutdown();
        CHECK(output.bytes() == before);
        CHECK(output.write_calls() == writes);
        CHECK(output.close_calls() == 1);
        sink.shutdown();
        CHECK(output.close_calls() == 1);
    }
}

TEST_CASE("V13 shutdown drains once and reports close failure",
          "[v13][structured-event][shutdown][intentional-red]")
{
    SECTION("healthy shutdown drains FIFO and is idempotent")
    {
        FakeClock clock({50, 51});
        MemoryOutput output;
        StructuredEventSink sink(event_config(), clock, output);
        sink.emit(process_event());
        sink.emit(process_event(ProcessLifecycleState::ready));
        REQUIRE(output.bytes().empty());

        sink.shutdown();
        const auto stopped = sink.health();
        CHECK(stopped.healthy);
        CHECK(stopped.stopped);
        CHECK(stopped.complete_records == 2);
        CHECK(stopped.queued_events == 0);
        CHECK(complete_lines(output.bytes()).size() == 2);
        CHECK(output.close_calls() == 1);

        const auto bytes = output.bytes();
        const auto writes = output.write_calls();
        const auto clock_calls = clock.calls();
        sink.emit(process_event(ProcessLifecycleState::stopped));
        sink.drain();
        CHECK(same_health(sink.health(), stopped));
        CHECK(clock.calls() == clock_calls);
        CHECK(output.bytes() == bytes);
        CHECK(output.write_calls() == writes);
        CHECK(output.close_calls() == 1);
        sink.shutdown();
        CHECK(output.bytes() == bytes);
        CHECK(output.write_calls() == writes);
        CHECK(output.close_calls() == 1);
    }

    SECTION("close failure is sticky after all complete records")
    {
        FakeClock clock({50});
        MemoryOutput output({}, false);
        StructuredEventSink sink(event_config(), clock, output);
        sink.emit(process_event());
        sink.shutdown();
        const auto failed = sink.health();
        CHECK_FALSE(failed.healthy);
        CHECK(failed.stopped);
        CHECK(failed.first_failure == StructuredEventFailure::close_failure);
        CHECK(failed.complete_records == 1);
        CHECK_FALSE(failed.interrupted_tail);
        CHECK(complete_lines(output.bytes()).size() == 1);
        sink.emit(process_event(ProcessLifecycleState::ready));
        sink.drain();
        sink.shutdown();
        CHECK(same_health(sink.health(), failed));
        CHECK(output.close_calls() == 1);
    }

    SECTION("destruction performs one final drain and close")
    {
        FakeClock clock({52});
        MemoryOutput output;
        {
            StructuredEventSink sink(event_config(), clock, output);
            sink.emit(process_event());
            CHECK(output.bytes().empty());
            CHECK(output.close_calls() == 0);
        }
        CHECK(complete_lines(output.bytes()).size() == 1);
        CHECK(output.close_calls() == 1);
    }

    SECTION("destruction after explicit shutdown does not close twice")
    {
        FakeClock clock({53});
        MemoryOutput output;
        {
            StructuredEventSink sink(event_config(), clock, output);
            sink.emit(process_event());
            sink.shutdown();
            CHECK(output.close_calls() == 1);
        }
        CHECK(output.close_calls() == 1);
    }
}

TEST_CASE("V13 rejects reentrant borrowed clock and output callbacks",
          "[v13][structured-event][ownership][reentrant][intentional-red]")
{
    const auto expected_failure =
        reentrant_call_failure<StructuredEventFailure>(0);
    CHECK(expected_failure.has_value());

    SECTION("clock callback reentry admits no duplicate sequence or record")
    {
        ReentrantProducerClock clock({54, 54});
        MemoryOutput output;
        StructuredEventSink sink(event_config(), clock, output);
        StructuredEventEmitter &producer = sink;
        StructuredEventDrainOwner &writer = sink;
        const auto nested = process_event(ProcessLifecycleState::ready);
        clock.arm(producer, nested);

        producer.emit(process_event(ProcessLifecycleState::started));
        REQUIRE(clock.reentered());
        const auto failed = writer.health();
        CHECK_FALSE(failed.healthy);
        CHECK(failed.stopped);
        CHECK(expected_failure.has_value());
        if (expected_failure)
            CHECK(failed.first_failure == *expected_failure);
        CHECK(failed.last_assigned_sequence == 0);
        CHECK_FALSE(failed.has_last_monotonic_ns);
        CHECK(failed.last_monotonic_ns == 0);
        CHECK(failed.queued_events == 0);
        CHECK(failed.queued_bytes == 0);
        CHECK(failed.complete_records == 0);
        CHECK(output.bytes().empty());
        CHECK(output.write_calls() == 0);
        CHECK(clock.calls() == 1);

        producer.emit(process_event(ProcessLifecycleState::stopped));
        writer.drain();
        CHECK(same_health(writer.health(), failed));
        CHECK(clock.calls() == 1);
        CHECK(output.bytes().empty());
        CHECK(output.write_calls() == 0);
        writer.shutdown();
        CHECK(output.close_calls() == 1);
    }

    SECTION("output callback reentry cannot duplicate or partially write")
    {
        FakeClock clock({55, 56});
        ReentrantOwnerOutput output;
        StructuredEventSink sink(event_config(), clock, output);
        StructuredEventEmitter &producer = sink;
        StructuredEventDrainOwner &writer = sink;

        producer.emit(process_event(ProcessLifecycleState::started));
        REQUIRE(writer.health().healthy);
        REQUIRE(writer.health().last_assigned_sequence == 1);
        REQUIRE(writer.health().queued_events == 1);
        output.arm(writer);
        writer.drain();
        REQUIRE(output.reentered());

        const auto failed = writer.health();
        CHECK_FALSE(failed.healthy);
        CHECK(failed.stopped);
        CHECK(expected_failure.has_value());
        if (expected_failure)
            CHECK(failed.first_failure == *expected_failure);
        CHECK(failed.last_assigned_sequence == 1);
        CHECK(failed.has_last_monotonic_ns);
        CHECK(failed.last_monotonic_ns == 55);
        CHECK(failed.queued_events == 0);
        CHECK(failed.queued_bytes == 0);
        CHECK(failed.complete_records == 0);
        CHECK_FALSE(failed.interrupted_tail);
        CHECK(clock.calls() == 1);
        CHECK(output.write_calls() == 1);
        CHECK(output.bytes().empty());

        producer.emit(process_event(ProcessLifecycleState::stopped));
        writer.drain();
        CHECK(same_health(writer.health(), failed));
        CHECK(clock.calls() == 1);
        CHECK(output.write_calls() == 1);
        CHECK(output.bytes().empty());
        writer.shutdown();
        CHECK(output.close_calls() == 1);
    }
}

TEST_CASE("V13 parser recovers only a complete NDJSON prefix",
          "[v13][structured-event][parser][intentional-red]")
{
    FakeClock clock({60, 61});
    MemoryOutput output;
    StructuredEventSink sink(event_config(), clock, output);
    sink.emit(process_event());
    sink.emit(process_event(ProcessLifecycleState::ready));
    sink.shutdown();

    const auto complete = hotstuff::parse_structured_event_prefix(
        output.bytes());
    CHECK(complete.status == StructuredEventPrefixStatus::complete);
    CHECK(complete.complete_records == 2);
    CHECK(complete.complete_bytes == output.bytes().size());

    auto interrupted = output.bytes();
    const std::string tail{"{\"event_schema_version\":1"};
    interrupted.insert(interrupted.end(), tail.begin(), tail.end());
    const auto recovered = hotstuff::parse_structured_event_prefix(
        interrupted);
    CHECK(recovered.status ==
          StructuredEventPrefixStatus::interrupted_tail);
    CHECK(recovered.complete_records == 2);
    CHECK(recovered.complete_bytes == output.bytes().size());

    auto malformed = output.bytes();
    const std::string invalid{"not-json\n"};
    malformed.insert(malformed.end(), invalid.begin(), invalid.end());
    const auto rejected = hotstuff::parse_structured_event_prefix(malformed);
    CHECK(rejected.status ==
          StructuredEventPrefixStatus::malformed_record);
    CHECK(rejected.complete_records == 2);
    CHECK(rejected.complete_bytes == output.bytes().size());

    SECTION("empty input is a complete zero-record prefix")
    {
        const auto empty = hotstuff::parse_structured_event_prefix({});
        CHECK(empty.status == StructuredEventPrefixStatus::complete);
        CHECK(empty.complete_records == 0);
        CHECK(empty.complete_bytes == 0);
    }

    SECTION("newline completion is distinct from JSON syntax completion")
    {
        const auto lines = complete_lines(output.bytes());
        REQUIRE_FALSE(lines.empty());
        const auto with_newline = hotstuff::parse_structured_event_prefix(
            bytes_of(lines.front()));
        CHECK(with_newline.status == StructuredEventPrefixStatus::complete);
        CHECK(with_newline.complete_records == 1);
        CHECK(with_newline.complete_bytes == lines.front().size());

        auto without_newline = lines.front();
        REQUIRE(without_newline.back() == '\n');
        without_newline.pop_back();
        const auto final_tail = hotstuff::parse_structured_event_prefix(
            bytes_of(without_newline));
        CHECK(final_tail.status ==
              StructuredEventPrefixStatus::interrupted_tail);
        CHECK(final_tail.complete_records == 0);
        CHECK(final_tail.complete_bytes == 0);
    }

    SECTION("complete malformed JSON records are rejected by syntax")
    {
        const std::array<std::string, 4> malformed_records{{
            "{\"x\":\"\\q\"}\n",
            "{\"x\":tru}\n",
            "{\"x\":[1,2}\n",
            "{\"x\":{\"y\":1}\n",
        }};
        for (const auto &record : malformed_records)
        {
            CAPTURE(record);
            const auto result = hotstuff::parse_structured_event_prefix(
                bytes_of(record));
            CHECK(result.status ==
                  StructuredEventPrefixStatus::malformed_record);
            CHECK(result.complete_records == 0);
            CHECK(result.complete_bytes == 0);
        }
    }

    SECTION("malformed middle record stops before later valid records")
    {
        const auto lines = complete_lines(output.bytes());
        REQUIRE(lines.size() == 2);
        std::string records = lines[0];
        records += "{\"x\":]\n";
        records += lines[1];
        const auto result = hotstuff::parse_structured_event_prefix(
            bytes_of(records));
        CHECK(result.status ==
              StructuredEventPrefixStatus::malformed_record);
        CHECK(result.complete_records == 1);
        CHECK(result.complete_bytes == lines[0].size());
    }

    SECTION("prefix recovery validates JSON syntax rather than event schema")
    {
        const std::string syntactic_json{
            "{\"not_the_event_schema\":true}\n"};
        const auto result = hotstuff::parse_structured_event_prefix(
            bytes_of(syntactic_json));
        CHECK(result.status == StructuredEventPrefixStatus::complete);
        CHECK(result.complete_records == 1);
        CHECK(result.complete_bytes == syntactic_json.size());
    }
}

TEST_CASE("V13 noexcept drain and parser fail on allocation at whole prefixes",
          "[v13][structured-event][allocation][prefix][intentional-red]")
{
    static_assert(noexcept(std::declval<StructuredEventDrainOwner &>().drain()),
                  "writer drain is a noexcept protocol boundary");
    using PrefixParser = hotstuff::StructuredEventPrefixResult (*)(
        const bytearray_t &) noexcept;
    static_assert(std::is_same<
                      decltype(&hotstuff::parse_structured_event_prefix),
                      PrefixParser>::value,
                  "prefix recovery is a noexcept artifact boundary");

    SECTION("drain preserves the complete prefix and stops after allocation")
    {
        constexpr std::size_t sweep = 32;
        std::size_t successful_drains = 0;
        for (std::size_t prefix = 0; prefix < sweep; ++prefix)
        {
            CAPTURE(prefix);
            FakeClock clock({80, 81, 82});
            MemoryOutput output;
            output.reserve(128 * 1024);
            StructuredEventSink sink(event_config(), clock, output);
            sink.emit(process_event(ProcessLifecycleState::started));
            sink.drain();
            const auto prior = output.bytes();
            REQUIRE(complete_lines(prior).size() == 1);
            sink.emit(process_event(ProcessLifecycleState::ready));

            bool injected = false;
            {
                structured_event_allocation_failure::OneShot fault(prefix);
                sink.drain();
                injected = fault.triggered();
            }

            const auto parsed = hotstuff::parse_structured_event_prefix(
                output.bytes());
            REQUIRE(output.bytes().size() >= prior.size());
            CHECK(std::equal(
                prior.begin(), prior.end(), output.bytes().begin()));
            CHECK(parsed.status !=
                  StructuredEventPrefixStatus::malformed_record);
            CHECK(parsed.complete_records >= 1);
            CHECK(parsed.complete_bytes >= prior.size());

            if (injected)
            {
                const auto failed = sink.health();
                CHECK_FALSE(failed.healthy);
                CHECK(failed.stopped);
                CHECK(failed.first_failure ==
                      StructuredEventFailure::allocation_failure);
                const auto bytes = output.bytes();
                sink.emit(process_event(ProcessLifecycleState::stopped));
                sink.drain();
                CHECK(same_health(sink.health(), failed));
                CHECK(output.bytes() == bytes);
            }
            else
            {
                ++successful_drains;
                CHECK(sink.health().healthy);
                CHECK(sink.health().complete_records == 2);
                CHECK(parsed.status == StructuredEventPrefixStatus::complete);
                CHECK(parsed.complete_records == 2);
            }
            sink.shutdown();
        }
        CHECK(successful_drains > 0);
    }

    SECTION("parser reports allocation failure only at record boundaries")
    {
        constexpr std::size_t sweep = 64;
        const std::string first{"{\"one\":1}\n"};
        const std::string second{"{\"two\":2}\n"};
        const auto records = bytes_of(first + second);
        std::size_t successful_parses = 0;
        for (std::size_t prefix = 0; prefix < sweep; ++prefix)
        {
            CAPTURE(prefix);
            hotstuff::StructuredEventPrefixResult result;
            bool injected = false;
            {
                structured_event_allocation_failure::OneShot fault(prefix);
                result = hotstuff::parse_structured_event_prefix(records);
                injected = fault.triggered();
            }

            if (injected)
            {
                CHECK(result.status ==
                      StructuredEventPrefixStatus::allocation_failure);
                CHECK(result.status !=
                      StructuredEventPrefixStatus::malformed_record);
                const bool whole_prefix =
                    (result.complete_records == 0 &&
                     result.complete_bytes == 0) ||
                    (result.complete_records == 1 &&
                     result.complete_bytes == first.size()) ||
                    (result.complete_records == 2 &&
                     result.complete_bytes == records.size());
                CHECK(whole_prefix);
            }
            else
            {
                ++successful_parses;
                CHECK(result.status == StructuredEventPrefixStatus::complete);
                CHECK(result.complete_records == 2);
                CHECK(result.complete_bytes == records.size());
            }
        }
        CHECK(successful_parses > 0);
    }
}

TEST_CASE("V13 allocation prefixes expose either one whole event or none",
          "[v13][structured-event][allocation][intentional-red]")
{
    constexpr std::size_t allocation_sweep = 64;
    std::size_t injected_failures = 0;
    std::size_t complete_prefixes = 0;

    for (std::size_t prefix = 0; prefix < allocation_sweep; ++prefix)
    {
        CAPTURE(prefix);
        auto config = event_config();
        FakeClock clock({70});
        MemoryOutput output;
        StructuredEventSink sink(config, clock, output);
        const auto payload = process_event();
        bool injected = false;
        {
            structured_event_allocation_failure::OneShot fault(prefix);
            sink.emit(payload);
            injected = fault.triggered();
        }

        if (injected)
        {
            ++injected_failures;
            const auto failed = sink.health();
            CHECK_FALSE(failed.healthy);
            CHECK(failed.stopped);
            CHECK(failed.first_failure ==
                  StructuredEventFailure::allocation_failure);
            CHECK(failed.last_assigned_sequence == 0);
            CHECK_FALSE(failed.has_last_monotonic_ns);
            CHECK(failed.queued_events == 0);
            CHECK(failed.queued_bytes == 0);
            CHECK(failed.complete_records == 0);
            CHECK(output.write_calls() == 0);

            sink.emit(process_event(ProcessLifecycleState::ready));
            CHECK(same_health(sink.health(), failed));
            CHECK(output.write_calls() == 0);
        }
        else
        {
            ++complete_prefixes;
            const auto accepted = sink.health();
            CHECK(accepted.healthy);
            CHECK_FALSE(accepted.stopped);
            CHECK(accepted.last_assigned_sequence == 1);
            CHECK(accepted.has_last_monotonic_ns);
            CHECK(accepted.last_monotonic_ns == 70);
            CHECK(accepted.queued_events == 1);
            CHECK(accepted.queued_bytes > 0);
            sink.shutdown();
            CHECK(complete_lines(output.bytes()).size() == 1);
        }
    }

    CAPTURE(injected_failures);
    CAPTURE(complete_prefixes);
    REQUIRE(injected_failures > 0);
    REQUIRE(complete_prefixes > 0);
}

TEST_CASE(
    "fault contribution opportunity serializes exact prospective identity",
    "[adaptive-v2][experiment][fault-opportunity][structured-event]"
    "[intentional-red]")
{
    const auto event = contribution_opportunity_event();
    const AuditStructuredEventPayload payload{event};
    CHECK(hotstuff::structured_event_type(payload) ==
          StructuredEventType::fault_contribution_opportunity);
    CHECK(std::string(hotstuff::structured_event_type_name(
              StructuredEventType::fault_contribution_opportunity)) ==
          "fault.contribution_opportunity");

    auto config = event_config();
    config.source.logical_id = "replica-2";
    config.designated_commit_observer.reset();
    FakeClock clock({500});
    MemoryOutput output;
    StructuredEventSink sink(config, clock, output);
    sink.emit_audit(payload);
    sink.drain();

    const auto record = rendered(output);
    CHECK(record.find("\"event_type\":\"fault.contribution_opportunity\"") !=
          std::string::npos);
    CHECK(record.find("\"source_id\":\"replica-2\"") !=
          std::string::npos);
    CHECK(record.find("\"actor\":2") != std::string::npos);
    CHECK(record.find("\"epoch_number\":7") != std::string::npos);
    CHECK(record.find("\"tree_id\":3") != std::string::npos);
    CHECK(record.find(event.proposal.configuration.epoch_digest.to_hex()) !=
          std::string::npos);
    CHECK(record.find(event.proposal.block_hash.to_hex()) !=
          std::string::npos);
    CHECK(record.find("\"view_generation\":19") != std::string::npos);
    CHECK(record.find("\"physical_role\":\"internal\"") !=
          std::string::npos);
    CHECK(record.find("\"parent_replica\":0") != std::string::npos);
    CHECK(record.find("\"expected_message_type\":\"aggregate_relay\"") !=
          std::string::npos);
    CHECK(record.find("\"cohort\":\"responsive_degraded\"") !=
          std::string::npos);
    CHECK(record.find("\"diagnostic_window\":\"factorial-window-1\"") !=
          std::string::npos);
    CHECK(record.find("\"window_start_monotonic_ns\":100") !=
          std::string::npos);
    CHECK(record.find("\"window_end_monotonic_ns\":200") !=
          std::string::npos);
    CHECK(record.find("\"decision_monotonic_ns\":150") !=
          std::string::npos);
    CHECK(record.find("\"contribution_ordinal\":81") !=
          std::string::npos);
    CHECK(record.find("\"role_contribution_ordinal\":41") !=
          std::string::npos);
    CHECK(record.find("\"scheduled_action\":\"omit_aggregate\"") !=
          std::string::npos);
    CHECK(record.find("\"responsive_omission_period\":41") !=
          std::string::npos);
    CHECK(record.find("\"fault_threshold\":10") != std::string::npos);
    CHECK(record.find("\"hard_actor_count\":3") != std::string::npos);
    CHECK(record.find("\"responsive_degraded_actor_count\":7") !=
          std::string::npos);
    CHECK(record.find(
              "\"fault_mode\":\"tiered_persistent_responsive_omission_v2\"") !=
          std::string::npos);
    CHECK(sink.health().complete_records == 1);

    auto mismatched_source = config;
    mismatched_source.source.logical_id = "replica-3";
    FakeClock rejected_clock({501});
    MemoryOutput rejected_output;
    StructuredEventSink rejected_sink(
        mismatched_source, rejected_clock, rejected_output);
    rejected_sink.emit_audit(payload);
    const auto rejected = rejected_sink.health();
    CHECK_FALSE(rejected.healthy);
    CHECK(rejected.stopped);
    CHECK(rejected.first_failure ==
          StructuredEventFailure::invalid_payload);
    CHECK(rejected.last_assigned_sequence == 0);
    CHECK(rejected_output.bytes().empty());

    const auto rejects = [&config](
                             FaultContributionOpportunityStructuredEvent
                                 invalid) {
        FakeClock invalid_clock({502});
        MemoryOutput invalid_output;
        StructuredEventSink invalid_sink(
            config, invalid_clock, invalid_output);
        invalid_sink.emit_audit(
            AuditStructuredEventPayload{std::move(invalid)});
        const auto failed = invalid_sink.health();
        return !failed.healthy && failed.stopped &&
               failed.first_failure ==
                   StructuredEventFailure::invalid_payload &&
               failed.last_assigned_sequence == 0 &&
               invalid_output.bytes().empty();
    };

    auto contradictory_forward = event;
    contradictory_forward.scheduled_action =
        ExperimentOmissionAction::forward;
    CHECK(rejects(std::move(contradictory_forward)));

    auto contradictory_omission = event;
    contradictory_omission.role_contribution_ordinal = 40;
    CHECK(rejects(std::move(contradictory_omission)));

    auto impossible_role_ordinal = event;
    impossible_role_ordinal.role_contribution_ordinal = 82;
    CHECK(rejects(std::move(impossible_role_ordinal)));
}

TEST_CASE(
    "blocked root QC serializes exact source-bound queue evidence",
    "[adaptive-v2][pipeline][root-qc-queue-blocked][structured-event]"
    "[intentional-red]")
{
    const auto event = root_qc_queue_blocked_event();
    const AuditStructuredEventPayload payload{event};
    CHECK(hotstuff::kStructuredEventSchemaVersion == 1);
    CHECK(hotstuff::structured_event_type(payload) ==
          StructuredEventType::pipeline_root_qc_queue_blocked);
    CHECK(std::string(hotstuff::structured_event_type_name(
              StructuredEventType::pipeline_root_qc_queue_blocked)) ==
          "pipeline.root_qc_queue_blocked");

    auto config = event_config();
    config.source.logical_id = "replica-0";
    config.designated_commit_observer.reset();
    FakeClock clock({600});
    MemoryOutput output;
    StructuredEventSink sink(config, clock, output);
    sink.emit_audit(payload);
    sink.shutdown();

    const auto expected =
        "{\"event_schema_version\":1,"
        "\"run_id\":\"run-structured-event\","
        "\"source_kind\":\"replica\","
        "\"source_id\":\"replica-0\","
        "\"source_instance\":\"spawn-9\","
        "\"source_sequence\":1,"
        "\"source_monotonic_ns\":600,"
        "\"event_type\":\"pipeline.root_qc_queue_blocked\","
        "\"payload\":{"
        "\"epoch_number\":2,"
        "\"tree_id\":0,"
        "\"epoch_digest\":\"" +
        event.configuration.epoch_digest.to_hex() + "\","
        "\"observer_replica\":0,"
        "\"global_quorum\":21,"
        "\"queue_head_position\":0,"
        "\"queued_candidate_position\":1,"
        "\"queue_head_context_generation\":40,"
        "\"queued_candidate_context_generation\":41,"
        "\"queue_head_block_height\":100,"
        "\"queue_head_block_hash\":\"" +
        event.queue_head_block_hash.to_hex() + "\","
        "\"queued_candidate_block_height\":101,"
        "\"queued_candidate_block_hash\":\"" +
        event.queued_candidate_block_hash.to_hex() + "\","
        "\"queued_candidate_parent_hash\":\"" +
        event.queued_candidate_parent_hash.to_hex() + "\","
        "\"queue_head_signer_count\":20,"
        "\"queued_candidate_signer_count\":21,"
        "\"queued_candidate_qc_ready\":true,"
        "\"queued_candidate_qc_published\":false}}\n";
    CHECK(rendered(output) == expected);
    CHECK(sink.health().healthy);
    CHECK(sink.health().complete_records == 1);

    const auto rejects = [](StructuredEventConfig invalid_config,
                            RootQcQueueBlockedStructuredEvent invalid) {
        FakeClock invalid_clock({601});
        MemoryOutput invalid_output;
        StructuredEventSink invalid_sink(
            std::move(invalid_config), invalid_clock, invalid_output);
        invalid_sink.emit_audit(
            AuditStructuredEventPayload{std::move(invalid)});
        const auto failed = invalid_sink.health();
        return !failed.healthy && failed.stopped &&
               failed.first_failure ==
                   StructuredEventFailure::invalid_payload &&
               failed.last_assigned_sequence == 0 &&
               invalid_output.bytes().empty();
    };

    auto mismatched_source = config;
    mismatched_source.source.logical_id = "replica-1";
    CHECK(rejects(std::move(mismatched_source), event));

    auto invalid = event;
    invalid.queue_head_signer_count = invalid.global_quorum;
    CHECK(rejects(config, std::move(invalid)));

    invalid = event;
    invalid.queued_candidate_signer_count = invalid.global_quorum - 1;
    CHECK(rejects(config, std::move(invalid)));

    invalid = event;
    invalid.queued_candidate_position = 2;
    CHECK(rejects(config, std::move(invalid)));

    invalid = event;
    invalid.queued_candidate_parent_hash = digest("wrong-parent");
    CHECK(rejects(config, std::move(invalid)));

    invalid = event;
    invalid.queued_candidate_qc_ready = false;
    CHECK(rejects(config, std::move(invalid)));

    invalid = event;
    invalid.queued_candidate_qc_published = true;
    CHECK(rejects(config, std::move(invalid)));
}
