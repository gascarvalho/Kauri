#include "hotstuff/response_attempt.h"

#include <algorithm>
#include <limits>
#include <map>
#include <type_traits>
#include <utility>

namespace hotstuff
{
namespace
{

constexpr std::uint64_t kNanosecondsPerMicrosecond = 1000;

enum class AttemptPhase : std::uint8_t
{
    waiting,
    timed_out,
    completed_on_time,
    completed_late,
};

struct ResponseAttemptKeyLess
{
    bool operator()(const ResponseAttemptKey &left,
                    const ResponseAttemptKey &right) const noexcept
    {
        if (left.proposal < right.proposal)
            return true;
        if (right.proposal < left.proposal)
            return false;
        if (left.observed_replica_id != right.observed_replica_id)
        {
            return left.observed_replica_id <
                   right.observed_replica_id;
        }
        using MessageType =
            std::underlying_type_t<ExpectedMessageType>;
        return static_cast<MessageType>(left.expected_message_type) <
               static_cast<MessageType>(right.expected_message_type);
    }
};

struct AttemptState
{
    std::uint64_t start_monotonic_ns{0};
    std::uint64_t deadline_duration_us{0};
    std::uint64_t absolute_deadline_ns{0};
    std::uint64_t generation{0};
    std::uint64_t timeout_monotonic_ns{0};
    AttemptPhase phase{AttemptPhase::waiting};
};

bool valid_message_type(ExpectedMessageType type) noexcept
{
    return type == ExpectedMessageType::direct_vote ||
           type == ExpectedMessageType::aggregate_relay;
}

bool compute_absolute_deadline(
    const ResponseAttemptArm &attempt,
    std::uint64_t &absolute_deadline_ns) noexcept
{
    if (attempt.start_monotonic_ns == 0 ||
        attempt.deadline_duration_us == 0 ||
        attempt.deadline_duration_us >
            std::numeric_limits<std::uint64_t>::max() /
                kNanosecondsPerMicrosecond)
    {
        return false;
    }
    const auto duration_ns =
        attempt.deadline_duration_us * kNanosecondsPerMicrosecond;
    if (attempt.start_monotonic_ns >
        std::numeric_limits<std::uint64_t>::max() - duration_ns)
    {
        return false;
    }
    absolute_deadline_ns = attempt.start_monotonic_ns + duration_ns;
    return true;
}

bool canonical_signers(
    const ResponseAttemptKey &key,
    const std::vector<ReplicaID> &signers,
    std::size_t maximum_signers) noexcept
{
    if (signers.empty() || signers.size() > maximum_signers)
        return false;
    if (std::adjacent_find(
            signers.begin(), signers.end(),
            [](ReplicaID left, ReplicaID right) {
                return left >= right;
            }) != signers.end())
    {
        return false;
    }
    if (key.expected_message_type == ExpectedMessageType::direct_vote)
    {
        return signers.size() == 1 &&
               signers.front() == key.observed_replica_id;
    }
    return key.expected_message_type ==
           ExpectedMessageType::aggregate_relay;
}

std::uint64_t elapsed_microseconds(
    std::uint64_t start_ns,
    std::uint64_t event_ns) noexcept
{
    return (event_ns - start_ns) /
           kNanosecondsPerMicrosecond;
}

} // namespace

struct ResponseAttemptTracker::State
{
    explicit State(ResponseAttemptLimits configured_limits)
        : limits(configured_limits)
    {
        if (limits.maximum_attempts == 0 ||
            limits.maximum_signers_per_response == 0 ||
            limits.maximum_generation == 0)
        {
            healthy = false;
        }
    }

    ResponseAttemptLimits limits;
    std::map<ResponseAttemptKey, AttemptState, ResponseAttemptKeyLess>
        attempts;
    std::uint64_t last_generation{0};
    bool healthy{true};
    bool stopped{false};
};

ResponseAttemptTracker::ResponseAttemptTracker(
    ResponseAttemptLimits limits)
    : state_(std::make_unique<State>(limits))
{}

ResponseAttemptTracker::~ResponseAttemptTracker() = default;

std::optional<ResponseAttemptHandle>
ResponseAttemptTracker::arm(const ResponseAttemptArm &attempt)
{
    if (state_->stopped)
        return std::nullopt;

    std::uint64_t absolute_deadline_ns = 0;
    if (!valid_message_type(attempt.key.expected_message_type) ||
        !compute_absolute_deadline(attempt, absolute_deadline_ns))
    {
        state_->healthy = false;
        return std::nullopt;
    }

    const auto existing = state_->attempts.find(attempt.key);
    if (existing != state_->attempts.end())
    {
        const auto &stored = existing->second;
        if (stored.start_monotonic_ns != attempt.start_monotonic_ns ||
            stored.deadline_duration_us !=
                attempt.deadline_duration_us)
        {
            state_->healthy = false;
        }
        return std::nullopt;
    }

    if (state_->attempts.size() >= state_->limits.maximum_attempts)
    {
        state_->healthy = false;
        return std::nullopt;
    }

    if (state_->last_generation >= state_->limits.maximum_generation)
    {
        state_->healthy = false;
        return std::nullopt;
    }

    const auto generation = state_->last_generation + 1;
    try
    {
        const auto inserted = state_->attempts.emplace(
            attempt.key,
            AttemptState{
                attempt.start_monotonic_ns,
                attempt.deadline_duration_us,
                absolute_deadline_ns,
                generation,
                0,
                AttemptPhase::waiting});
        if (!inserted.second)
        {
            state_->healthy = false;
            return std::nullopt;
        }
        state_->last_generation = generation;
        return ResponseAttemptHandle{attempt.key, generation};
    }
    catch (...)
    {
        state_->healthy = false;
        return std::nullopt;
    }
}

std::optional<ResponseAttemptFact>
ResponseAttemptTracker::record_response(
    const ResponseAttemptHandle &handle,
    std::uint64_t response_monotonic_ns,
    const std::vector<ReplicaID> &canonical_verified_signers)
{
    const auto found = state_->attempts.find(handle.key);
    if (found == state_->attempts.end())
        return std::nullopt;

    auto &attempt = found->second;
    if (attempt.generation != handle.generation)
        return std::nullopt;
    if (attempt.phase == AttemptPhase::completed_on_time ||
        attempt.phase == AttemptPhase::completed_late)
    {
        return std::nullopt;
    }
    if (response_monotonic_ns < attempt.start_monotonic_ns ||
        !canonical_signers(
            handle.key,
            canonical_verified_signers,
            state_->limits.maximum_signers_per_response))
    {
        state_->healthy = false;
        return std::nullopt;
    }

    const bool is_late = attempt.phase == AttemptPhase::timed_out;
    if (is_late &&
        (response_monotonic_ns < attempt.absolute_deadline_ns ||
         response_monotonic_ns < attempt.timeout_monotonic_ns))
    {
        state_->healthy = false;
        return std::nullopt;
    }

    try
    {
        std::optional<ResponseAttemptFact> fact{
            std::in_place,
            ResponseAttemptFact{
                handle.key,
                is_late ? ResponseOutcome::late
                        : ResponseOutcome::on_time,
                elapsed_microseconds(
                    attempt.start_monotonic_ns,
                    response_monotonic_ns),
                attempt.deadline_duration_us,
                response_monotonic_ns,
                canonical_verified_signers}};
        attempt.phase = is_late
                            ? AttemptPhase::completed_late
                            : AttemptPhase::completed_on_time;
        return fact;
    }
    catch (...)
    {
        state_->healthy = false;
        return std::nullopt;
    }
}

std::optional<ResponseAttemptFact>
ResponseAttemptTracker::record_timeout(
    const ResponseAttemptHandle &handle,
    std::uint64_t timeout_monotonic_ns)
{
    const auto found = state_->attempts.find(handle.key);
    if (found == state_->attempts.end())
        return std::nullopt;

    auto &attempt = found->second;
    if (attempt.generation != handle.generation)
        return std::nullopt;
    if (attempt.phase != AttemptPhase::waiting)
        return std::nullopt;
    if (timeout_monotonic_ns < attempt.start_monotonic_ns ||
        timeout_monotonic_ns < attempt.absolute_deadline_ns)
    {
        state_->healthy = false;
        return std::nullopt;
    }

    try
    {
        std::optional<ResponseAttemptFact> fact{
            std::in_place,
            ResponseAttemptFact{
                handle.key,
                ResponseOutcome::timeout,
                0,
                attempt.deadline_duration_us,
                timeout_monotonic_ns,
                {}}};
        attempt.timeout_monotonic_ns = timeout_monotonic_ns;
        attempt.phase = AttemptPhase::timed_out;
        return fact;
    }
    catch (...)
    {
        state_->healthy = false;
        return std::nullopt;
    }
}

std::size_t ResponseAttemptTracker::retire(
    const ProposalKey &proposal) noexcept
{
    std::size_t retired = 0;
    for (auto iterator = state_->attempts.begin();
         iterator != state_->attempts.end();)
    {
        if (iterator->first.proposal == proposal)
        {
            iterator = state_->attempts.erase(iterator);
            ++retired;
        }
        else
        {
            ++iterator;
        }
    }
    return retired;
}

void ResponseAttemptTracker::shutdown() noexcept
{
    state_->attempts.clear();
    state_->stopped = true;
}

std::size_t ResponseAttemptTracker::size() const noexcept
{
    return state_->attempts.size();
}

bool ResponseAttemptTracker::healthy() const noexcept
{
    return state_->healthy;
}

} // namespace hotstuff
