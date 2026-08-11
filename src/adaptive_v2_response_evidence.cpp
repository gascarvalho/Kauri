#include "hotstuff/adaptive_v2_response_evidence.h"

#include <algorithm>
#include <limits>
#include <map>
#include <memory>
#include <optional>
#include <stdexcept>
#include <type_traits>
#include <utility>
#include <vector>

#include "hotstuff/util.h"

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
    std::uint64_t attempt_start_monotonic_ns{0};
    bool timeout_eligible{false};
    bool timeout_recorded{false};
    bool response_recorded{false};
    bool late_reservation{false};
};

struct BoundDeadline
{
    std::uint64_t generation{0};
    std::uint64_t reporter_local_commit_monotonic_ns{0};
    bool consensus_context_closed{false};
    bool fired{false};
    bool dispatching{false};
    bool delivery_failed{false};
    std::size_t pending_evidence_acceptances{0};
    EvidenceDeadlineCancellation cancellation;
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

bool has_unanswered_required_child(
    const std::map<ChildAttemptKey,
                   BoundAttempt,
                   ChildAttemptKeyLess> &handles,
    const ProposalKey &proposal) noexcept
{
    return std::any_of(
        handles.begin(), handles.end(), [&proposal](const auto &entry) {
            return entry.first.proposal == proposal &&
                   entry.second.timeout_eligible &&
                   !entry.second.timeout_recorded &&
                   !entry.second.response_recorded;
        });
}

const char *response_message_type_name(
    ExpectedMessageType message_type) noexcept
{
    return message_type == ExpectedMessageType::aggregate_relay
               ? "aggregate_relay"
               : "direct_vote";
}

} // namespace

AdaptiveV2CrossCommitRetentionAdmission
select_adaptive_v2_cross_commit_retention_admission(
    const std::vector<AcceptedEvidenceRecord> &accepted,
    const AdaptationEpochId &current_epoch,
    std::uint64_t evidence_cutoff,
    const std::vector<ReplicaID> &responsive_degraded_actor_ids,
    AdaptiveV2CrossCommitRetentionAdmissionPolicy admission_policy) noexcept
{
    AdaptiveV2CrossCommitRetentionAdmission output;
    output.evidence_cutoff = evidence_cutoff;
    try
    {
        if (current_epoch.epoch_digest == uint256_t{} ||
            evidence_cutoff == 0 ||
            responsive_degraded_actor_ids.empty() ||
            (admission_policy !=
                 AdaptiveV2CrossCommitRetentionAdmissionPolicy::
                     one_per_actor_with_global_aggregate_v1 &&
             admission_policy !=
                 AdaptiveV2CrossCommitRetentionAdmissionPolicy::
                     aggregate_relay_per_actor_v1) ||
            !std::is_sorted(
                responsive_degraded_actor_ids.begin(),
                responsive_degraded_actor_ids.end()) ||
            std::adjacent_find(
                responsive_degraded_actor_ids.begin(),
                responsive_degraded_actor_ids.end()) !=
                responsive_degraded_actor_ids.end())
        {
            return output;
        }

        struct Candidate
        {
            ExpectedMessageType message_type{
                ExpectedMessageType::direct_vote};
            std::uint64_t ingestion_sequence{0};
            uint256_t observation_id;
        };
        const auto preferred = [](const Candidate &left,
                                  const Candidate &right) {
            const auto left_type =
                left.message_type == ExpectedMessageType::aggregate_relay
                    ? 0
                    : 1;
            const auto right_type =
                right.message_type == ExpectedMessageType::aggregate_relay
                    ? 0
                    : 1;
            if (left_type != right_type)
                return left_type < right_type;
            if (left.ingestion_sequence != right.ingestion_sequence)
                return left.ingestion_sequence < right.ingestion_sequence;
            return left.observation_id.to_hex() <
                right.observation_id.to_hex();
        };
        std::map<uint256_t, const AcceptedEvidenceRecord *> outstanding;
        std::uint64_t previous_sequence = 0;
        for (const auto &record : accepted)
        {
            if (record.ingestion_sequence == 0 ||
                record.ingestion_sequence <= previous_sequence)
            {
                return output;
            }
            if (record.ingestion_sequence > evidence_cutoff)
                break;
            previous_sequence = record.ingestion_sequence;

            const auto &observation = record.observation;
            if (observation.configuration.epoch_number !=
                    current_epoch.epoch_number ||
                observation.configuration.epoch_digest !=
                    current_epoch.epoch_digest)
            {
                continue;
            }
            const auto retained = outstanding.find(
                observation.observation_id);
            if (observation.outcome == ResponseOutcome::late &&
                retained != outstanding.end())
            {
                constexpr std::size_t kMaximumRetentionSigners = 4'096;
                const auto &timeout = retained->second->observation;
                if (timeout.outcome != ResponseOutcome::timeout ||
                    timeout.observation_id != observation.observation_id ||
                    timeout.attempt_identity() !=
                        observation.attempt_identity() ||
                    timeout.deadline_duration_us !=
                        observation.deadline_duration_us ||
                    observation.schema_version !=
                        kResponseObservationSchemaVersionV1 ||
                    !valid_response_observation_retention_witness(
                        observation) ||
                    observation.deadline_duration_us == 0 ||
                    observation.response_duration_us <
                        observation.deadline_duration_us ||
                    observation.signer_set.empty() ||
                    observation.signer_set.size() >
                        kMaximumRetentionSigners ||
                    std::adjacent_find(
                        observation.signer_set.begin(),
                        observation.signer_set.end(),
                        [](ReplicaID left, ReplicaID right) {
                            return left >= right;
                        }) != observation.signer_set.end())
                {
                    return output;
                }
                outstanding.erase(retained);
                continue;
            }
            if (retained != outstanding.end())
                return output;
            if (!std::binary_search(
                    responsive_degraded_actor_ids.begin(),
                    responsive_degraded_actor_ids.end(),
                    observation.observed_replica_id) ||
                observation.schema_version !=
                    kResponseObservationSchemaVersionV2)
            {
                continue;
            }
            if (observation.outcome != ResponseOutcome::timeout ||
                (observation.expected_message_type !=
                     ExpectedMessageType::direct_vote &&
                 observation.expected_message_type !=
                     ExpectedMessageType::aggregate_relay) ||
                !valid_response_observation_retention_witness(
                    observation))
            {
                return output;
            }
            if (!outstanding.emplace(
                    observation.observation_id, &record).second)
            {
                return output;
            }
        }
        std::map<ReplicaID, Candidate> selected;
        for (const auto &entry : outstanding)
        {
            const auto &record = *entry.second;
            const auto &observation = record.observation;
            const Candidate candidate{
                observation.expected_message_type,
                record.ingestion_sequence,
                observation.observation_id};
            const auto found = selected.find(
                observation.observed_replica_id);
            if (found == selected.end() ||
                preferred(candidate, found->second))
            {
                selected[observation.observed_replica_id] = candidate;
            }
        }

        bool contains_aggregate_relay = false;
        output.responsive_degraded_actor_ids =
            responsive_degraded_actor_ids;
        output.admitted_observation_ids.reserve(
            responsive_degraded_actor_ids.size());
        for (const auto actor : responsive_degraded_actor_ids)
        {
            const auto candidate = selected.find(actor);
            if (candidate == selected.end())
            {
                output.status =
                    AdaptiveV2CrossCommitRetentionAdmissionStatus::
                        incomplete;
                output.admitted_observation_ids.clear();
                return output;
            }
            if (admission_policy ==
                    AdaptiveV2CrossCommitRetentionAdmissionPolicy::
                        aggregate_relay_per_actor_v1 &&
                candidate->second.message_type !=
                    ExpectedMessageType::aggregate_relay)
            {
                output.status =
                    AdaptiveV2CrossCommitRetentionAdmissionStatus::
                        incomplete;
                output.admitted_observation_ids.clear();
                return output;
            }
            contains_aggregate_relay = contains_aggregate_relay ||
                candidate->second.message_type ==
                    ExpectedMessageType::aggregate_relay;
            output.admitted_observation_ids.push_back(
                candidate->second.observation_id);
        }
        if (admission_policy ==
                AdaptiveV2CrossCommitRetentionAdmissionPolicy::
                    one_per_actor_with_global_aggregate_v1 &&
            !contains_aggregate_relay)
        {
            output.status =
                AdaptiveV2CrossCommitRetentionAdmissionStatus::incomplete;
            output.admitted_observation_ids.clear();
            return output;
        }
        output.status =
            AdaptiveV2CrossCommitRetentionAdmissionStatus::ready;
        return output;
    }
    catch (...)
    {
        output.status =
            AdaptiveV2CrossCommitRetentionAdmissionStatus::invalid;
        output.responsive_degraded_actor_ids.clear();
        output.admitted_observation_ids.clear();
        return output;
    }
}

struct AdaptiveV2ResponseEvidenceBridge::State
{
    State(ReplicaID reporter_id,
          AdaptiveV2ResponseEvidenceLimits configured_limits)
        : reporter_id(reporter_id),
          limits(std::move(configured_limits)),
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

    ReplicaID reporter_id{0};
    AdaptiveV2ResponseEvidenceLimits limits;
    ResponseAttemptTracker tracker;
    EvidenceReporter reporter;
    RetainedFactQueue retained_facts;
    RetainedFactQueue late_compensations;
    std::map<ChildAttemptKey, BoundAttempt, ChildAttemptKeyLess> handles;
    std::map<ProposalKey, BoundDeadline> deadlines;
    EvidenceDeadlineScheduler deadline_scheduler;
    EvidenceDeadlineResultCallback deadline_result_callback;
    EvidenceTransportCallback transport;
    EvidenceRetryScheduler retry_scheduler;
    EvidenceRetryCancellation retry_cancellation;
    // Scheduler cancellations are user-supplied and may throw without
    // actually releasing a retained callback. Expiring this token before
    // shutdown makes every such late callback a no-op before it touches the
    // bridge object.
    std::shared_ptr<const bool> callback_lifetime{
        std::make_shared<const bool>(true)};
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
    std::uint64_t last_deadline_generation{0};
    bool retry_scheduled{false};
    bool cross_commit_retention_v2_enabled{false};
    bool terminal_delivery_failure{false};
    bool healthy{true};
    bool stopped{false};
};

AdaptiveV2ResponseEvidenceBridge::AdaptiveV2ResponseEvidenceBridge(
    ReplicaID reporter_id,
    AdaptiveV2ResponseEvidenceLimits limits)
    : state_(std::make_unique<State>(reporter_id, std::move(limits)))
{}

AdaptiveV2ResponseEvidenceBridge::~AdaptiveV2ResponseEvidenceBridge()
{
    shutdown();
}

bool AdaptiveV2ResponseEvidenceBridge::
enable_cross_commit_retention_v2() noexcept
{
    if (state_->stopped || !state_->handles.empty() ||
        !state_->deadlines.empty())
    {
        increment(state_->rejected_operations);
        state_->healthy = false;
        return false;
    }
    state_->cross_commit_retention_v2_enabled = true;
    return true;
}

bool AdaptiveV2ResponseEvidenceBridge::arm(
    const ProposalKey &proposal,
    const ProposalTreeSnapshot &tree,
    std::uint64_t start_monotonic_ns,
    std::uint64_t deadline_duration_us) noexcept
{
    if (state_->stopped)
    {
        increment(state_->rejected_operations);
        return false;
    }
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
                    start_monotonic_ns,
                    attempt.timeout_eligible,
                    false,
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

bool AdaptiveV2ResponseEvidenceBridge::arm_with_deadline(
    const ProposalKey &proposal,
    const ProposalTreeSnapshot &tree,
    std::uint64_t start_monotonic_ns,
    std::uint64_t deadline_duration_us) noexcept
{
    if (state_->terminal_delivery_failure)
    {
        increment(state_->rejected_operations);
        state_->healthy = false;
        notify_deadline_result(
            proposal, EvidenceDeadlineResult::failed);
        return false;
    }
    const bool already_armed = std::any_of(
        state_->handles.begin(),
        state_->handles.end(),
        [&proposal](const auto &entry) {
            return entry.first.proposal == proposal;
        });
    if (!arm(
            proposal,
            tree,
            start_monotonic_ns,
            deadline_duration_us))
    {
        // A rejected duplicate must not leave an earlier exact deadline
        // alive after reporting this proposal failed. Otherwise that timer
        // could later report success for the same failed fence.
        static_cast<void>(retire(proposal));
        notify_deadline_result(
            proposal, EvidenceDeadlineResult::failed);
        return false;
    }
    if (already_armed)
    {
        if (state_->deadlines.find(proposal) !=
            state_->deadlines.end())
            return true;
        increment(state_->rejected_operations);
        state_->healthy = false;
        static_cast<void>(retire(proposal));
        notify_deadline_result(
            proposal, EvidenceDeadlineResult::failed);
        return false;
    }
    if (schedule_deadline(proposal, deadline_duration_us))
        return true;
    static_cast<void>(retire(proposal));
    return false;
}

bool AdaptiveV2ResponseEvidenceBridge::schedule_deadline(
    const ProposalKey &proposal,
    std::uint64_t deadline_duration_us) noexcept
{
    if (state_->stopped || !state_->deadline_scheduler ||
        deadline_duration_us == 0 ||
        state_->deadlines.size() >= state_->limits.maximum_handles ||
        state_->last_deadline_generation ==
            std::numeric_limits<std::uint64_t>::max())
    {
        increment(state_->deadline_schedule_failures);
        state_->healthy = false;
        notify_deadline_result(
            proposal, EvidenceDeadlineResult::failed);
        return false;
    }
    if (state_->deadlines.find(proposal) != state_->deadlines.end())
        return true;

    const auto generation = state_->last_deadline_generation + 1;
    bool generation_inserted = false;
    try
    {
        const auto inserted = state_->deadlines.emplace(
            proposal,
            BoundDeadline{generation});
        if (!inserted.second)
        {
            increment(state_->deadline_schedule_failures);
            state_->healthy = false;
            notify_deadline_result(
                proposal, EvidenceDeadlineResult::failed);
            return false;
        }
        generation_inserted = true;
        state_->last_deadline_generation = generation;

        const std::weak_ptr<const bool> lifetime =
            state_->callback_lifetime;
        auto cancellation = state_->deadline_scheduler(
            proposal,
            deadline_duration_us,
            [this, lifetime, proposal, generation](
                std::uint64_t now_ns) {
                if (lifetime.expired())
                    return;
                dispatch_deadline(proposal, generation, now_ns);
            },
            [this, lifetime, proposal, generation] {
                if (lifetime.expired())
                    return;
                fail_deadline(proposal, generation);
            });
        auto found = state_->deadlines.find(proposal);
        if (!cancellation || found == state_->deadlines.end() ||
            found->second.generation != generation)
        {
            if (cancellation)
            {
                try
                {
                    cancellation();
                }
                catch (...)
                {
                    increment(
                        state_->deadline_cancellation_failures);
                    state_->healthy = false;
                }
            }
            if (found != state_->deadlines.end() &&
                found->second.generation == generation)
                fail_deadline(proposal, generation);
            // If the entry disappeared, a scheduler callback already
            // diagnosed and retired this generation synchronously.
            return false;
        }
        found->second.cancellation = std::move(cancellation);
        increment(state_->armed_deadlines);
        return true;
    }
    catch (...)
    {
        const auto found = state_->deadlines.find(proposal);
        if (found != state_->deadlines.end() &&
            found->second.generation == generation)
            fail_deadline(proposal, generation);
        else if (!generation_inserted)
        {
            increment(state_->deadline_schedule_failures);
            state_->healthy = false;
            notify_deadline_result(
                proposal, EvidenceDeadlineResult::failed);
        }
        return false;
    }
}

void AdaptiveV2ResponseEvidenceBridge::dispatch_deadline(
    const ProposalKey &proposal,
    std::uint64_t generation,
    std::uint64_t timeout_monotonic_ns) noexcept
{
    const auto found = state_->deadlines.find(proposal);
    if (found == state_->deadlines.end() ||
        found->second.generation != generation)
        return;

    found->second.fired = true;
    found->second.dispatching = true;
    found->second.cancellation = {};
    increment(state_->fired_deadlines);

    if (timeout_monotonic_ns == 0)
    {
        increment(state_->deadline_callback_failures);
        state_->healthy = false;
        found->second.dispatching = false;
        fail_deadline_delivery(proposal, generation);
        return;
    }

    std::set<ReplicaID> unanswered_required_children;
    try
    {
        for (const auto &entry : state_->handles)
            if (entry.first.proposal == proposal &&
                entry.second.timeout_eligible &&
                !entry.second.timeout_recorded &&
                !entry.second.response_recorded)
                unanswered_required_children.insert(entry.first.child);
    }
    catch (...)
    {
        increment(state_->deadline_callback_failures);
        state_->healthy = false;
        const auto active = state_->deadlines.find(proposal);
        if (active != state_->deadlines.end() &&
            active->second.generation == generation)
            active->second.dispatching = false;
        fail_deadline_delivery(proposal, generation);
        return;
    }

    const auto recorded = record_timeouts_impl(
        proposal,
        unanswered_required_children,
        timeout_monotonic_ns,
        true);
    const auto active = state_->deadlines.find(proposal);
    if (active == state_->deadlines.end() ||
        active->second.generation != generation)
        return;
    active->second.dispatching = false;
    if (recorded != unanswered_required_children.size() ||
        active->second.delivery_failed)
    {
        fail_deadline_delivery(proposal, generation);
        return;
    }
    finalize_ready_deadlines();
}

void AdaptiveV2ResponseEvidenceBridge::fail_deadline(
    const ProposalKey &proposal,
    std::uint64_t generation) noexcept
{
    const auto found = state_->deadlines.find(proposal);
    if (found == state_->deadlines.end() ||
        found->second.generation != generation)
        return;
    increment(state_->deadline_schedule_failures);
    state_->healthy = false;
    static_cast<void>(retire(proposal));
    notify_deadline_result(
        proposal, EvidenceDeadlineResult::failed);
}

void AdaptiveV2ResponseEvidenceBridge::complete_deadline(
    const ProposalKey &proposal,
    std::uint64_t generation) noexcept
{
    auto found = state_->deadlines.find(proposal);
    if (found == state_->deadlines.end() ||
        found->second.generation != generation ||
        found->second.dispatching || found->second.delivery_failed ||
        found->second.pending_evidence_acceptances != 0)
        return;
    const bool closed_without_unanswered =
        found->second.consensus_context_closed &&
        !has_unanswered_required_child(state_->handles, proposal);
    if (!closed_without_unanswered)
        return;

    auto cancellation = std::move(found->second.cancellation);
    if (cancellation)
    {
        try
        {
            cancellation();
            increment(state_->deadline_cancellations);
        }
        catch (...)
        {
            increment(state_->deadline_cancellation_failures);
            state_->healthy = false;
            fail_deadline_delivery(proposal, generation);
            return;
        }
        found = state_->deadlines.find(proposal);
        if (found == state_->deadlines.end() ||
            found->second.generation != generation)
            return;
    }
    const bool consensus_context_closed =
        found->second.consensus_context_closed;
    state_->deadlines.erase(found);
    increment(state_->completed_deadlines);
    if (consensus_context_closed)
        static_cast<void>(retire(proposal));
    notify_deadline_result(
        proposal, EvidenceDeadlineResult::evidence_accepted);
}

void AdaptiveV2ResponseEvidenceBridge::fail_deadline_delivery(
    const ProposalKey &proposal,
    std::uint64_t generation) noexcept
{
    const auto found = state_->deadlines.find(proposal);
    if (found == state_->deadlines.end() ||
        found->second.generation != generation)
        return;
    increment(state_->deadline_delivery_failures);
    state_->healthy = false;
    static_cast<void>(retire(proposal));
    notify_deadline_result(
        proposal, EvidenceDeadlineResult::failed);
}

void AdaptiveV2ResponseEvidenceBridge::notify_deadline_result(
    const ProposalKey &proposal,
    EvidenceDeadlineResult result) noexcept
{
    if (!state_->deadline_result_callback)
        return;
    try
    {
        state_->deadline_result_callback(proposal, result);
    }
    catch (...)
    {
        increment(state_->deadline_callback_failures);
        state_->healthy = false;
    }
}

bool AdaptiveV2ResponseEvidenceBridge::record_verified_response(
    const ProposalKey &proposal,
    ReplicaID authenticated_sender,
    ExpectedMessageType message_type,
    const std::set<ReplicaID> &canonical_verified_signers,
    std::uint64_t response_monotonic_ns) noexcept
{
    auto found = state_->handles.find(
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
        found = state_->handles.find(
            ChildAttemptKey{proposal, authenticated_sender});
        if (found == state_->handles.end() ||
            found->second.handle.key.expected_message_type != message_type)
        {
            increment(state_->rejected_operations);
            return false;
        }
        if (found->second.response_recorded)
        {
            increment(state_->idempotent_duplicate_responses);
            try
            {
                HOTSTUFF_LOG_INFO(
                    "KAURI_RESPONSE_EVIDENCE "
                    "disposition=idempotent_duplicate "
                    "reporter=%u child=%u epoch=%u tree=%u "
                    "digest=%s block=%s message_type=%s "
                    "attempt_generation=%llu response_monotonic_ns=%llu",
                    static_cast<unsigned>(state_->reporter_id),
                    static_cast<unsigned>(authenticated_sender),
                    proposal.configuration.epoch_number,
                    proposal.configuration.tree_id,
                    proposal.configuration.epoch_digest.to_hex().c_str(),
                    proposal.block_hash.to_hex().c_str(),
                    response_message_type_name(message_type),
                    static_cast<unsigned long long>(
                        found->second.handle.generation),
                    static_cast<unsigned long long>(
                        response_monotonic_ns));
            }
            catch (...)
            {
                // Audit output is best effort and cannot change protocol or
                // response-evidence state.
            }
            return false;
        }
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
            mark_deadline_delivery_failed(proposal);
            finalize_ready_deadlines();
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
            mark_deadline_delivery_failed(proposal);
            finalize_ready_deadlines();
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
            mark_deadline_delivery_failed(proposal);
            finalize_ready_deadlines();
            return false;
        }
        if ((expects_late && fact->outcome != ResponseOutcome::late) ||
            (!expects_late &&
             fact->outcome != ResponseOutcome::on_time))
        {
            increment(state_->rejected_operations);
            state_->healthy = false;
            mark_deadline_delivery_failed(proposal);
            finalize_ready_deadlines();
            return false;
        }
        // The tracker has irreversibly completed this exact attempt. Keep
        // the deadline callback idempotent even if an unreachable retention
        // failure is diagnosed below.
        found->second.response_recorded = true;
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
            mark_deadline_delivery_failed(proposal);
            finalize_ready_deadlines();
            return false;
        }
        const auto deadline = state_->deadlines.find(proposal);
        if (deadline != state_->deadlines.end())
        {
            if (deadline->second.pending_evidence_acceptances ==
                std::numeric_limits<std::size_t>::max())
            {
                mark_deadline_delivery_failed(proposal);
                finalize_ready_deadlines();
                return false;
            }
            else
                ++deadline->second.pending_evidence_acceptances;
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
        mark_deadline_delivery_failed(proposal);
        finalize_ready_deadlines();
        return false;
    }
}

std::size_t AdaptiveV2ResponseEvidenceBridge::record_timeouts(
    const ProposalKey &proposal,
    const std::set<ReplicaID> &exact_missing_direct_children,
    std::uint64_t timeout_monotonic_ns) noexcept
{
    return record_timeouts_impl(
        proposal,
        exact_missing_direct_children,
        timeout_monotonic_ns,
        false);
}

bool AdaptiveV2ResponseEvidenceBridge::record_reporter_local_commit(
    const ProposalKey &proposal,
    std::uint64_t commit_monotonic_ns) noexcept
{
    if (!state_->cross_commit_retention_v2_enabled)
        return false;
    if (state_->stopped || commit_monotonic_ns == 0)
    {
        increment(state_->rejected_operations);
        state_->healthy = false;
        return false;
    }
    const auto deadline = state_->deadlines.find(proposal);
    if (deadline == state_->deadlines.end())
        return true;
    if (deadline->second.reporter_local_commit_monotonic_ns == 0)
    {
        deadline->second.reporter_local_commit_monotonic_ns =
            commit_monotonic_ns;
    }
    else if (deadline->second.reporter_local_commit_monotonic_ns !=
             commit_monotonic_ns)
    {
        increment(state_->rejected_operations);
        state_->healthy = false;
        return false;
    }
    return true;
}

std::size_t AdaptiveV2ResponseEvidenceBridge::record_timeouts_impl(
    const ProposalKey &proposal,
    const std::set<ReplicaID> &exact_missing_direct_children,
    std::uint64_t timeout_monotonic_ns,
    bool produced_by_deadline) noexcept
{
    if (produced_by_deadline)
    {
        const auto deadline = state_->deadlines.find(proposal);
        if (deadline == state_->deadlines.end() ||
            !deadline->second.fired ||
            !deadline->second.dispatching)
        {
            increment(state_->rejected_operations);
            state_->healthy = false;
            return 0;
        }
    }
    std::size_t recorded = 0;
    for (const auto child : exact_missing_direct_children)
    {
        // A retry may complete a closed exact deadline and retire its
        // handles, so flush before taking an iterator into the handle map.
        static_cast<void>(flush());
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
        if (found->second.timeout_recorded ||
            found->second.response_recorded)
            continue;
        try
        {
            if (!state_->late_compensations.empty() ||
                state_->retained_facts.full())
            {
                increment(state_->retention_capacity_failures);
                increment(state_->rejected_operations);
                state_->healthy = false;
                mark_deadline_delivery_failed(proposal);
                finalize_ready_deadlines();
                continue;
            }
            if (!found->second.late_reservation)
            {
                increment(
                    state_->late_compensation_capacity_failures);
                increment(state_->rejected_operations);
                state_->healthy = false;
                mark_deadline_delivery_failed(proposal);
                finalize_ready_deadlines();
                continue;
            }
            auto fact = state_->tracker.record_timeout(
                found->second.handle, timeout_monotonic_ns);
            if (!fact.has_value())
            {
                increment(state_->timeout_tracker_rejections);
                mark_deadline_delivery_failed(proposal);
                finalize_ready_deadlines();
                continue;
            }
            const auto retained_deadline =
                state_->deadlines.find(proposal);
            if (state_->cross_commit_retention_v2_enabled &&
                retained_deadline != state_->deadlines.end() &&
                retained_deadline->second
                        .reporter_local_commit_monotonic_ns != 0 &&
                found->second.attempt_start_monotonic_ns != 0 &&
                fact->deadline_duration_us <=
                    std::numeric_limits<std::uint64_t>::max() / 1'000)
            {
                const auto deadline_duration_ns =
                    fact->deadline_duration_us * 1'000;
                const auto attempt_start =
                    found->second.attempt_start_monotonic_ns;
                const auto commit =
                    retained_deadline->second
                        .reporter_local_commit_monotonic_ns;
                if (attempt_start <=
                        std::numeric_limits<std::uint64_t>::max() -
                            deadline_duration_ns)
                {
                    const auto absolute_deadline =
                        attempt_start + deadline_duration_ns;
                    if (attempt_start <= commit &&
                        commit < absolute_deadline &&
                        absolute_deadline <= timeout_monotonic_ns)
                    {
                        fact->attempt_start_monotonic_ns =
                            attempt_start;
                        fact->reporter_local_commit_monotonic_ns =
                            commit;
                    }
                }
            }
            if (!state_->retained_facts.push(std::move(*fact)))
            {
                increment(state_->retention_capacity_failures);
                state_->healthy = false;
                mark_deadline_delivery_failed(proposal);
                finalize_ready_deadlines();
                continue;
            }
            const auto deadline = state_->deadlines.find(proposal);
            if (deadline != state_->deadlines.end())
            {
                if (deadline->second.pending_evidence_acceptances ==
                    std::numeric_limits<std::size_t>::max())
                {
                    mark_deadline_delivery_failed(proposal);
                    finalize_ready_deadlines();
                    continue;
                }
                else
                    ++deadline->second.pending_evidence_acceptances;
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
            mark_deadline_delivery_failed(proposal);
            finalize_ready_deadlines();
        }
    }
    return recorded;
}

bool AdaptiveV2ResponseEvidenceBridge::close_consensus_context(
    const ProposalKey &proposal) noexcept
{
    const auto deadline = state_->deadlines.find(proposal);
    if (deadline == state_->deadlines.end())
    {
        static_cast<void>(retire(proposal));
        return false;
    }
    if (!deadline->second.fired &&
        !has_unanswered_required_child(state_->handles, proposal) &&
        deadline->second.pending_evidence_acceptances == 0)
    {
        static_cast<void>(retire(proposal));
        return false;
    }
    if (!deadline->second.consensus_context_closed)
    {
        deadline->second.consensus_context_closed = true;
        increment(state_->closed_contexts_retained);
    }
    finalize_ready_deadlines();
    return true;
}

bool AdaptiveV2ResponseEvidenceBridge::should_defer_commit_report(
    const ProposalKey &proposal) const noexcept
{
    const auto deadline = state_->deadlines.find(proposal);
    return deadline != state_->deadlines.end() &&
           !deadline->second.delivery_failed &&
           (deadline->second.fired ||
            deadline->second.pending_evidence_acceptances != 0 ||
            has_unanswered_required_child(state_->handles, proposal));
}

std::size_t AdaptiveV2ResponseEvidenceBridge::retire(
    const ProposalKey &proposal) noexcept
{
    const auto deadline = state_->deadlines.find(proposal);
    if (deadline != state_->deadlines.end())
    {
        auto cancellation = std::move(deadline->second.cancellation);
        state_->deadlines.erase(deadline);
        if (cancellation)
        {
            try
            {
                cancellation();
                increment(state_->deadline_cancellations);
            }
            catch (...)
            {
                increment(state_->deadline_cancellation_failures);
                state_->healthy = false;
            }
        }
    }
    const auto tracker_retired = state_->tracker.retire(proposal);
    const auto handles_retired = erase_proposal_handles(
        state_->handles, proposal);
    if (tracker_retired != handles_retired)
        state_->healthy = false;
    for (std::size_t index = 0; index < handles_retired; ++index)
        increment(state_->retired_attempts);
    return handles_retired;
}

void AdaptiveV2ResponseEvidenceBridge::shutdown() noexcept
{
    if (state_->stopped)
        return;
    state_->stopped = true;
    state_->callback_lifetime.reset();
    while (!state_->deadlines.empty())
    {
        const auto proposal = state_->deadlines.begin()->first;
        const auto generation =
            state_->deadlines.begin()->second.generation;
        fail_deadline_delivery(proposal, generation);
    }
    while (!state_->handles.empty())
        static_cast<void>(retire(state_->handles.begin()->first.proposal));
    state_->deadline_scheduler = {};
    unbind_transport();
    unbind_retry_scheduler();
    state_->tracker.shutdown();
    state_->reporter.shutdown();
}

void AdaptiveV2ResponseEvidenceBridge::bind_deadline_scheduler(
    EvidenceDeadlineScheduler scheduler)
{
    if (!scheduler)
        throw std::invalid_argument(
            "adaptive-v2 evidence deadline scheduler must be callable");
    if (state_->stopped)
        throw std::logic_error(
            "adaptive-v2 evidence bridge is stopped");
    if (!state_->deadlines.empty())
        throw std::logic_error(
            "adaptive-v2 evidence deadlines are active");
    state_->deadline_scheduler = std::move(scheduler);
}

void AdaptiveV2ResponseEvidenceBridge::unbind_deadline_scheduler() noexcept
{
    while (!state_->deadlines.empty())
    {
        const auto proposal = state_->deadlines.begin()->first;
        const auto generation =
            state_->deadlines.begin()->second.generation;
        fail_deadline_delivery(proposal, generation);
    }
    state_->deadline_scheduler = {};
}

void AdaptiveV2ResponseEvidenceBridge::bind_deadline_result_callback(
    EvidenceDeadlineResultCallback callback)
{
    if (!callback)
        throw std::invalid_argument(
            "adaptive-v2 evidence deadline result callback must be callable");
    if (state_->stopped)
        throw std::logic_error(
            "adaptive-v2 evidence bridge is stopped");
    state_->deadline_result_callback = std::move(callback);
}

void AdaptiveV2ResponseEvidenceBridge::
unbind_deadline_result_callback() noexcept
{
    state_->deadline_result_callback = {};
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
        const std::weak_ptr<const bool> lifetime =
            state_->callback_lifetime;
        auto cancellation = state_->retry_scheduler(
            [this, lifetime] {
                if (lifetime.expired())
                    return;
                run_scheduled_retry();
            });
        if (!cancellation)
        {
            state_->retry_scheduled = false;
            increment(state_->retry_schedule_failures);
            state_->healthy = false;
            mark_all_deadline_deliveries_failed();
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
        mark_all_deadline_deliveries_failed();
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

void AdaptiveV2ResponseEvidenceBridge::acknowledge_accepted_evidence(
    const ProposalKey &proposal) noexcept
{
    const auto deadline = state_->deadlines.find(proposal);
    if (deadline == state_->deadlines.end() ||
        deadline->second.pending_evidence_acceptances == 0)
        return;
    --deadline->second.pending_evidence_acceptances;
}

void AdaptiveV2ResponseEvidenceBridge::mark_deadline_delivery_failed(
    const ProposalKey &proposal) noexcept
{
    const auto deadline = state_->deadlines.find(proposal);
    if (deadline == state_->deadlines.end())
        return;
    deadline->second.delivery_failed = true;
    state_->healthy = false;
}

void AdaptiveV2ResponseEvidenceBridge::
mark_all_deadline_deliveries_failed() noexcept
{
    state_->terminal_delivery_failure = true;
    for (auto &entry : state_->deadlines)
        entry.second.delivery_failed = true;
    state_->healthy = false;
}

void AdaptiveV2ResponseEvidenceBridge::finalize_ready_deadlines() noexcept
{
    while (true)
    {
        auto candidate = state_->deadlines.end();
        for (auto entry = state_->deadlines.begin();
             entry != state_->deadlines.end(); ++entry)
        {
            if (entry->second.dispatching)
                continue;
            const bool closed_without_unanswered =
                entry->second.consensus_context_closed &&
                !has_unanswered_required_child(
                    state_->handles, entry->first);
            if (entry->second.delivery_failed ||
                (entry->second.pending_evidence_acceptances == 0 &&
                 closed_without_unanswered))
            {
                candidate = entry;
                break;
            }
        }
        if (candidate == state_->deadlines.end())
            return;
        const auto proposal = candidate->first;
        const auto generation = candidate->second.generation;
        if (candidate->second.delivery_failed)
            fail_deadline_delivery(proposal, generation);
        else
            complete_deadline(proposal, generation);
    }
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
                mark_all_deadline_deliveries_failed();
                finalize_ready_deadlines();
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
            const auto *pending = state_->reporter.front();
            if (pending == nullptr)
                break;
            const ProposalKey pending_proposal{
                pending->envelope.observation.configuration,
                pending->envelope.observation.block_hash};
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
            {
                mark_all_deadline_deliveries_failed();
                break;
            }
            acknowledge_accepted_evidence(pending_proposal);
            ++accepted;
            progressed = true;
        }
        catch (...)
        {
            state_->healthy = false;
            mark_all_deadline_deliveries_failed();
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
    finalize_ready_deadlines();
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
    result.active_deadlines = state_->deadlines.size();
    result.pending_reports = state_->reporter.pending_size();
    result.retained_facts = state_->retained_facts.size();
    result.retention_capacity = state_->retained_facts.capacity();
    result.pending_late_compensations =
        state_->late_compensations.size();
    result.late_compensation_capacity =
        state_->late_compensations.capacity();
    result.armed_attempts = state_->armed_attempts;
    result.armed_deadlines = state_->armed_deadlines;
    result.fired_deadlines = state_->fired_deadlines;
    result.completed_deadlines = state_->completed_deadlines;
    result.closed_contexts_retained =
        state_->closed_contexts_retained;
    result.deadline_schedule_failures =
        state_->deadline_schedule_failures;
    result.deadline_callback_failures =
        state_->deadline_callback_failures;
    result.deadline_delivery_failures =
        state_->deadline_delivery_failures;
    result.deadline_cancellations =
        state_->deadline_cancellations;
    result.deadline_cancellation_failures =
        state_->deadline_cancellation_failures;
    result.response_facts = state_->response_facts;
    result.idempotent_duplicate_responses =
        state_->idempotent_duplicate_responses;
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
    result.deadline_scheduler_bound =
        static_cast<bool>(state_->deadline_scheduler);
    result.deadline_result_callback_bound =
        static_cast<bool>(state_->deadline_result_callback);
    result.retry_scheduled = state_->retry_scheduled;
    result.healthy = state_->healthy && state_->tracker.healthy() &&
                     state_->reporter.healthy();
    return result;
}

} // namespace hotstuff
