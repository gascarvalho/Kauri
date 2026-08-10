#include "hotstuff/epoch_live_binding.h"

#include <algorithm>
#include <exception>
#include <iterator>
#include <utility>

namespace hotstuff
{

struct HotStuffEpochLiveState::State
{
    explicit State(HotStuffEpochLiveStateCallbacks callbacks_value)
        : callbacks(std::move(callbacks_value))
    {}

    HotStuffEpochLiveStateCallbacks callbacks;
};

HotStuffEpochLiveState::HotStuffEpochLiveState(
    HotStuffEpochLiveStateCallbacks callbacks)
    : state_(new State(std::move(callbacks)))
{}

HotStuffEpochLiveState::~HotStuffEpochLiveState() = default;

std::optional<PreparedEpochLiveRuntime>
HotStuffEpochLiveState::prepare(const EpochRuntimePlan &plan)
{
    if (!state_->callbacks.prepare)
        return std::nullopt;
    return state_->callbacks.prepare(plan);
}

void HotStuffEpochLiveState::discard(
    PreparedEpochLiveRuntime prepared) noexcept
{
    if (!state_->callbacks.discard)
        return;
    try
    {
        state_->callbacks.discard(std::move(prepared));
    }
    catch (...)
    {
        std::terminate();
    }
}

void HotStuffEpochLiveState::arm(
    PreparedEpochLiveRuntime prepared,
    const EpochRuntimeUpdate &update) noexcept
{
    if (!state_->callbacks.arm)
        std::terminate();
    try
    {
        state_->callbacks.arm(std::move(prepared), update);
    }
    catch (...)
    {
        std::terminate();
    }
}

void HotStuffEpochLiveState::arm_rotation(
    const EpochRuntimeRotation &update) noexcept
{
    if (!state_->callbacks.arm_rotation)
        std::terminate();
    try
    {
        state_->callbacks.arm_rotation(update);
    }
    catch (...)
    {
        std::terminate();
    }
}

void HotStuffEpochLiveState::apply_update(
    const EpochRuntimeUpdate &update) noexcept
{
    if (!state_->callbacks.apply_update)
        std::terminate();
    try
    {
        state_->callbacks.apply_update(update);
    }
    catch (...)
    {
        std::terminate();
    }
}

HotStuffEpochLiveBinding::HotStuffEpochLiveBinding(
    HotStuffEpochRuntimeAdapter &adapter,
    ReplicaEpochActivation &activation,
    EpochLiveEffects &live_effects,
    EpochManagerEgress &manager_egress,
    EpochContributionContinuations &continuations) noexcept
    : adapter_(adapter),
      activation_(activation),
      live_effects_(live_effects),
      manager_egress_(manager_egress),
      continuations_(continuations)
{}

ReplicaStageIngressResult HotStuffEpochLiveBinding::handle_stage(
    MsgStageEpochDefinition &&message,
    const AuthenticatedEpochPeer &peer,
    const EpochValidationContext &validation_context)
{
    auto result = adapter_.handle_stage(
        std::move(message), peer, validation_context);
    if (result.error == EpochIngressError::none &&
        result.acknowledgement.has_value())
        manager_egress_.send_stage_ack(*result.acknowledgement);
    return result;
}

ReplicaArmIngressResult HotStuffEpochLiveBinding::handle_arm(
    MsgArmActivation &&message,
    const AuthenticatedEpochPeer &peer)
{
    return adapter_.handle_arm(std::move(message), peer);
}

EpochCommitIngressResult HotStuffEpochLiveBinding::finish_commit(
    EpochCommitIngressResult result) noexcept
{
    if (result.transition != ActivationTransition::activated ||
        result.error != EpochIngressError::none ||
        !result.update.has_value())
        return result;

    live_effects_.apply_update(*result.update);
    const auto status = activation_.active_status();
    if (status.has_value())
        manager_egress_.send_activation_status(*status);
    return result;
}

EpochCommitIngressResult HotStuffEpochLiveBinding::on_predecessor_commit(
    std::uint64_t height,
    const uint256_t &predecessor_digest) noexcept
{
    return finish_commit(
        adapter_.on_predecessor_commit(height, predecessor_digest));
}

EpochCommitIngressResult
HotStuffEpochLiveBinding::on_v2_post_block_commit(
    std::uint64_t height,
    const uint256_t &predecessor_digest) noexcept
{
    return finish_commit(
        adapter_.on_v2_post_block_commit(height, predecessor_digest));
}

EpochCommitIngressResult HotStuffEpochLiveBinding::replay_blocked_commit()
    noexcept
{
    return finish_commit(adapter_.replay_blocked_commit());
}

std::optional<EpochActivationEffect>
HotStuffEpochLiveBinding::active_view() const noexcept
{
    try
    {
        return activation_.active_effect();
    }
    catch (...)
    {
        return std::nullopt;
    }
}

std::optional<std::uint32_t>
HotStuffEpochLiveBinding::next_tree_id() const noexcept
{
    const auto active = active_view();
    if (!active.has_value() || active->definition == nullptr ||
        active->definition->trees().size() < 2)
        return std::nullopt;
    try
    {
        const auto &trees = active->definition->trees();
        const auto current = std::find_if(
            trees.begin(),
            trees.end(),
            [&active](const EpochTreeDefinition &tree) {
                return tree.tree_id == active->configuration.tree_id;
            });
        if (current == trees.end())
            return std::nullopt;
        const auto next = std::next(current) == trees.end()
                              ? trees.begin()
                              : std::next(current);
        return next->tree_id;
    }
    catch (...)
    {
        return std::nullopt;
    }
}

EpochRotationResult HotStuffEpochLiveBinding::rotate_to_tree(
    std::uint32_t tree_id) noexcept
{
    auto result = adapter_.rotate_to_tree(tree_id);
    if (result.error == EpochIngressError::none && result.update.has_value())
    {
        live_effects_.apply_update(*result.update);
        // Rotation is already published.  Buffered proposal replay remains
        // retryable and cannot retroactively report the rotation as failed.
        static_cast<void>(adapter_.drain_activated_futures());
    }
    return result;
}

EpochConsensusIngressResult HotStuffEpochLiveBinding::handle_proposal(
    MsgPropose &&message,
    const AuthenticatedEpochPeer &peer) noexcept
{
    return adapter_.handle_proposal(std::move(message), peer);
}

EpochConsensusIngressResult HotStuffEpochLiveBinding::handle_vote(
    MsgVote &&message,
    const AuthenticatedEpochPeer &peer) noexcept
{
    auto result = adapter_.handle_vote(std::move(message), peer);
    if (result.permission == EpochConsensusPermission::accept_contribution &&
        result.decoded_envelope.has_value())
        continuations_.continue_vote(result.decoded_envelope->body, peer);
    return result;
}

EpochConsensusIngressResult HotStuffEpochLiveBinding::handle_relay(
    MsgRelay &&message,
    const AuthenticatedEpochPeer &peer) noexcept
{
    auto result = adapter_.handle_relay(std::move(message), peer);
    if (result.permission == EpochConsensusPermission::accept_contribution &&
        result.decoded_envelope.has_value())
        continuations_.continue_relay(result.decoded_envelope->body, peer);
    return result;
}

bytearray_t adaptive_epoch_consensus_message(
    const ConfigurationId &configuration,
    std::uint64_t generation,
    EpochConsensusWireKind kind,
    const ProposalKey &key,
    ReplicaID originator,
    ReplicaID proposer,
    const bytearray_t &body,
    const EpochWireLimits &limits,
    EpochProtocolMode protocol_mode)
{
    if (key.configuration != configuration || generation == 0)
        return {};

    return encode_epoch_consensus_envelope(
        EpochConsensusEnvelope{
            configuration,
            generation,
            key.block_hash,
            originator,
            proposer,
            body,
            kEpochConsensusWireSchemaVersion,
            protocol_mode,
            kind},
        limits);
}

bytearray_t adaptive_epoch_consensus_message(
    const EpochActivationEffect &active,
    EpochConsensusWireKind kind,
    const ProposalKey &key,
    ReplicaID originator,
    ReplicaID proposer,
    const bytearray_t &body,
    const EpochWireLimits &limits)
{
    return adaptive_epoch_consensus_message(
        active.configuration,
        active.generation,
        kind,
        key,
        originator,
        proposer,
        body,
        limits);
}

} // namespace hotstuff
