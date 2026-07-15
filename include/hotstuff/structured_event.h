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

#include "hotstuff/configuration.h"

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

using StructuredEventPayload = std::variant<
    ProcessLifecycleEvent,
    EpochLifecycleEvent,
    CommitStructuredEvent>;

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
