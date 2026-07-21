#include "hotstuff/adaptive_v2_manager_convergence.h"

#include <algorithm>
#include <limits>
#include <map>
#include <optional>
#include <stdexcept>
#include <utility>

namespace hotstuff
{
namespace
{

bool zero_digest(const uint256_t &digest) noexcept
{
    return digest == uint256_t{};
}

bool valid_identity(
    const AdaptiveV2EpochChangeIdentity &identity) noexcept
{
    if (identity.command_block_height == 0 ||
        identity.activation_delay_blocks == 0 ||
        identity.predecessor_epoch_number ==
            std::numeric_limits<std::uint32_t>::max() ||
        identity.successor_epoch_number !=
            identity.predecessor_epoch_number + 1 ||
        zero_digest(identity.predecessor_epoch_digest) ||
        zero_digest(identity.successor_epoch_digest) ||
        zero_digest(identity.command_payload_digest) ||
        zero_digest(identity.command_block_hash) ||
        identity.predecessor_epoch_digest ==
            identity.successor_epoch_digest)
    {
        return false;
    }
    if (identity.command_block_height >
        std::numeric_limits<std::uint64_t>::max() -
            identity.activation_delay_blocks)
    {
        return false;
    }
    return identity.activation_height ==
           identity.command_block_height +
               identity.activation_delay_blocks;
}

bool same_committed_observation(
    const AdaptiveV2EpochChangeCommittedObservation &left,
    const AdaptiveV2EpochChangeCommittedObservation &right) noexcept
{
    return left.schema_version == right.schema_version &&
           left.claimed_source_replica_id ==
               right.claimed_source_replica_id &&
           left.identity == right.identity;
}

bool same_activated_observation(
    const AdaptiveV2EpochActivatedObservation &left,
    const AdaptiveV2EpochActivatedObservation &right) noexcept
{
    return left.schema_version == right.schema_version &&
           left.claimed_source_replica_id ==
               right.claimed_source_replica_id &&
           left.identity == right.identity &&
           left.activated_epoch_number ==
               right.activated_epoch_number &&
           left.activated_epoch_digest ==
               right.activated_epoch_digest;
}

struct PrecommitIdentity
{
    std::uint32_t predecessor_epoch_number{0};
    uint256_t predecessor_epoch_digest;
    std::uint32_t successor_epoch_number{0};
    uint256_t successor_epoch_digest;
    uint256_t command_payload_digest;
    std::uint64_t activation_delay_blocks{0};
};

bool matches_precommit(
    const AdaptiveV2EpochChangeIdentity &identity,
    const PrecommitIdentity &precommit) noexcept
{
    return identity.predecessor_epoch_number ==
               precommit.predecessor_epoch_number &&
           identity.predecessor_epoch_digest ==
               precommit.predecessor_epoch_digest &&
           identity.successor_epoch_number ==
               precommit.successor_epoch_number &&
           identity.successor_epoch_digest ==
               precommit.successor_epoch_digest &&
           identity.command_payload_digest ==
               precommit.command_payload_digest &&
           identity.activation_delay_blocks ==
               precommit.activation_delay_blocks;
}

std::vector<ReplicaID> canonical_members(
    const EpochTreeDefinition &tree)
{
    auto membership = tree.members_breadth_first;
    std::sort(membership.begin(), membership.end());
    if (membership.empty() ||
        std::adjacent_find(membership.begin(), membership.end()) !=
            membership.end())
    {
        throw std::invalid_argument(
            "adaptive-v2 convergence bundle tree membership is invalid");
    }
    return membership;
}

bool valid_config(
    const AdaptiveV2ManagerConvergenceConfig &config,
    std::uint64_t start_tick)
{
    if (config.membership.empty() ||
        config.retry_interval_ticks == 0 ||
        config.maximum_attempts_per_recipient == 0 ||
        config.convergence_deadline_tick <= start_tick)
    {
        return false;
    }

    auto membership = config.membership;
    std::sort(membership.begin(), membership.end());
    if (std::adjacent_find(membership.begin(), membership.end()) !=
        membership.end())
    {
        return false;
    }
    const auto quorum = derive_byzantine_quorum(membership.size());
    return quorum.has_value() && quorum->fault_threshold != 0;
}

struct ValidatedStartup
{
    std::shared_ptr<const bytearray_t> bundle_bytes;
    PrecommitIdentity precommit;
    AdaptiveV2ManagerConvergenceConfig config;
    std::size_t required_activations{0};
};

ValidatedStartup validate_startup(
    const AdaptiveV2EpochChangeBundle &bundle,
    AdaptiveV2ManagerConvergenceConfig config,
    std::uint64_t start_tick)
{
    if (!valid_config(config, start_tick) ||
        bundle.schema_version() !=
            kEpochChangeBundleSchemaVersionV1 ||
        bundle.protocol_mode() != EpochProtocolMode::adaptive_v2 ||
        bundle.canonical_bytes().empty())
    {
        throw std::invalid_argument(
            "invalid adaptive-v2 convergence startup");
    }

    const auto &definition = bundle.definition();
    const auto &command = bundle.command();
    if (definition.schema_version !=
            kEpochDefinitionSchemaVersionV2 ||
        definition.activation_height != 0 ||
        definition.trees.empty() ||
        command.schema_version != kEpochChangeSchemaVersionV1 ||
        command.protocol_mode != EpochProtocolMode::adaptive_v2 ||
        command.payload.successor_epoch_number == 0 ||
        command.payload.activation_delay_blocks == 0)
    {
        throw std::invalid_argument(
            "adaptive-v2 convergence bundle is not a successor");
    }

    auto bundle_membership = canonical_members(
        definition.trees.front());
    for (const auto &tree : definition.trees)
    {
        if (canonical_members(tree) != bundle_membership)
        {
            throw std::invalid_argument(
                "adaptive-v2 convergence bundle changes membership");
        }
    }
    if (definition.membership_digest !=
        canonical_membership_digest(bundle_membership))
    {
        throw std::invalid_argument(
            "adaptive-v2 convergence membership digest is invalid");
    }

    auto configured_membership = config.membership;
    std::sort(
        configured_membership.begin(),
        configured_membership.end());
    if (configured_membership != bundle_membership)
    {
        throw std::invalid_argument(
            "adaptive-v2 convergence requires complete membership");
    }
    const auto quorum = derive_byzantine_quorum(
        bundle_membership.size());
    if (!quorum.has_value() || quorum->fault_threshold == 0)
    {
        throw std::invalid_argument(
            "adaptive-v2 convergence requires exact N=3f+1");
    }

    const auto definition_digest = compute_epoch_digest(definition);
    const auto &payload = command.payload;
    if (payload.successor_epoch_number != definition.epoch_number ||
        payload.predecessor_epoch_digest !=
            definition.previous_epoch_digest ||
        payload.successor_epoch_digest != definition_digest)
    {
        throw std::invalid_argument(
            "adaptive-v2 convergence bundle identity is invalid");
    }

    const auto payload_digest = epoch_change_payload_digest(payload);
    if (zero_digest(payload.predecessor_epoch_digest) ||
        zero_digest(payload.successor_epoch_digest) ||
        zero_digest(payload_digest))
    {
        throw std::invalid_argument(
            "adaptive-v2 convergence bundle contains a zero digest");
    }

    config.membership = std::move(bundle_membership);
    return {
        std::make_shared<const bytearray_t>(bundle.canonical_bytes()),
        PrecommitIdentity{
            payload.successor_epoch_number - 1,
            payload.predecessor_epoch_digest,
            payload.successor_epoch_number,
            payload.successor_epoch_digest,
            payload_digest,
            payload.activation_delay_blocks},
        std::move(config),
        quorum->quorum};
}

} // namespace

struct AdaptiveV2ManagerConvergence::State
{
    struct RecipientState
    {
        std::uint32_t attempts_issued{0};
        std::uint64_t next_attempt_tick{0};
        bool delivery_exhausted{false};
        bool any_enqueue_succeeded{false};
        bool quarantined{false};
        std::map<std::uint32_t, bool> enqueue_results;
        std::optional<AdaptiveV2EpochChangeCommittedObservation>
            committed;
        std::optional<AdaptiveV2EpochActivatedObservation> activated;
    };

    struct CandidateActivationGroup
    {
        AdaptiveV2EpochChangeIdentity identity;
        std::vector<ReplicaID> sources;
    };

    State(ValidatedStartup startup, std::uint64_t start_tick)
        : bundle(std::move(startup.bundle_bytes)),
          precommit(std::move(startup.precommit)),
          config(std::move(startup.config)),
          required_activations(startup.required_activations)
    {
        for (const auto replica : config.membership)
        {
            RecipientState recipient;
            recipient.next_attempt_tick = start_tick;
            recipients.emplace(replica, std::move(recipient));
        }
    }

    bool awaiting() const noexcept
    {
        return convergence_status ==
               AdaptiveV2ManagerConvergenceStatus::
                   awaiting_activations;
    }

    bool ready() const noexcept
    {
        return convergence_status ==
               AdaptiveV2ManagerConvergenceStatus::
                   ready_for_optimization;
    }

    bool failed() const noexcept
    {
        return convergence_status ==
                   AdaptiveV2ManagerConvergenceStatus::retry_exhausted ||
               convergence_status ==
                   AdaptiveV2ManagerConvergenceStatus::
                       conflicting_observation;
    }

    bool has_member(ReplicaID replica) const noexcept
    {
        return recipients.find(replica) != recipients.end();
    }

    bool deadline_reached(std::uint64_t tick) noexcept
    {
        if (awaiting() &&
            tick >= config.convergence_deadline_tick)
        {
            convergence_status =
                AdaptiveV2ManagerConvergenceStatus::retry_exhausted;
            return true;
        }
        return !awaiting();
    }

    void fail_conflicting() noexcept
    {
        convergence_status = AdaptiveV2ManagerConvergenceStatus::
            conflicting_observation;
        ready_pending = false;
    }

    void update_retry_exhaustion() noexcept
    {
        if (!awaiting())
            return;

        std::size_t possible_activations = 0;
        for (const auto &entry : recipients)
        {
            const auto &recipient = entry.second;
            if (!recipient.quarantined &&
                (recipient.activated.has_value() ||
                recipient.committed.has_value() ||
                recipient.any_enqueue_succeeded ||
                !recipient.delivery_exhausted))
            {
                ++possible_activations;
            }
        }
        if (possible_activations < required_activations)
        {
            convergence_status =
                AdaptiveV2ManagerConvergenceStatus::retry_exhausted;
        }
    }

    AdaptiveV2ManagerConvergenceDisposition validate_source(
        ReplicaID authenticated_replica,
        ReplicaID claimed_replica) const noexcept
    {
        if (!has_member(authenticated_replica))
        {
            return AdaptiveV2ManagerConvergenceDisposition::
                rejected_nonmember;
        }
        if (authenticated_replica != claimed_replica)
        {
            return AdaptiveV2ManagerConvergenceDisposition::
                rejected_spoofed_source;
        }
        return AdaptiveV2ManagerConvergenceDisposition::accepted;
    }

    AdaptiveV2ManagerConvergenceDisposition classify_identity(
        std::uint32_t schema_version,
        const AdaptiveV2EpochChangeIdentity &identity) const noexcept
    {
        if (schema_version !=
            kAdaptiveV2ConvergenceObservationSchemaVersionV1)
        {
            return AdaptiveV2ManagerConvergenceDisposition::
                rejected_wrong_identity;
        }
        if (identity.predecessor_epoch_number <
                precommit.predecessor_epoch_number ||
            identity.successor_epoch_number <
                precommit.successor_epoch_number)
        {
            return AdaptiveV2ManagerConvergenceDisposition::
                rejected_stale;
        }
        if (!valid_identity(identity) ||
            !matches_precommit(identity, precommit))
        {
            return AdaptiveV2ManagerConvergenceDisposition::
                rejected_wrong_identity;
        }
        return AdaptiveV2ManagerConvergenceDisposition::accepted;
    }

    CandidateActivationGroup *find_activation_group(
        const AdaptiveV2EpochChangeIdentity &identity) noexcept
    {
        const auto found = std::find_if(
            activation_groups.begin(),
            activation_groups.end(),
            [&identity](const auto &group) {
                return group.identity == identity;
            });
        return found == activation_groups.end() ? nullptr : &*found;
    }

    std::size_t add_activation_contribution(
        ReplicaID source,
        const AdaptiveV2EpochChangeIdentity &identity)
    {
        auto *group = find_activation_group(identity);
        if (group == nullptr)
        {
            activation_groups.push_back({identity, {}});
            group = &activation_groups.back();
        }
        group->sources.push_back(source);
        return group->sources.size();
    }

    void remove_activation_contribution(
        ReplicaID source,
        const AdaptiveV2EpochChangeIdentity &identity) noexcept
    {
        auto *group = find_activation_group(identity);
        if (group == nullptr)
            return;
        group->sources.erase(
            std::remove(
                group->sources.begin(), group->sources.end(), source),
            group->sources.end());
    }

    void quarantine_source(ReplicaID source) noexcept
    {
        const auto found = recipients.find(source);
        if (found == recipients.end() || found->second.quarantined)
            return;

        auto &recipient = found->second;
        if (recipient.activated.has_value())
        {
            remove_activation_contribution(
                source, recipient.activated->identity);
        }
        recipient.quarantined = true;
    }

    std::size_t activation_contribution_count() const noexcept
    {
        std::size_t count = 0;
        for (const auto &group : activation_groups)
            count += group.sources.size();
        return count;
    }

    std::shared_ptr<const bytearray_t> bundle;
    PrecommitIdentity precommit;
    AdaptiveV2ManagerConvergenceConfig config;
    const std::size_t required_activations;
    std::map<ReplicaID, RecipientState> recipients;
    std::vector<CandidateActivationGroup> activation_groups;
    std::optional<AdaptiveV2EpochChangeIdentity> ready_identity;
    std::vector<ReplicaID> ready_activation_sources;
    std::size_t ready_activation_count{0};
    AdaptiveV2ManagerConvergenceStatus convergence_status{
        AdaptiveV2ManagerConvergenceStatus::awaiting_activations};
    std::size_t commit_count{0};
    bool ready_pending{false};
    bool ready_consumed{false};
};

AdaptiveV2ManagerConvergence::AdaptiveV2ManagerConvergence(
    const AdaptiveV2EpochChangeBundle &bundle,
    AdaptiveV2ManagerConvergenceConfig config,
    std::uint64_t start_tick)
    : state_(std::make_unique<State>(
          validate_startup(bundle, std::move(config), start_tick),
          start_tick))
{}

AdaptiveV2ManagerConvergence::~AdaptiveV2ManagerConvergence() = default;

std::vector<AdaptiveV2ManagerDeliveryRequest>
AdaptiveV2ManagerConvergence::due_deliveries(
    std::uint64_t logical_tick) noexcept
{
    auto &state = *state_;
    std::vector<AdaptiveV2ManagerDeliveryRequest> requests;
    try
    {
        if (state.deadline_reached(logical_tick))
            return requests;
        requests.reserve(state.recipients.size());

        for (const auto replica : state.config.membership)
        {
            auto &recipient = state.recipients.at(replica);
            if (recipient.committed.has_value() ||
                recipient.activated.has_value() ||
                recipient.delivery_exhausted ||
                logical_tick < recipient.next_attempt_tick)
            {
                continue;
            }
            if (recipient.attempts_issued >=
                state.config.maximum_attempts_per_recipient)
            {
                recipient.delivery_exhausted = true;
                continue;
            }

            ++recipient.attempts_issued;
            if (logical_tick >
                std::numeric_limits<std::uint64_t>::max() -
                    state.config.retry_interval_ticks)
            {
                recipient.next_attempt_tick =
                    state.config.convergence_deadline_tick;
            }
            else
            {
                recipient.next_attempt_tick =
                    logical_tick + state.config.retry_interval_ticks;
            }
            requests.push_back(
                {replica,
                 recipient.attempts_issued,
                 state.bundle.get(),
                 state.bundle});
        }

        state.update_retry_exhaustion();
        if (!state.awaiting())
            requests.clear();
        return requests;
    }
    catch (...)
    {
        state.convergence_status =
            AdaptiveV2ManagerConvergenceStatus::retry_exhausted;
        state.ready_pending = false;
        return {};
    }
}

AdaptiveV2ManagerConvergenceDisposition
AdaptiveV2ManagerConvergence::record_enqueue_result(
    ReplicaID recipient,
    std::uint32_t attempt,
    bool enqueued) noexcept
{
    auto &state = *state_;
    if (!state.has_member(recipient))
    {
        return AdaptiveV2ManagerConvergenceDisposition::
            rejected_nonmember;
    }
    if (!state.awaiting())
        return AdaptiveV2ManagerConvergenceDisposition::terminal;

    try
    {
        auto &recipient_state = state.recipients.at(recipient);
        if (attempt == 0 ||
            attempt > recipient_state.attempts_issued)
        {
            return AdaptiveV2ManagerConvergenceDisposition::
                rejected_stale;
        }
        const auto found =
            recipient_state.enqueue_results.find(attempt);
        if (found != recipient_state.enqueue_results.end())
        {
            if (found->second == enqueued)
                return AdaptiveV2ManagerConvergenceDisposition::duplicate;
            return AdaptiveV2ManagerConvergenceDisposition::
                conflicting_enqueue_result;
        }
        recipient_state.enqueue_results.emplace(attempt, enqueued);
        if (enqueued)
            recipient_state.any_enqueue_succeeded = true;
        return AdaptiveV2ManagerConvergenceDisposition::
            advisory_enqueue_recorded;
    }
    catch (...)
    {
        state.convergence_status =
            AdaptiveV2ManagerConvergenceStatus::retry_exhausted;
        return AdaptiveV2ManagerConvergenceDisposition::terminal;
    }
}

AdaptiveV2ManagerConvergenceDisposition
AdaptiveV2ManagerConvergence::observe_commit(
    ReplicaID authenticated_replica,
    const AdaptiveV2EpochChangeCommittedObservation &observation) noexcept
{
    auto &state = *state_;
    const auto source = state.validate_source(
        authenticated_replica,
        observation.claimed_source_replica_id);
    if (source != AdaptiveV2ManagerConvergenceDisposition::accepted)
        return source;

    auto &recipient = state.recipients.at(authenticated_replica);
    if (state.ready())
    {
        if (!recipient.quarantined &&
            state.ready_identity.has_value() &&
            observation.schema_version ==
                kAdaptiveV2ConvergenceObservationSchemaVersionV1 &&
            observation.identity == *state.ready_identity &&
            (!recipient.committed.has_value() ||
             same_committed_observation(
                 *recipient.committed, observation)) &&
            (!recipient.activated.has_value() ||
             recipient.activated->identity == observation.identity))
        {
            return AdaptiveV2ManagerConvergenceDisposition::duplicate;
        }
        return AdaptiveV2ManagerConvergenceDisposition::terminal;
    }
    if (state.failed())
        return AdaptiveV2ManagerConvergenceDisposition::terminal;

    const auto identity = state.classify_identity(
        observation.schema_version, observation.identity);
    if (identity != AdaptiveV2ManagerConvergenceDisposition::accepted)
        return identity;
    if (recipient.quarantined)
    {
        return AdaptiveV2ManagerConvergenceDisposition::
            conflicting_observation;
    }
    if (recipient.committed.has_value())
    {
        if (same_committed_observation(
                *recipient.committed, observation))
        {
            return AdaptiveV2ManagerConvergenceDisposition::duplicate;
        }
        state.quarantine_source(authenticated_replica);
        return AdaptiveV2ManagerConvergenceDisposition::
            conflicting_observation;
    }
    if (recipient.activated.has_value() &&
        recipient.activated->identity != observation.identity)
    {
        state.quarantine_source(authenticated_replica);
        return AdaptiveV2ManagerConvergenceDisposition::
            conflicting_observation;
    }

    try
    {
        recipient.committed = observation;
        ++state.commit_count;
        return AdaptiveV2ManagerConvergenceDisposition::accepted;
    }
    catch (...)
    {
        state.fail_conflicting();
        return AdaptiveV2ManagerConvergenceDisposition::terminal;
    }
}

AdaptiveV2ManagerConvergenceDisposition
AdaptiveV2ManagerConvergence::observe_activation(
    ReplicaID authenticated_replica,
    const AdaptiveV2EpochActivatedObservation &observation) noexcept
{
    auto &state = *state_;
    const auto source = state.validate_source(
        authenticated_replica,
        observation.claimed_source_replica_id);
    if (source != AdaptiveV2ManagerConvergenceDisposition::accepted)
        return source;

    auto &recipient = state.recipients.at(authenticated_replica);
    if (state.ready())
    {
        if (!recipient.quarantined &&
            state.ready_identity.has_value() &&
            observation.schema_version ==
                kAdaptiveV2ConvergenceObservationSchemaVersionV1 &&
            observation.identity == *state.ready_identity &&
            observation.activated_epoch_number ==
                observation.identity.successor_epoch_number &&
            observation.activated_epoch_digest ==
                observation.identity.successor_epoch_digest &&
            (!recipient.activated.has_value() ||
             same_activated_observation(
                 *recipient.activated, observation)) &&
            (!recipient.committed.has_value() ||
             recipient.committed->identity == observation.identity))
        {
            return AdaptiveV2ManagerConvergenceDisposition::duplicate;
        }
        return AdaptiveV2ManagerConvergenceDisposition::terminal;
    }
    if (state.failed())
        return AdaptiveV2ManagerConvergenceDisposition::terminal;

    auto identity = state.classify_identity(
        observation.schema_version, observation.identity);
    if (identity == AdaptiveV2ManagerConvergenceDisposition::accepted &&
        (observation.activated_epoch_number !=
             observation.identity.successor_epoch_number ||
         observation.activated_epoch_digest !=
             observation.identity.successor_epoch_digest))
    {
        identity = AdaptiveV2ManagerConvergenceDisposition::
            rejected_wrong_identity;
    }
    if (identity != AdaptiveV2ManagerConvergenceDisposition::accepted)
        return identity;
    if (recipient.quarantined)
    {
        return AdaptiveV2ManagerConvergenceDisposition::
            conflicting_observation;
    }
    if (recipient.activated.has_value())
    {
        if (same_activated_observation(
                *recipient.activated, observation))
        {
            return AdaptiveV2ManagerConvergenceDisposition::duplicate;
        }
        state.quarantine_source(authenticated_replica);
        return AdaptiveV2ManagerConvergenceDisposition::
            conflicting_observation;
    }
    if (recipient.committed.has_value() &&
        recipient.committed->identity != observation.identity)
    {
        state.quarantine_source(authenticated_replica);
        return AdaptiveV2ManagerConvergenceDisposition::
            conflicting_observation;
    }

    bool contribution_added = false;
    try
    {
        const auto group_count = state.add_activation_contribution(
            authenticated_replica, observation.identity);
        contribution_added = true;
        recipient.activated = observation;
        if (group_count >= state.required_activations)
        {
            const auto *winning_group =
                state.find_activation_group(observation.identity);
            if (winning_group == nullptr ||
                winning_group->sources.size() != group_count)
            {
                state.remove_activation_contribution(
                    authenticated_replica, observation.identity);
                recipient.activated.reset();
                state.fail_conflicting();
                return AdaptiveV2ManagerConvergenceDisposition::
                    terminal;
            }
            auto winning_sources = winning_group->sources;
            std::sort(
                winning_sources.begin(), winning_sources.end());
            if (std::adjacent_find(
                    winning_sources.begin(),
                    winning_sources.end()) != winning_sources.end())
            {
                state.remove_activation_contribution(
                    authenticated_replica, observation.identity);
                recipient.activated.reset();
                state.fail_conflicting();
                return AdaptiveV2ManagerConvergenceDisposition::
                    terminal;
            }
            state.ready_identity = observation.identity;
            state.ready_activation_sources =
                std::move(winning_sources);
            state.ready_activation_count = group_count;
            state.convergence_status =
                AdaptiveV2ManagerConvergenceStatus::
                    ready_for_optimization;
            state.ready_pending = !state.ready_consumed;
        }
        return AdaptiveV2ManagerConvergenceDisposition::accepted;
    }
    catch (...)
    {
        if (contribution_added)
        {
            state.remove_activation_contribution(
                authenticated_replica, observation.identity);
            recipient.activated.reset();
        }
        state.fail_conflicting();
        return AdaptiveV2ManagerConvergenceDisposition::terminal;
    }
}

AdaptiveV2ManagerConvergenceStatus
AdaptiveV2ManagerConvergence::status() const noexcept
{
    return state_->convergence_status;
}

std::size_t
AdaptiveV2ManagerConvergence::accepted_commit_count() const noexcept
{
    return state_->commit_count;
}

std::size_t
AdaptiveV2ManagerConvergence::accepted_activation_count() const noexcept
{
    return state_->activation_contribution_count();
}

const AdaptiveV2EpochChangeIdentity *
AdaptiveV2ManagerConvergence::winning_identity() const noexcept
{
    return state_->ready_identity.has_value()
               ? &*state_->ready_identity
               : nullptr;
}

std::size_t
AdaptiveV2ManagerConvergence::winning_activation_count() const noexcept
{
    return state_->ready_activation_count;
}

const std::vector<ReplicaID> &
AdaptiveV2ManagerConvergence::winning_activation_sources() const noexcept
{
    return state_->ready_activation_sources;
}

bool AdaptiveV2ManagerConvergence::consume_ready_for_optimization()
    noexcept
{
    auto &state = *state_;
    if (!state.ready() || !state.ready_pending || state.ready_consumed)
        return false;
    state.ready_pending = false;
    state.ready_consumed = true;
    return true;
}

} // namespace hotstuff
