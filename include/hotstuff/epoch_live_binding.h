/**
 * Process-owned binding between the adaptive epoch state machine and the
 * live HotStuff runtime.
 */

#ifndef HOTSTUFF_EPOCH_LIVE_BINDING_H_INCLUDED
#define HOTSTUFF_EPOCH_LIVE_BINDING_H_INCLUDED

#include <functional>
#include <memory>
#include <optional>

#include "hotstuff/epoch_runtime.h"

namespace hotstuff
{

struct PreparedEpochLiveRuntime
{
    std::uint64_t token{0};
    uint256_t canonical_plan_digest;
};

class EpochLiveEffects
{
public:
    virtual ~EpochLiveEffects() = default;

    virtual std::optional<PreparedEpochLiveRuntime> prepare(
        const EpochRuntimePlan &plan) = 0;
    virtual void discard(PreparedEpochLiveRuntime prepared) noexcept = 0;
    virtual void arm(
        PreparedEpochLiveRuntime prepared,
        const EpochRuntimeUpdate &update) noexcept = 0;
    virtual void arm_rotation(
        const EpochRuntimeRotation &update) noexcept = 0;
    virtual void apply_update(
        const EpochRuntimeUpdate &update) noexcept = 0;
};

class EpochManagerEgress
{
public:
    virtual ~EpochManagerEgress() = default;

    virtual void send_stage_ack(
        const StageAck &acknowledgement) noexcept = 0;
    virtual void send_activation_status(
        const ActivationStatus &status) noexcept = 0;
};

class EpochContributionContinuations
{
public:
    virtual ~EpochContributionContinuations() = default;

    virtual void continue_vote(
        const bytearray_t &body,
        const AuthenticatedEpochPeer &peer) noexcept = 0;
    virtual void continue_relay(
        const bytearray_t &body,
        const AuthenticatedEpochPeer &peer) noexcept = 0;
};

/**
 * Narrow callback-backed live-state owner.  The callbacks are installed once
 * by HotStuffBase.  All fallible topology construction happens in prepare;
 * apply_update only publishes the state selected by arm/arm_rotation.
 */
struct HotStuffEpochLiveStateCallbacks
{
    std::function<std::optional<PreparedEpochLiveRuntime>(
        const EpochRuntimePlan &)> prepare;
    std::function<void(PreparedEpochLiveRuntime)> discard;
    std::function<void(
        PreparedEpochLiveRuntime,
        const EpochRuntimeUpdate &)> arm;
    std::function<void(const EpochRuntimeRotation &)> arm_rotation;
    std::function<void(const EpochRuntimeUpdate &)> apply_update;
};

class HotStuffEpochLiveState final : public EpochLiveEffects
{
public:
    explicit HotStuffEpochLiveState(
        HotStuffEpochLiveStateCallbacks callbacks);
    ~HotStuffEpochLiveState() override;

    std::optional<PreparedEpochLiveRuntime> prepare(
        const EpochRuntimePlan &plan) override;
    void discard(PreparedEpochLiveRuntime prepared) noexcept override;
    void arm(
        PreparedEpochLiveRuntime prepared,
        const EpochRuntimeUpdate &update) noexcept override;
    void arm_rotation(
        const EpochRuntimeRotation &update) noexcept override;
    void apply_update(
        const EpochRuntimeUpdate &update) noexcept override;

private:
    struct State;
    std::unique_ptr<State> state_;
};

class HotStuffEpochLiveBinding final : public AdaptiveV2RotationEffects
{
public:
    HotStuffEpochLiveBinding(
        HotStuffEpochRuntimeAdapter &adapter,
        ReplicaEpochActivation &activation,
        EpochLiveEffects &live_effects,
        EpochManagerEgress &manager_egress,
        EpochContributionContinuations &continuations) noexcept;

    ReplicaStageIngressResult handle_stage(
        MsgStageEpochDefinition &&message,
        const AuthenticatedEpochPeer &peer,
        const EpochValidationContext &validation_context);
    ReplicaArmIngressResult handle_arm(
        MsgArmActivation &&message,
        const AuthenticatedEpochPeer &peer);
    EpochCommitIngressResult on_predecessor_commit(
        std::uint64_t height,
        const uint256_t &predecessor_digest) noexcept;
    EpochCommitIngressResult on_v2_post_block_commit(
        std::uint64_t height,
        const uint256_t &predecessor_digest) noexcept;
    AdaptiveV3CommitIngressResult on_v3_post_block_commit(
        AdaptiveV3CertifiedActivationGate &gate,
        std::uint64_t height,
        const ConfigurationId &predecessor_configuration,
        std::uint64_t predecessor_generation,
        const uint256_t &block_hash,
        std::uint64_t source_sequence,
        std::uint64_t monotonic_raw_ns) noexcept;
    AdaptiveV3CertificateIngressResult apply_v3_readiness_certificate(
        AdaptiveV3CertifiedActivationGate &gate,
        const AdaptiveV3ActivationReadinessCertificate &certificate)
        noexcept;
    EpochCommitIngressResult replay_blocked_commit() noexcept;
    std::optional<EpochActivationEffect> active_view()
        const noexcept override;
    std::optional<std::uint32_t> next_tree_id() const noexcept override;
    EpochRotationResult rotate_to_tree(
        std::uint32_t tree_id) noexcept override;
    EpochConsensusIngressResult handle_proposal(
        MsgPropose &&message,
        const AuthenticatedEpochPeer &peer) noexcept;
    EpochConsensusIngressResult handle_vote(
        MsgVote &&message,
        const AuthenticatedEpochPeer &peer) noexcept;
    EpochConsensusIngressResult handle_relay(
        MsgRelay &&message,
        const AuthenticatedEpochPeer &peer) noexcept;

private:
    EpochCommitIngressResult finish_commit(
        EpochCommitIngressResult result) noexcept;

    HotStuffEpochRuntimeAdapter &adapter_;
    ReplicaEpochActivation &activation_;
    EpochLiveEffects &live_effects_;
    EpochManagerEgress &manager_egress_;
    EpochContributionContinuations &continuations_;
};

bytearray_t adaptive_epoch_consensus_message(
    const ConfigurationId &configuration,
    std::uint64_t generation,
    EpochConsensusWireKind kind,
    const ProposalKey &key,
    ReplicaID originator,
    ReplicaID proposer,
    const bytearray_t &body,
    const EpochWireLimits &limits,
    EpochProtocolMode protocol_mode = EpochProtocolMode::adaptive_v1);

bytearray_t adaptive_epoch_consensus_message(
    const EpochActivationEffect &active,
    EpochConsensusWireKind kind,
    const ProposalKey &key,
    ReplicaID originator,
    ReplicaID proposer,
    const bytearray_t &body,
    const EpochWireLimits &limits);

} // namespace hotstuff

#endif
