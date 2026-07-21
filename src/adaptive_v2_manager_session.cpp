#include "hotstuff/adaptive_v2_manager_session.h"

#include "hotstuff/epoch_activation.h"

#include <algorithm>
#include <limits>
#include <optional>
#include <stdexcept>
#include <utility>

namespace hotstuff
{
namespace
{

bool checked_add(
    std::uint64_t left,
    std::uint64_t right,
    std::uint64_t &result) noexcept
{
    if (left > std::numeric_limits<std::uint64_t>::max() - right)
        return false;
    result = left + right;
    return true;
}

bool valid_policy(
    const AdaptiveV2TransitionPolicy &policy,
    std::uint32_t tree_count) noexcept
{
    switch (policy.intent)
    {
    case TreePolicyKind::fault_containment:
        if (policy.containment_baseline_roots.size() != tree_count)
            return false;
        for (std::size_t index = 0;
             index < policy.containment_baseline_roots.size();
             ++index)
        {
            const auto tree_id =
                policy.containment_baseline_roots[index].tree_id;
            if (tree_id >= tree_count)
                return false;
            for (std::size_t previous = 0;
                 previous < index;
                 ++previous)
            {
                if (policy.containment_baseline_roots[previous].tree_id ==
                    tree_id)
                {
                    return false;
                }
            }
        }
        return true;
    case TreePolicyKind::performance_optimization:
        return policy.containment_baseline_roots.empty();
    }
    return false;
}

bool contains_tree(
    const EpochDefinitionInput &definition,
    std::uint32_t tree_id) noexcept
{
    return std::any_of(
        definition.trees.begin(),
        definition.trees.end(),
        [tree_id](const EpochTreeDefinition &tree) {
            return tree.tree_id == tree_id;
        });
}

bool exact_terminal_identity(
    const AdaptiveV2ManagerIngress &ingress,
    const AdaptiveV2EpochChangeBundle &bundle,
    const AdaptiveV2EpochChangeIdentity &identity,
    std::uint32_t active_tree_id) noexcept
{
    if (identity.command_block_height == 0 ||
        identity.command_block_hash == uint256_t{})
    {
        return false;
    }

    const auto successor = checked_successor_epoch(
        ingress.current_epoch().epoch_number());
    if (!successor.has_value())
        return false;

    const auto &definition = bundle.definition();
    const auto &payload = bundle.command().payload;
    std::uint64_t activation_height = 0;
    if (!checked_add(
            identity.command_block_height,
            payload.activation_delay_blocks,
            activation_height))
    {
        return false;
    }

    return definition.schema_version ==
               kEpochDefinitionSchemaVersionV2 &&
           definition.epoch_number == *successor &&
           definition.previous_epoch_digest ==
               ingress.current_epoch().epoch_digest() &&
           definition.membership_digest ==
               ingress.current_epoch().membership_digest() &&
           contains_tree(definition, active_tree_id) &&
           payload.successor_epoch_number == definition.epoch_number &&
           payload.predecessor_epoch_digest ==
               definition.previous_epoch_digest &&
           payload.successor_epoch_digest ==
               compute_epoch_digest(definition) &&
           identity.predecessor_epoch_number ==
               ingress.current_epoch().epoch_number() &&
           identity.predecessor_epoch_digest ==
               ingress.current_epoch().epoch_digest() &&
           identity.successor_epoch_number ==
               payload.successor_epoch_number &&
           identity.successor_epoch_digest ==
               payload.successor_epoch_digest &&
           identity.command_payload_digest ==
               epoch_change_payload_digest(payload) &&
           identity.activation_delay_blocks ==
               payload.activation_delay_blocks &&
           identity.activation_height == activation_height;
}

AdaptiveV2ManagerConvergenceDisposition classify_observation_source(
    const AdaptiveV2ManagerIngress &ingress,
    ReplicaID authenticated_replica,
    ReplicaID claimed_replica) noexcept
{
    const auto &members = ingress.membership();
    if (!std::binary_search(
            members.begin(), members.end(), authenticated_replica))
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

} // namespace

struct AdaptiveV2ManagerSession::State
{
    enum class Phase : std::uint8_t
    {
        evidence_window_open = 1,
        collecting_cycle,
        successor_available,
        observing_successor,
        unavailable,
    };

    State(
        std::vector<ReplicaID> membership,
        EpochDefinitionInput initial_epoch,
        AdaptiveV2ManagerSessionConfig config_)
        : config(std::move(config_)),
          ingress(
              std::move(membership),
              std::move(initial_epoch),
              config.active_tree_id,
              config.activation_generation,
              config.ingress_limits)
    {
        auto placement_membership =
            config.controller.placement.membership;
        std::sort(
            placement_membership.begin(),
            placement_membership.end());
        if (placement_membership != ingress.membership() ||
            config.controller.placement.shape.tree_count !=
                ingress.quorum_metadata().quorum ||
            config.active_tree_id >=
                config.controller.placement.shape.tree_count ||
            config.controller.activation_delay_blocks == 0 ||
            config.retry_interval_ticks == 0 ||
            config.maximum_attempts_per_recipient == 0 ||
            config.convergence_window_ticks == 0)
        {
            throw std::invalid_argument(
                "invalid adaptive-v2 manager session configuration");
        }
        commit_bindings.reserve(ingress.membership().size());
    }

    bool owns_cycle() const noexcept
    {
        return (phase == Phase::collecting_cycle ||
                phase == Phase::successor_available ||
                phase == Phase::observing_successor) &&
               controller != nullptr && current_policy.has_value();
    }

    bool append_terminal(
        AdaptiveV2ManagerCycleOutcome outcome,
        AdaptiveV2ManagerCycleTerminalReason reason,
        const AdaptiveV2EpochChangeIdentity *winning) noexcept
    {
        if (!owns_cycle() || records.capacity() <= records.size())
            return false;

        AdaptiveV2ManagerSessionTerminalRecord record;
        record.cycle_ordinal = next_cycle_ordinal;
        record.policy_intent = current_policy->intent;
        record.outcome = outcome;
        record.reason = reason;
        record.predecessor_epoch_number =
            ingress.current_epoch().epoch_number();
        record.predecessor_epoch_digest =
            ingress.current_epoch().epoch_digest();

        if (outcome != AdaptiveV2ManagerCycleOutcome::no_op &&
            controller != nullptr)
        {
            const auto *bundle = controller->successor_bundle();
            if (bundle != nullptr)
            {
                record.successor_epoch_number =
                    bundle->definition().epoch_number;
                record.successor_epoch_digest =
                    bundle->command().payload.successor_epoch_digest;
                record.command_payload_digest =
                    epoch_change_payload_digest(
                        bundle->command().payload);
            }
        }
        if (winning != nullptr)
            record.winning_activation = *winning;

        try
        {
            records.push_back(std::move(record));
            return true;
        }
        catch (...)
        {
            return false;
        }
    }

    const AdaptiveV2EpochChangeIdentity *commit_binding(
        ReplicaID source) const noexcept
    {
        const auto found = std::find_if(
            commit_bindings.begin(),
            commit_bindings.end(),
            [source](const auto &binding) {
                return binding.first == source;
            });
        return found == commit_bindings.end()
                   ? nullptr
                   : &found->second;
    }

    bool bind_commit(
        ReplicaID source,
        const AdaptiveV2EpochChangeIdentity &identity) noexcept
    {
        if (commit_binding(source) != nullptr ||
            commit_bindings.size() >= commit_bindings.capacity())
        {
            return false;
        }
        try
        {
            commit_bindings.emplace_back(source, identity);
            return true;
        }
        catch (...)
        {
            return false;
        }
    }

    void release_cycle(Phase next_phase) noexcept
    {
        ++next_cycle_ordinal;
        convergence.reset();
        controller.reset();
        current_policy.reset();
        commit_bindings.clear();
        phase = next_phase;
    }

    AdaptiveV2ManagerSessionConfig config;
    AdaptiveV2ManagerIngress ingress;
    std::unique_ptr<AdaptiveV2ManagerController> controller;
    std::unique_ptr<AdaptiveV2ManagerConvergence> convergence;
    std::optional<AdaptiveV2TransitionPolicy> current_policy;
    std::vector<std::pair<
        ReplicaID,
        AdaptiveV2EpochChangeIdentity>> commit_bindings;
    std::vector<AdaptiveV2ManagerSessionTerminalRecord> records;
    std::uint64_t next_cycle_ordinal{0};
    Phase phase{Phase::evidence_window_open};
};

AdaptiveV2ManagerSession::AdaptiveV2ManagerSession(
    std::vector<ReplicaID> membership,
    EpochDefinitionInput initial_epoch,
    AdaptiveV2ManagerSessionConfig config)
    : state_(std::make_unique<State>(
          std::move(membership),
          std::move(initial_epoch),
          std::move(config)))
{}

AdaptiveV2ManagerSession::~AdaptiveV2ManagerSession() = default;

const AdaptiveV2ManagerIngress &
AdaptiveV2ManagerSession::ingress() const noexcept
{
    return state_->ingress;
}

AdaptiveV2ManagerReadinessResult
AdaptiveV2ManagerSession::ingest_readiness(
    const AuthenticatedReporter &authenticated_source,
    const MsgAdaptiveV2ReadinessNotice &message) noexcept
{
    auto &state = *state_;
    if (state.phase == State::Phase::unavailable)
    {
        return {AdaptiveV2ManagerIngressStatus::stopped,
                std::nullopt,
                state.ingress.readiness_stats()};
    }
    auto result = state.ingress.ingest_readiness(
        authenticated_source, message);
    if (result.status ==
            AdaptiveV2ManagerIngressStatus::evidence_unhealthy &&
        state.owns_cycle())
    {
        static_cast<void>(finalize_failed_cycle(
            AdaptiveV2ManagerCycleTerminalReason::
                controller_unhealthy));
    }
    return result;
}

AdaptiveV2ManagerReadinessResult
AdaptiveV2ManagerSession::ingest_readiness(
    const AuthenticatedReporter &authenticated_source,
    const bytearray_t &canonical_payload) noexcept
{
    auto &state = *state_;
    if (state.phase == State::Phase::unavailable)
    {
        return {AdaptiveV2ManagerIngressStatus::stopped,
                std::nullopt,
                state.ingress.readiness_stats()};
    }
    auto result = state.ingress.ingest_readiness(
        authenticated_source, canonical_payload);
    if (result.status ==
            AdaptiveV2ManagerIngressStatus::evidence_unhealthy &&
        state.owns_cycle())
    {
        static_cast<void>(finalize_failed_cycle(
            AdaptiveV2ManagerCycleTerminalReason::
                controller_unhealthy));
    }
    return result;
}

AdaptiveV2ManagerLifecycleResult
AdaptiveV2ManagerSession::ingest_lifecycle(
    const AuthenticatedReporter &authenticated_source,
    const MsgProposalLifecycleNotice &message) noexcept
{
    auto &state = *state_;
    if (state.phase == State::Phase::unavailable)
    {
        AdaptiveV2ManagerLifecycleResult stopped;
        stopped.status = AdaptiveV2ManagerIngressStatus::stopped;
        return stopped;
    }
    auto result = state.ingress.ingest_lifecycle(
        authenticated_source, message);
    if (result.status ==
            AdaptiveV2ManagerIngressStatus::evidence_unhealthy &&
        state.owns_cycle())
    {
        static_cast<void>(finalize_failed_cycle(
            AdaptiveV2ManagerCycleTerminalReason::
                controller_unhealthy));
    }
    return result;
}

AdaptiveV2ManagerLifecycleResult
AdaptiveV2ManagerSession::ingest_lifecycle(
    const AuthenticatedReporter &authenticated_source,
    const bytearray_t &canonical_payload) noexcept
{
    auto &state = *state_;
    if (state.phase == State::Phase::unavailable)
    {
        AdaptiveV2ManagerLifecycleResult stopped;
        stopped.status = AdaptiveV2ManagerIngressStatus::stopped;
        return stopped;
    }
    auto result = state.ingress.ingest_lifecycle(
        authenticated_source, canonical_payload);
    if (result.status ==
            AdaptiveV2ManagerIngressStatus::evidence_unhealthy &&
        state.owns_cycle())
    {
        static_cast<void>(finalize_failed_cycle(
            AdaptiveV2ManagerCycleTerminalReason::
                controller_unhealthy));
    }
    return result;
}

AdaptiveV2ManagerEvidenceResult
AdaptiveV2ManagerSession::ingest_evidence(
    const AuthenticatedReporter &authenticated_reporter,
    const MsgEvidenceReport &message) noexcept
{
    auto &state = *state_;
    if (state.phase == State::Phase::unavailable)
    {
        AdaptiveV2ManagerEvidenceResult stopped;
        stopped.status = AdaptiveV2ManagerIngressStatus::stopped;
        stopped.ledger_high_watermark =
            state.ingress.ledger().high_watermark();
        return stopped;
    }
    auto result = state.ingress.ingest_evidence(
        authenticated_reporter, message);
    if (result.status ==
            AdaptiveV2ManagerIngressStatus::evidence_unhealthy &&
        state.owns_cycle())
    {
        static_cast<void>(finalize_failed_cycle(
            AdaptiveV2ManagerCycleTerminalReason::
                controller_unhealthy));
    }
    return result;
}

AdaptiveV2ManagerEvidenceResult
AdaptiveV2ManagerSession::ingest_evidence(
    const AuthenticatedReporter &authenticated_reporter,
    const bytearray_t &canonical_payload) noexcept
{
    auto &state = *state_;
    if (state.phase == State::Phase::unavailable)
    {
        AdaptiveV2ManagerEvidenceResult stopped;
        stopped.status = AdaptiveV2ManagerIngressStatus::stopped;
        stopped.ledger_high_watermark =
            state.ingress.ledger().high_watermark();
        return stopped;
    }
    auto result = state.ingress.ingest_evidence(
        authenticated_reporter, canonical_payload);
    if (result.status ==
            AdaptiveV2ManagerIngressStatus::evidence_unhealthy &&
        state.owns_cycle())
    {
        static_cast<void>(finalize_failed_cycle(
            AdaptiveV2ManagerCycleTerminalReason::
                controller_unhealthy));
    }
    return result;
}

bool AdaptiveV2ManagerSession::begin_cycle(
    const AdaptiveV2TransitionPolicy &policy) noexcept
{
    auto &state = *state_;
    const auto successor = checked_successor_epoch(
        state.ingress.current_epoch().epoch_number());
    const auto successor_generation = successor.has_value()
        ? checked_activation_generation(*successor, 0)
        : std::nullopt;
    if (state.phase != State::Phase::evidence_window_open ||
        state.controller != nullptr || state.convergence != nullptr ||
        state.current_policy.has_value() ||
        !state.commit_bindings.empty() ||
        !state.ingress.healthy() ||
        !successor.has_value() ||
        !successor_generation.has_value() ||
        state.ingress.activation_generation() ==
            std::numeric_limits<std::uint64_t>::max() ||
        state.next_cycle_ordinal ==
            std::numeric_limits<std::uint64_t>::max() ||
        state.records.size() == state.records.max_size() ||
        !valid_policy(
            policy,
            state.config.controller.placement.shape.tree_count))
    {
        return false;
    }

    try
    {
        auto frozen_policy = policy;
        auto controller_config = state.config.controller;
        controller_config.transition_policy = frozen_policy;
        auto controller =
            std::make_unique<AdaptiveV2ManagerController>(
                state.ingress, std::move(controller_config));

        state.records.reserve(state.records.size() + 1);
        state.current_policy.emplace(std::move(frozen_policy));
        state.controller = std::move(controller);
        state.phase = State::Phase::collecting_cycle;
        return true;
    }
    catch (...)
    {
        return false;
    }
}

AdaptiveV2ManagerControllerStatus
AdaptiveV2ManagerSession::evaluate() noexcept
{
    auto &state = *state_;
    if ((state.phase != State::Phase::collecting_cycle &&
         state.phase != State::Phase::successor_available) ||
        state.controller == nullptr || state.convergence != nullptr ||
        !state.current_policy.has_value())
    {
        return AdaptiveV2ManagerControllerStatus::unhealthy;
    }

    const auto result = state.controller->evaluate();
    if (result == AdaptiveV2ManagerControllerStatus::unhealthy)
    {
        static_cast<void>(finalize_failed_cycle(
            AdaptiveV2ManagerCycleTerminalReason::
                controller_unhealthy));
        return result;
    }
    if (result == AdaptiveV2ManagerControllerStatus::successor_ready ||
        result == AdaptiveV2ManagerControllerStatus::already_ready)
    {
        state.phase = State::Phase::successor_available;
    }
    return result;
}

const AdaptiveV2EpochChangeBundle *
AdaptiveV2ManagerSession::successor_bundle() const noexcept
{
    return state_->controller == nullptr
               ? nullptr
               : state_->controller->successor_bundle();
}

bool AdaptiveV2ManagerSession::start_convergence(
    std::uint64_t logical_start_tick) noexcept
{
    auto &state = *state_;
    const auto *bundle = successor_bundle();
    if (state.phase != State::Phase::successor_available ||
        state.controller == nullptr || state.convergence != nullptr ||
        !state.current_policy.has_value())
    {
        return false;
    }
    if (bundle == nullptr)
    {
        static_cast<void>(finalize_failed_cycle(
            AdaptiveV2ManagerCycleTerminalReason::
                convergence_start_failed));
        return false;
    }

    std::uint64_t deadline = 0;
    if (!checked_add(
            logical_start_tick,
            state.config.convergence_window_ticks,
            deadline))
    {
        static_cast<void>(finalize_failed_cycle(
            AdaptiveV2ManagerCycleTerminalReason::
                convergence_start_failed));
        return false;
    }
    try
    {
        AdaptiveV2ManagerConvergenceConfig convergence_config;
        convergence_config.membership = state.ingress.membership();
        convergence_config.retry_interval_ticks =
            state.config.retry_interval_ticks;
        convergence_config.maximum_attempts_per_recipient =
            state.config.maximum_attempts_per_recipient;
        convergence_config.convergence_deadline_tick = deadline;
        auto convergence =
            std::make_unique<AdaptiveV2ManagerConvergence>(
                *bundle,
                std::move(convergence_config),
                logical_start_tick);

        state.convergence = std::move(convergence);
        state.phase = State::Phase::observing_successor;
        return true;
    }
    catch (...)
    {
        static_cast<void>(finalize_failed_cycle(
            AdaptiveV2ManagerCycleTerminalReason::
                convergence_start_failed));
        return false;
    }
}

std::vector<AdaptiveV2ManagerDeliveryRequest>
AdaptiveV2ManagerSession::due_deliveries(
    std::uint64_t logical_tick) noexcept
{
    auto &state = *state_;
    if (state.phase != State::Phase::observing_successor ||
        state.convergence == nullptr)
    {
        return {};
    }
    auto deliveries = state.convergence->due_deliveries(logical_tick);
    static_cast<void>(finalize_convergence_failure_if_needed());
    return deliveries;
}

AdaptiveV2ManagerConvergenceDisposition
AdaptiveV2ManagerSession::record_enqueue_result(
    ReplicaID recipient,
    std::uint32_t attempt,
    bool enqueued) noexcept
{
    auto &state = *state_;
    if (state.phase != State::Phase::observing_successor ||
        state.convergence == nullptr)
    {
        return AdaptiveV2ManagerConvergenceDisposition::terminal;
    }
    const auto result = state.convergence->record_enqueue_result(
        recipient, attempt, enqueued);
    static_cast<void>(finalize_convergence_failure_if_needed());
    return result;
}

AdaptiveV2ManagerConvergenceDisposition
AdaptiveV2ManagerSession::observe_commit(
    ReplicaID authenticated_replica,
    const AdaptiveV2EpochChangeCommittedObservation &observation)
    noexcept
{
    bool terminal_match = false;
    const auto terminal = classify_terminal_commit(
        authenticated_replica, observation, terminal_match);
    if (terminal_match)
        return terminal;

    auto &state = *state_;
    if (state.phase != State::Phase::observing_successor ||
        state.convergence == nullptr)
    {
        return AdaptiveV2ManagerConvergenceDisposition::
            rejected_wrong_identity;
    }
    const auto result = state.convergence->observe_commit(
        authenticated_replica, observation);
    if (result == AdaptiveV2ManagerConvergenceDisposition::accepted &&
        !state.bind_commit(
            authenticated_replica, observation.identity))
    {
        static_cast<void>(finalize_failed_cycle(
            AdaptiveV2ManagerCycleTerminalReason::
                invalid_terminal_identity));
        return AdaptiveV2ManagerConvergenceDisposition::terminal;
    }
    static_cast<void>(finalize_convergence_failure_if_needed());
    return result;
}

AdaptiveV2ManagerConvergenceDisposition
AdaptiveV2ManagerSession::observe_activation(
    ReplicaID authenticated_replica,
    const AdaptiveV2EpochActivatedObservation &observation) noexcept
{
    bool terminal_match = false;
    const auto terminal = classify_terminal_activation(
        authenticated_replica, observation, terminal_match);
    if (terminal_match)
        return terminal;

    auto &state = *state_;
    if (state.phase != State::Phase::observing_successor ||
        state.convergence == nullptr)
    {
        return AdaptiveV2ManagerConvergenceDisposition::
            rejected_wrong_identity;
    }
    if (state.convergence->status() ==
        AdaptiveV2ManagerConvergenceStatus::awaiting_activations)
    {
        const auto source = classify_observation_source(
            state.ingress,
            authenticated_replica,
            observation.claimed_source_replica_id);
        if (source !=
            AdaptiveV2ManagerConvergenceDisposition::accepted)
        {
            return source;
        }
        if (state.commit_binding(authenticated_replica) == nullptr)
        {
            return AdaptiveV2ManagerConvergenceDisposition::
                rejected_wrong_identity;
        }
    }
    const auto result = state.convergence->observe_activation(
        authenticated_replica, observation);
    static_cast<void>(finalize_convergence_failure_if_needed());
    return result;
}

std::optional<AdaptiveV2ManagerConvergenceStatus>
AdaptiveV2ManagerSession::convergence_status() const noexcept
{
    return state_->convergence == nullptr
               ? std::nullopt
               : std::optional<AdaptiveV2ManagerConvergenceStatus>{
                     state_->convergence->status()};
}

std::optional<AdaptiveV2ManagerControllerAuditSnapshot>
AdaptiveV2ManagerSession::controller_audit() const noexcept
{
    const auto &state = *state_;
    if (state.controller == nullptr)
        return std::nullopt;
    try
    {
        AdaptiveV2ManagerControllerAuditSnapshot snapshot;
        snapshot.baseline_cutoff =
            state.controller->baseline_cutoff();
        snapshot.current_cutoff =
            state.controller->current_cutoff();
        snapshot.score_trajectory =
            state.controller->score_trajectory();
        return snapshot;
    }
    catch (...)
    {
        return std::nullopt;
    }
}

std::optional<AdaptiveV2ManagerConvergenceAuditSnapshot>
AdaptiveV2ManagerSession::convergence_audit() const noexcept
{
    const auto &state = *state_;
    if (state.convergence == nullptr)
        return std::nullopt;
    try
    {
        AdaptiveV2ManagerConvergenceAuditSnapshot snapshot;
        snapshot.status = state.convergence->status();
        snapshot.accepted_commit_count =
            state.convergence->accepted_commit_count();
        snapshot.accepted_activation_count =
            state.convergence->accepted_activation_count();
        snapshot.winning_activation_count =
            state.convergence->winning_activation_count();
        const auto *identity =
            state.convergence->winning_identity();
        if (identity != nullptr)
            snapshot.winning_identity = *identity;
        snapshot.winning_activation_sources =
            state.convergence->winning_activation_sources();
        return snapshot;
    }
    catch (...)
    {
        return std::nullopt;
    }
}

bool AdaptiveV2ManagerSession::consume_ready_and_rotate() noexcept
{
    auto &state = *state_;
    if (state.phase != State::Phase::observing_successor ||
        state.controller == nullptr || state.convergence == nullptr ||
        !state.current_policy.has_value() ||
        state.convergence->status() !=
            AdaptiveV2ManagerConvergenceStatus::
                ready_for_optimization)
    {
        return false;
    }

    const auto *bundle = state.controller->successor_bundle();
    const auto *identity = state.convergence->winning_identity();
    const auto &winning_sources =
        state.convergence->winning_activation_sources();
    if (bundle == nullptr || identity == nullptr ||
        winning_sources.size() !=
            state.convergence->winning_activation_count() ||
        winning_sources.size() != static_cast<std::size_t>(
            state.ingress.quorum_metadata().quorum) ||
        state.records.capacity() <= state.records.size() ||
        !exact_terminal_identity(
            state.ingress,
            *bundle,
            *identity,
            state.config.active_tree_id))
    {
        static_cast<void>(finalize_failed_cycle(
            AdaptiveV2ManagerCycleTerminalReason::
                invalid_terminal_identity));
        return false;
    }

    const auto prepared = state.ingress.prepare_successor_rotation(
        bundle->definition(), state.config.active_tree_id);
    if (prepared != AdaptiveV2ManagerIngressStatus::processed)
    {
        static_cast<void>(finalize_failed_cycle(
            AdaptiveV2ManagerCycleTerminalReason::
                successor_rotation_failed));
        return false;
    }

    const auto seeded =
        state.ingress.seed_prepared_successor_readiness(
            winning_sources, identity->activation_height);
    if (seeded != AdaptiveV2ManagerIngressStatus::processed)
    {
        state.ingress.discard_prepared_window();
        static_cast<void>(finalize_failed_cycle(
            AdaptiveV2ManagerCycleTerminalReason::
                successor_rotation_failed));
        return false;
    }

    if (!state.convergence->consume_ready_for_optimization())
    {
        state.ingress.discard_prepared_window();
        static_cast<void>(finalize_failed_cycle(
            AdaptiveV2ManagerCycleTerminalReason::
                invalid_terminal_identity));
        return false;
    }

    if (!state.append_terminal(
            AdaptiveV2ManagerCycleOutcome::advanced,
            AdaptiveV2ManagerCycleTerminalReason::successor_converged,
            identity))
    {
        state.ingress.discard_prepared_window();
        return false;
    }

    state.ingress.publish_prepared_window();
    state.release_cycle(State::Phase::evidence_window_open);
    return true;
}

bool AdaptiveV2ManagerSession::finalize_noop_cycle(
    AdaptiveV2ManagerCycleTerminalReason reason) noexcept
{
    const auto valid_reason =
        reason == AdaptiveV2ManagerCycleTerminalReason::explicit_no_op;
    auto &state = *state_;
    if (!valid_reason ||
        state.phase != State::Phase::collecting_cycle ||
        state.convergence != nullptr || !state.owns_cycle())
    {
        return false;
    }

    const auto prepared =
        state.ingress.prepare_same_epoch_window_reset();
    if (prepared != AdaptiveV2ManagerIngressStatus::processed)
    {
        static_cast<void>(finalize_failed_cycle(
            AdaptiveV2ManagerCycleTerminalReason::
                evidence_window_reset_failed));
        return false;
    }
    if (!state.append_terminal(
            AdaptiveV2ManagerCycleOutcome::no_op, reason, nullptr))
    {
        state.ingress.discard_prepared_window();
        return false;
    }

    state.ingress.publish_prepared_window();
    state.release_cycle(State::Phase::evidence_window_open);
    return true;
}

bool AdaptiveV2ManagerSession::finalize_failed_cycle(
    AdaptiveV2ManagerCycleTerminalReason reason) noexcept
{
    switch (reason)
    {
    case AdaptiveV2ManagerCycleTerminalReason::controller_unhealthy:
    case AdaptiveV2ManagerCycleTerminalReason::convergence_start_failed:
    case AdaptiveV2ManagerCycleTerminalReason::
        convergence_retry_exhausted:
    case AdaptiveV2ManagerCycleTerminalReason::
        convergence_conflicting_observation:
    case AdaptiveV2ManagerCycleTerminalReason::invalid_terminal_identity:
    case AdaptiveV2ManagerCycleTerminalReason::successor_rotation_failed:
    case AdaptiveV2ManagerCycleTerminalReason::
        evidence_window_reset_failed:
    case AdaptiveV2ManagerCycleTerminalReason::caller_failed:
        break;
    case AdaptiveV2ManagerCycleTerminalReason::successor_converged:
    case AdaptiveV2ManagerCycleTerminalReason::explicit_no_op:
        return false;
    }

    auto &state = *state_;
    if (!state.owns_cycle())
        return false;
    const auto *winning = state.convergence == nullptr
        ? nullptr
        : state.convergence->winning_identity();
    if (!state.append_terminal(
            AdaptiveV2ManagerCycleOutcome::failed,
            reason,
            winning))
    {
        return false;
    }

    state.ingress.discard_prepared_window();
    state.release_cycle(State::Phase::unavailable);
    return true;
}

bool AdaptiveV2ManagerSession::finalize_convergence_failure_if_needed()
    noexcept
{
    const auto status = convergence_status();
    if (!status.has_value())
        return false;
    switch (*status)
    {
    case AdaptiveV2ManagerConvergenceStatus::retry_exhausted:
        return finalize_failed_cycle(
            AdaptiveV2ManagerCycleTerminalReason::
                convergence_retry_exhausted);
    case AdaptiveV2ManagerConvergenceStatus::conflicting_observation:
        return finalize_failed_cycle(
            AdaptiveV2ManagerCycleTerminalReason::
                convergence_conflicting_observation);
    case AdaptiveV2ManagerConvergenceStatus::awaiting_activations:
    case AdaptiveV2ManagerConvergenceStatus::ready_for_optimization:
        return false;
    }
    return false;
}

AdaptiveV2ManagerConvergenceDisposition
AdaptiveV2ManagerSession::classify_terminal_commit(
    ReplicaID authenticated_replica,
    const AdaptiveV2EpochChangeCommittedObservation &observation,
    bool &matched) const noexcept
{
    matched = true;
    const auto &members = state_->ingress.membership();
    if (!std::binary_search(
            members.begin(), members.end(), authenticated_replica))
    {
        return AdaptiveV2ManagerConvergenceDisposition::
            rejected_nonmember;
    }
    if (authenticated_replica != observation.claimed_source_replica_id)
    {
        return AdaptiveV2ManagerConvergenceDisposition::
            rejected_spoofed_source;
    }
    if (observation.schema_version ==
        kAdaptiveV2ConvergenceObservationSchemaVersionV1)
    {
        for (auto record = state_->records.rbegin();
             record != state_->records.rend();
             ++record)
        {
            if (record->winning_activation.has_value() &&
                *record->winning_activation == observation.identity)
            {
                return AdaptiveV2ManagerConvergenceDisposition::duplicate;
            }
        }
    }
    matched = false;
    return AdaptiveV2ManagerConvergenceDisposition::
        rejected_wrong_identity;
}

AdaptiveV2ManagerConvergenceDisposition
AdaptiveV2ManagerSession::classify_terminal_activation(
    ReplicaID authenticated_replica,
    const AdaptiveV2EpochActivatedObservation &observation,
    bool &matched) const noexcept
{
    matched = true;
    const auto &members = state_->ingress.membership();
    if (!std::binary_search(
            members.begin(), members.end(), authenticated_replica))
    {
        return AdaptiveV2ManagerConvergenceDisposition::
            rejected_nonmember;
    }
    if (authenticated_replica != observation.claimed_source_replica_id)
    {
        return AdaptiveV2ManagerConvergenceDisposition::
            rejected_spoofed_source;
    }
    if (observation.schema_version ==
        kAdaptiveV2ConvergenceObservationSchemaVersionV1)
    {
        for (auto record = state_->records.rbegin();
             record != state_->records.rend();
             ++record)
        {
            if (record->winning_activation.has_value() &&
                *record->winning_activation == observation.identity)
            {
                return observation.activated_epoch_number ==
                               observation.identity.successor_epoch_number &&
                               observation.activated_epoch_digest ==
                                   observation.identity.successor_epoch_digest
                    ? AdaptiveV2ManagerConvergenceDisposition::duplicate
                    : AdaptiveV2ManagerConvergenceDisposition::
                          rejected_wrong_identity;
            }
        }
    }
    matched = false;
    return AdaptiveV2ManagerConvergenceDisposition::
        rejected_wrong_identity;
}

const std::vector<AdaptiveV2ManagerSessionTerminalRecord> &
AdaptiveV2ManagerSession::terminal_records() const noexcept
{
    return state_->records;
}

void AdaptiveV2ManagerSession::shutdown() noexcept
{
    auto &state = *state_;
    if (state.owns_cycle())
    {
        if (!finalize_failed_cycle(
                AdaptiveV2ManagerCycleTerminalReason::caller_failed))
        {
            return;
        }
    }
    else if (state.phase != State::Phase::unavailable)
    {
        state.convergence.reset();
        state.controller.reset();
        state.current_policy.reset();
        state.commit_bindings.clear();
        state.phase = State::Phase::unavailable;
    }
    state.ingress.discard_prepared_window();
    state.ingress.shutdown();
}

} // namespace hotstuff
