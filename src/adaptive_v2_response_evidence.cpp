#include "hotstuff/adaptive_v2_response_evidence.h"

#include <algorithm>
#include <limits>
#include <map>
#include <optional>
#include <stdexcept>
#include <type_traits>
#include <utility>
#include <vector>

namespace hotstuff
{
namespace
{

struct ChildAttemptKey
{
    ProposalKey proposal;
    ReplicaID child{0};
};

struct ChildAttemptKeyLess
{
    bool operator()(const ChildAttemptKey &left,
                    const ChildAttemptKey &right) const noexcept
    {
        if (left.proposal < right.proposal)
            return true;
        if (right.proposal < left.proposal)
            return false;
        return left.child < right.child;
    }
};

struct BoundAttempt
{
    ResponseAttemptHandle handle;
    bool timeout_eligible{false};
    bool timeout_recorded{false};
    bool late_reservation{false};
};

struct PreparedAttempt
{
    ChildAttemptKey key;
    ExpectedMessageType message_type{ExpectedMessageType::direct_vote};
    bool timeout_eligible{false};
};

/**
 * Fixed-capacity FIFO whose storage is allocated before any tracker event.
 * Moving a fact into an empty slot cannot allocate, so a successful capacity
 * preflight is a strong retention guarantee across reporter failures.
 */
class RetainedFactQueue final
{
public:
    explicit RetainedFactQueue(std::size_t capacity)
        : slots_(capacity)
    {
        static_assert(
            std::is_nothrow_move_constructible<
                ResponseAttemptFact>::value,
            "retained response facts must move without allocation");
    }

    bool push(ResponseAttemptFact &&fact) noexcept
    {
        if (full())
            return false;
        const auto tail = (head_ + size_) % slots_.size();
        slots_[tail].emplace(std::move(fact));
        ++size_;
        return true;
    }

    const ResponseAttemptFact *front() const noexcept
    {
        if (empty())
            return nullptr;
        return &*slots_[head_];
    }

    void pop() noexcept
    {
        if (empty())
            return;
        slots_[head_].reset();
        head_ = (head_ + 1) % slots_.size();
        --size_;
    }

    bool empty() const noexcept
    {
        return size_ == 0;
    }

    bool full() const noexcept
    {
        return size_ == slots_.size();
    }

    std::size_t size() const noexcept
    {
        return size_;
    }

    std::size_t capacity() const noexcept
    {
        return slots_.size();
    }

private:
    std::vector<std::optional<ResponseAttemptFact>> slots_;
    std::size_t head_{0};
    std::size_t size_{0};
};

void increment(std::uint64_t &value) noexcept
{
    if (value != std::numeric_limits<std::uint64_t>::max())
        ++value;
}

std::size_t erase_proposal_handles(
    std::map<ChildAttemptKey, BoundAttempt, ChildAttemptKeyLess> &handles,
    const ProposalKey &proposal) noexcept
{
    std::size_t erased = 0;
    for (auto iterator = handles.begin(); iterator != handles.end();)
    {
        if (iterator->first.proposal == proposal)
        {
            iterator = handles.erase(iterator);
            ++erased;
        }
        else
        {
            ++iterator;
        }
    }
    return erased;
}

std::size_t active_late_reservations(
    const std::map<ChildAttemptKey,
                   BoundAttempt,
                   ChildAttemptKeyLess> &handles) noexcept
{
    return static_cast<std::size_t>(std::count_if(
        handles.begin(),
        handles.end(),
        [](const auto &entry) {
            return entry.second.late_reservation;
        }));
}

} // namespace

struct AdaptiveV2ResponseEvidenceBridge::State
{
    State(ReplicaID reporter_id,
          AdaptiveV2ResponseEvidenceLimits configured_limits)
        : limits(std::move(configured_limits)),
          tracker(limits.attempts),
          reporter(EvidenceReporterConfig{
              reporter_id,
              0,
              0,
              limits.reporter,
              limits.wire}),
          retained_facts(limits.maximum_retained_facts),
          late_compensations(limits.maximum_late_compensations)
    {
        if (limits.maximum_handles == 0 ||
            limits.maximum_retained_facts == 0 ||
            limits.maximum_late_compensations == 0)
            healthy = false;
    }

    AdaptiveV2ResponseEvidenceLimits limits;
    ResponseAttemptTracker tracker;
    EvidenceReporter reporter;
    RetainedFactQueue retained_facts;
    RetainedFactQueue late_compensations;
    std::map<ChildAttemptKey, BoundAttempt, ChildAttemptKeyLess> handles;
    EvidenceTransportCallback transport;
    EvidenceRetryScheduler retry_scheduler;
    EvidenceRetryCancellation retry_cancellation;
    std::uint64_t armed_attempts{0};
    std::uint64_t response_facts{0};
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
    bool retry_scheduled{false};
    bool healthy{true};
};

AdaptiveV2ResponseEvidenceBridge::AdaptiveV2ResponseEvidenceBridge(
    ReplicaID reporter_id,
    AdaptiveV2ResponseEvidenceLimits limits)
    : state_(std::make_unique<State>(reporter_id, std::move(limits)))
{}

AdaptiveV2ResponseEvidenceBridge::~AdaptiveV2ResponseEvidenceBridge()
{
    unbind_transport();
    unbind_retry_scheduler();
    state_->tracker.shutdown();
    state_->reporter.shutdown();
}

bool AdaptiveV2ResponseEvidenceBridge::arm(
    const ProposalKey &proposal,
    const ProposalTreeSnapshot &tree,
    std::uint64_t start_monotonic_ns,
    std::uint64_t deadline_duration_us) noexcept
{
    if (start_monotonic_ns == 0 || deadline_duration_us == 0)
    {
        increment(state_->rejected_operations);
        state_->healthy = false;
        return false;
    }

    try
    {
        std::vector<PreparedAttempt> prepared;
        prepared.reserve(tree.direct_children.size());
        std::set<ReplicaID> unique_children;
        for (const auto child : tree.direct_children)
        {
            if (!unique_children.insert(child).second)
            {
                increment(state_->rejected_operations);
                state_->healthy = false;
                return false;
            }
            const auto subtree = tree.child_subtrees.find(child);
            const auto required = tree.required_child_subtrees.find(child);
            if (subtree == tree.child_subtrees.end() ||
                subtree->second.empty() ||
                subtree->second.count(child) == 0 ||
                required == tree.required_child_subtrees.end())
            {
                increment(state_->rejected_operations);
                state_->healthy = false;
                return false;
            }
            prepared.push_back(PreparedAttempt{
                ChildAttemptKey{proposal, child},
                subtree->second.size() > 1
                    ? ExpectedMessageType::aggregate_relay
                    : ExpectedMessageType::direct_vote,
                !required->second.empty()});
        }

        std::size_t existing = 0;
        std::size_t proposal_handles = 0;
        for (const auto &handle : state_->handles)
            if (handle.first.proposal == proposal)
                ++proposal_handles;
        for (const auto &attempt : prepared)
        {
            const auto found = state_->handles.find(attempt.key);
            if (found == state_->handles.end())
                continue;
            ++existing;
            if (found->second.handle.key.expected_message_type !=
                    attempt.message_type ||
                found->second.timeout_eligible !=
                    attempt.timeout_eligible)
            {
                increment(state_->rejected_operations);
                state_->healthy = false;
                return false;
            }
        }
        if (existing != 0)
        {
            if (existing == prepared.size() &&
                proposal_handles == prepared.size())
                return true;
            increment(state_->rejected_operations);
            state_->healthy = false;
            return false;
        }

        if (prepared.size() > state_->limits.maximum_handles ||
            state_->handles.size() >
                state_->limits.maximum_handles - prepared.size() ||
            prepared.size() > state_->limits.attempts.maximum_attempts ||
            state_->tracker.size() >
                state_->limits.attempts.maximum_attempts - prepared.size())
        {
            increment(state_->capacity_failures);
            state_->healthy = false;
            return false;
        }

        const auto new_late_reservations =
            static_cast<std::size_t>(std::count_if(
                prepared.begin(),
                prepared.end(),
                [](const PreparedAttempt &attempt) {
                    return attempt.timeout_eligible;
                }));
        const auto active_reservations =
            active_late_reservations(state_->handles);
        const auto queued_late_compensations =
            state_->late_compensations.size();
        const auto late_capacity =
            state_->limits.maximum_late_compensations;
        if (new_late_reservations >
                late_capacity ||
            queued_late_compensations > late_capacity ||
            active_reservations >
                late_capacity - queued_late_compensations ||
            new_late_reservations >
                late_capacity - queued_late_compensations -
                    active_reservations)
        {
            increment(state_->late_compensation_capacity_failures);
            increment(state_->capacity_failures);
            state_->healthy = false;
            return false;
        }

        for (const auto &attempt : prepared)
        {
            auto handle = state_->tracker.arm(ResponseAttemptArm{
                ResponseAttemptKey{
                    proposal, attempt.key.child, attempt.message_type},
                start_monotonic_ns,
                deadline_duration_us});
            if (!handle.has_value())
                throw std::runtime_error("response attempt arm failed");
            const auto inserted = state_->handles.emplace(
                attempt.key,
                BoundAttempt{
                    *handle,
                    attempt.timeout_eligible,
                    false,
                    attempt.timeout_eligible});
            if (!inserted.second)
                throw std::runtime_error("response handle insert failed");
        }
        for (std::size_t index = 0; index < prepared.size(); ++index)
            increment(state_->armed_attempts);
        return true;
    }
    catch (...)
    {
        static_cast<void>(state_->tracker.retire(proposal));
        static_cast<void>(erase_proposal_handles(
            state_->handles, proposal));
        increment(state_->rejected_operations);
        state_->healthy = false;
        return false;
    }
}

bool AdaptiveV2ResponseEvidenceBridge::record_verified_response(
    const ProposalKey &proposal,
    ReplicaID authenticated_sender,
    ExpectedMessageType message_type,
    const std::set<ReplicaID> &canonical_verified_signers,
    std::uint64_t response_monotonic_ns) noexcept
{
    const auto found = state_->handles.find(
        ChildAttemptKey{proposal, authenticated_sender});
    if (found == state_->handles.end() ||
        found->second.handle.key.expected_message_type != message_type)
    {
        increment(state_->rejected_operations);
        return false;
    }

    try
    {
        // Give a bound transport and the reporter FIFO the first chance to
        // release retention before the tracker state can advance.
        static_cast<void>(flush());
        const bool expects_late = found->second.timeout_recorded;
        const bool use_late_reservation =
            expects_late &&
            (!state_->late_compensations.empty() ||
             state_->retained_facts.full());
        if (!expects_late &&
            (!state_->late_compensations.empty() ||
             state_->retained_facts.full()))
        {
            increment(state_->retention_capacity_failures);
            increment(state_->rejected_operations);
            state_->healthy = false;
            return false;
        }
        if (expects_late &&
            (!found->second.late_reservation ||
             (use_late_reservation &&
              state_->late_compensations.full())))
        {
            increment(state_->late_compensation_capacity_failures);
            increment(state_->rejected_operations);
            state_->healthy = false;
            return false;
        }
        const std::vector<ReplicaID> signers(
            canonical_verified_signers.begin(),
            canonical_verified_signers.end());
        auto fact = state_->tracker.record_response(
            found->second.handle,
            response_monotonic_ns,
            signers);
        if (!fact.has_value())
        {
            increment(state_->rejected_operations);
            return false;
        }
        if ((expects_late && fact->outcome != ResponseOutcome::late) ||
            (!expects_late &&
             fact->outcome != ResponseOutcome::on_time))
        {
            increment(state_->rejected_operations);
            state_->healthy = false;
            return false;
        }
        auto &destination = use_late_reservation
                                ? state_->late_compensations
                                : state_->retained_facts;
        if (!destination.push(std::move(*fact)))
        {
            // Event-loop confinement plus the capacity preflight makes this
            // unreachable; retain the fail-closed diagnostic if violated.
            if (use_late_reservation)
                increment(
                    state_->late_compensation_capacity_failures);
            else
                increment(state_->retention_capacity_failures);
            state_->healthy = false;
            return false;
        }
        found->second.late_reservation = false;
        increment(state_->response_facts);
        static_cast<void>(flush());
        return true;
    }
    catch (...)
    {
        increment(state_->rejected_operations);
        state_->healthy = false;
        return false;
    }
}

std::size_t AdaptiveV2ResponseEvidenceBridge::record_timeouts(
    const ProposalKey &proposal,
    const std::set<ReplicaID> &exact_missing_direct_children,
    std::uint64_t timeout_monotonic_ns) noexcept
{
    std::size_t recorded = 0;
    for (const auto child : exact_missing_direct_children)
    {
        const auto found = state_->handles.find(
            ChildAttemptKey{proposal, child});
        if (found == state_->handles.end())
        {
            increment(state_->timeout_missing_handles);
            continue;
        }
        if (!found->second.timeout_eligible)
        {
            increment(state_->timeout_ineligible_attempts);
            continue;
        }
        try
        {
            static_cast<void>(flush());
            if (!state_->late_compensations.empty() ||
                state_->retained_facts.full())
            {
                increment(state_->retention_capacity_failures);
                increment(state_->rejected_operations);
                state_->healthy = false;
                continue;
            }
            if (!found->second.late_reservation)
            {
                increment(
                    state_->late_compensation_capacity_failures);
                increment(state_->rejected_operations);
                state_->healthy = false;
                continue;
            }
            auto fact = state_->tracker.record_timeout(
                found->second.handle, timeout_monotonic_ns);
            if (!fact.has_value())
            {
                increment(state_->timeout_tracker_rejections);
                continue;
            }
            if (!state_->retained_facts.push(std::move(*fact)))
            {
                increment(state_->retention_capacity_failures);
                state_->healthy = false;
                continue;
            }
            found->second.timeout_recorded = true;
            ++recorded;
            increment(state_->timeout_facts);
            static_cast<void>(flush());
        }
        catch (...)
        {
            increment(state_->timeout_exceptions);
            increment(state_->rejected_operations);
            state_->healthy = false;
        }
    }
    return recorded;
}

std::size_t AdaptiveV2ResponseEvidenceBridge::retire(
    const ProposalKey &proposal) noexcept
{
    const auto tracker_retired = state_->tracker.retire(proposal);
    const auto handles_retired = erase_proposal_handles(
        state_->handles, proposal);
    if (tracker_retired != handles_retired)
        state_->healthy = false;
    for (std::size_t index = 0; index < handles_retired; ++index)
        increment(state_->retired_attempts);
    return handles_retired;
}

void AdaptiveV2ResponseEvidenceBridge::bind_retry_scheduler(
    EvidenceRetryScheduler scheduler)
{
    if (!scheduler)
        throw std::invalid_argument(
            "adaptive-v2 evidence retry scheduler must be callable");
    cancel_retry();
    state_->retry_scheduler = std::move(scheduler);
    static_cast<void>(flush());
}

void AdaptiveV2ResponseEvidenceBridge::unbind_retry_scheduler() noexcept
{
    cancel_retry();
    state_->retry_scheduler = {};
}

void AdaptiveV2ResponseEvidenceBridge::bind_transport(
    EvidenceTransportCallback transport)
{
    if (!transport)
        throw std::invalid_argument(
            "adaptive-v2 evidence transport must be callable");
    cancel_retry();
    state_->transport = std::move(transport);
    static_cast<void>(flush());
}

void AdaptiveV2ResponseEvidenceBridge::unbind_transport() noexcept
{
    cancel_retry();
    state_->transport = {};
}

void AdaptiveV2ResponseEvidenceBridge::schedule_retry() noexcept
{
    if (state_->retry_scheduled || !state_->retry_scheduler)
        return;

    state_->retry_scheduled = true;
    try
    {
        auto cancellation = state_->retry_scheduler(
            [this] { run_scheduled_retry(); });
        if (!cancellation)
        {
            state_->retry_scheduled = false;
            increment(state_->retry_schedule_failures);
            state_->healthy = false;
            return;
        }
        state_->retry_cancellation = std::move(cancellation);
        increment(state_->retry_schedules);
    }
    catch (...)
    {
        state_->retry_scheduled = false;
        state_->retry_cancellation = {};
        increment(state_->retry_schedule_failures);
        state_->healthy = false;
    }
}

void AdaptiveV2ResponseEvidenceBridge::cancel_retry() noexcept
{
    auto cancellation = std::move(state_->retry_cancellation);
    state_->retry_scheduled = false;
    if (!cancellation)
        return;
    try
    {
        cancellation();
    }
    catch (...)
    {
        increment(state_->retry_schedule_failures);
        state_->healthy = false;
    }
}

void AdaptiveV2ResponseEvidenceBridge::run_scheduled_retry() noexcept
{
    if (!state_->retry_scheduled)
        return;
    state_->retry_scheduled = false;
    state_->retry_cancellation = {};
    static_cast<void>(flush());
}

std::size_t AdaptiveV2ResponseEvidenceBridge::flush() noexcept
{
    std::size_t accepted = 0;
    while (true)
    {
        bool progressed = false;
        while (state_->reporter.pending_size() <
               state_->limits.reporter.maximum_pending_reports)
        {
            auto *source = !state_->retained_facts.empty()
                               ? &state_->retained_facts
                               : &state_->late_compensations;
            if (source->empty())
                break;
            const auto *fact = source->front();
            if (fact == nullptr || !state_->reporter.enqueue(*fact))
            {
                increment(state_->enqueue_failures);
                state_->healthy = false;
                return accepted;
            }
            source->pop();
            progressed = true;
        }

        if (!state_->transport ||
            state_->reporter.pending_size() == 0)
            break;
        try
        {
            const auto result = state_->reporter.dispatch_one(
                state_->transport);
            if (!result.has_value())
                break;
            if (*result == EvidenceTransportResult::temporary_failure)
            {
                schedule_retry();
                break;
            }
            if (*result != EvidenceTransportResult::accepted)
                break;
            ++accepted;
            progressed = true;
        }
        catch (...)
        {
            state_->healthy = false;
            break;
        }
        if (!progressed)
            break;
    }
    if (state_->reporter.pending_size() == 0 &&
        state_->retained_facts.empty() &&
        state_->late_compensations.empty())
    {
        cancel_retry();
    }
    return accepted;
}

const PendingEvidenceReport *
AdaptiveV2ResponseEvidenceBridge::front() const noexcept
{
    return state_->reporter.front();
}

AdaptiveV2ResponseEvidenceDiagnostics
AdaptiveV2ResponseEvidenceBridge::diagnostics() const noexcept
{
    AdaptiveV2ResponseEvidenceDiagnostics result;
    result.active_handles = state_->handles.size();
    result.pending_reports = state_->reporter.pending_size();
    result.retained_facts = state_->retained_facts.size();
    result.retention_capacity = state_->retained_facts.capacity();
    result.pending_late_compensations =
        state_->late_compensations.size();
    result.late_compensation_capacity =
        state_->late_compensations.capacity();
    result.armed_attempts = state_->armed_attempts;
    result.response_facts = state_->response_facts;
    result.timeout_facts = state_->timeout_facts;
    result.timeout_missing_handles =
        state_->timeout_missing_handles;
    result.timeout_ineligible_attempts =
        state_->timeout_ineligible_attempts;
    result.timeout_tracker_rejections =
        state_->timeout_tracker_rejections;
    result.timeout_exceptions = state_->timeout_exceptions;
    result.retired_attempts = state_->retired_attempts;
    result.rejected_operations = state_->rejected_operations;
    result.capacity_failures = state_->capacity_failures;
    result.retention_capacity_failures =
        state_->retention_capacity_failures;
    result.late_compensation_capacity_failures =
        state_->late_compensation_capacity_failures;
    result.enqueue_failures = state_->enqueue_failures;
    result.retry_schedules = state_->retry_schedules;
    result.retry_schedule_failures =
        state_->retry_schedule_failures;
    result.transport_bound = static_cast<bool>(state_->transport);
    result.retry_scheduler_bound =
        static_cast<bool>(state_->retry_scheduler);
    result.retry_scheduled = state_->retry_scheduled;
    result.healthy = state_->healthy && state_->tracker.healthy() &&
                     state_->reporter.healthy();
    return result;
}

} // namespace hotstuff
