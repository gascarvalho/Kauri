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
    std::uint64_t command_block_height,
    std::uint32_t active_tree_id) noexcept
{
    const auto successor = checked_successor_epoch(
        ingress.current_epoch().epoch_number());
    if (!successor.has_value())
        return false;

    const auto &definition = bundle.definition();
    const auto &payload = bundle.command().payload;
    std::uint64_t activation_height = 0;
    if (!checked_add(
            command_block_height,
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
           identity.command_block_height == command_block_height &&
           identity.activation_delay_blocks ==
               payload.activation_delay_blocks &&
           identity.activation_height == activation_height;
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
            ingress.current_epoch().epoch_number() >
                config.maximum_epoch_number ||
            ingress.activation_generation() >
                config.maximum_activation_generation ||
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
    }

    void make_unavailable() noexcept
    {
        convergence.reset();
        controller.reset();
        current_policy.reset();
        command_block_height.reset();
        phase = Phase::unavailable;
    }

    AdaptiveV2ManagerSessionConfig config;
    AdaptiveV2ManagerIngress ingress;
    std::unique_ptr<AdaptiveV2ManagerController> controller;
    std::unique_ptr<AdaptiveV2ManagerConvergence> convergence;
    std::optional<AdaptiveV2TransitionPolicy> current_policy;
    std::optional<std::uint64_t> command_block_height;
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

AdaptiveV2ManagerIngress &
AdaptiveV2ManagerSession::ingress() noexcept
{
    return state_->ingress;
}

const AdaptiveV2ManagerIngress &
AdaptiveV2ManagerSession::ingress() const noexcept
{
    return state_->ingress;
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
        state.command_block_height.has_value() ||
        !state.ingress.healthy() ||
        !successor.has_value() ||
        *successor > state.config.maximum_epoch_number ||
        !successor_generation.has_value() ||
        *successor_generation >
            state.config.maximum_activation_generation ||
        state.next_cycle_ordinal ==
            std::numeric_limits<std::uint64_t>::max() ||
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
        state.make_unavailable();
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
    std::uint64_t command_block_height) noexcept
{
    auto &state = *state_;
    const auto *bundle = successor_bundle();
    if (state.phase != State::Phase::successor_available ||
        state.controller == nullptr || state.convergence != nullptr ||
        !state.current_policy.has_value() || bundle == nullptr ||
        command_block_height == 0)
    {
        return false;
    }

    std::uint64_t activation_height = 0;
    std::uint64_t deadline = 0;
    if (!checked_add(
            command_block_height,
            bundle->command().payload.activation_delay_blocks,
            activation_height) ||
        !checked_add(
            command_block_height,
            state.config.convergence_window_ticks,
            deadline))
    {
        return false;
    }
    (void)activation_height;

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
                command_block_height);

        state.convergence = std::move(convergence);
        state.command_block_height = command_block_height;
        state.phase = State::Phase::observing_successor;
        return true;
    }
    catch (...)
    {
        return false;
    }
}

AdaptiveV2ManagerConvergenceDisposition
AdaptiveV2ManagerSession::observe_activation(
    ReplicaID authenticated_replica,
    const AdaptiveV2EpochActivatedObservation &observation) noexcept
{
    auto &state = *state_;
    if (state.phase != State::Phase::observing_successor ||
        state.convergence == nullptr)
    {
        return AdaptiveV2ManagerConvergenceDisposition::terminal;
    }
    return state.convergence->observe_activation(
        authenticated_replica, observation);
}

bool AdaptiveV2ManagerSession::consume_ready_and_rotate() noexcept
{
    auto &state = *state_;
    if (state.phase != State::Phase::observing_successor ||
        state.controller == nullptr || state.convergence == nullptr ||
        !state.current_policy.has_value() ||
        !state.command_block_height.has_value() ||
        state.convergence->status() !=
            AdaptiveV2ManagerConvergenceStatus::
                ready_for_optimization)
    {
        return false;
    }

    const auto *bundle = state.controller->successor_bundle();
    const auto *identity = state.convergence->winning_identity();
    if (bundle == nullptr || identity == nullptr ||
        state.records.capacity() <= state.records.size() ||
        !exact_terminal_identity(
            state.ingress,
            *bundle,
            *identity,
            *state.command_block_height,
            state.config.active_tree_id))
    {
        state.make_unavailable();
        return false;
    }

    AdaptiveV2ManagerSessionTerminalRecord record{
        state.next_cycle_ordinal,
        state.current_policy->intent,
        identity->predecessor_epoch_number,
        identity->predecessor_epoch_digest,
        identity->successor_epoch_number,
        identity->successor_epoch_digest,
        identity->command_payload_digest,
        *identity};

    if (!state.convergence->consume_ready_for_optimization())
    {
        state.make_unavailable();
        return false;
    }

    try
    {
        state.records.push_back(std::move(record));
    }
    catch (...)
    {
        state.make_unavailable();
        return false;
    }

    const auto rotated = state.ingress.rotate_to_successor(
        bundle->definition(), state.config.active_tree_id);
    if (rotated != AdaptiveV2ManagerIngressStatus::processed)
    {
        state.records.pop_back();
        state.make_unavailable();
        return false;
    }

    ++state.next_cycle_ordinal;
    state.convergence.reset();
    state.controller.reset();
    state.current_policy.reset();
    state.command_block_height.reset();
    state.phase = State::Phase::evidence_window_open;
    return true;
}

const std::vector<AdaptiveV2ManagerSessionTerminalRecord> &
AdaptiveV2ManagerSession::terminal_records() const noexcept
{
    return state_->records;
}

} // namespace hotstuff
