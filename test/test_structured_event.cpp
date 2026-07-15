#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <initializer_list>
#include <limits>
#include <locale>
#include <memory>
#include <new>
#include <optional>
#include <string>
#include <type_traits>
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
 * phase deliberately has no HotStuffCore hook, background writer, socket,
 * filesystem, or adaptation-manager dependency.
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

using hotstuff::CommitStructuredEvent;
using hotstuff::ConfigurationId;
using hotstuff::DataStream;
using hotstuff::EpochLifecycleEvent;
using hotstuff::EpochLifecycleTransition;
using hotstuff::ProcessLifecycleEvent;
using hotstuff::ProcessLifecycleState;
using hotstuff::ProposalKey;
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
        bool close_result = true)
        : actions_(std::move(actions)), close_result_(close_result)
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

    void reserve(std::size_t bytes)
    {
        bytes_.reserve(bytes);
    }

private:
    std::vector<WriteAction> actions_;
    bool close_result_{true};
    bytearray_t bytes_;
    std::size_t next_action_{0};
    std::size_t write_calls_{0};
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

} // namespace

TEST_CASE("V13 exposes a closed payload-only protocol emitter",
          "[v13][structured-event][contract][intentional-red]")
{
    CHECK(KAURI_HAS_STRUCTURED_EVENT_API == 1);
    CHECK(hotstuff::kStructuredEventSchemaVersion == 1);

    static_assert(std::variant_size<StructuredEventPayload>::value == 3,
                  "phase one has only process, epoch and commit payloads");
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

#if defined(HOTSTUFF_PROTO_LOG)
    INFO("the same structured contract is exercised with human logs enabled");
#else
    INFO("the structured contract remains active with human logs disabled");
#endif
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
