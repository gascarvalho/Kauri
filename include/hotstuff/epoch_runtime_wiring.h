/**
 * Concrete HotStuff adapters for the adaptive epoch runtime.
 */

#ifndef HOTSTUFF_EPOCH_RUNTIME_WIRING_H_INCLUDED
#define HOTSTUFF_EPOCH_RUNTIME_WIRING_H_INCLUDED

#include <functional>
#include <memory>

#include "hotstuff/epoch_runtime.h"

namespace hotstuff
{

class HotStuffCore;
class EpochLiveEffects;

class HotStuffEpochRuntimeTransaction final : public EpochRuntimeTransaction
{
public:
    HotStuffEpochRuntimeTransaction(
        ProposalAdmissionCoordinator &admission,
        ProposalContextLifecycle &contexts);
    HotStuffEpochRuntimeTransaction(
        ProposalAdmissionCoordinator &admission,
        ProposalContextLifecycle &contexts,
        EpochLiveEffects &live_state);
    ~HotStuffEpochRuntimeTransaction() override;

    std::optional<PreparedEpochRuntime> prepare(
        const EpochRuntimePlan &plan) override;
    void discard(PreparedEpochRuntime prepared) noexcept override;
    void commit(
        PreparedEpochRuntime prepared,
        const EpochRuntimeUpdate &update) noexcept override;
    void rotate(const EpochRuntimeRotation &update) noexcept override;

private:
    struct State;
    std::unique_ptr<EpochLiveEffects> owned_live_state_;
    std::unique_ptr<State> state_;
};

class HotStuffRetryableFutureProposalStore final
    : public RetryableFutureProposalStore
{
public:
    HotStuffRetryableFutureProposalStore(
        FutureProposalBuffer &buffer,
        ProposalAdmissionCoordinator &admission);
    ~HotStuffRetryableFutureProposalStore() override;

    bool insert(BufferedProposal proposal) override;
    std::optional<FutureProposalClaim> claim_next(
        const ConfigurationId &configuration) override;
    void process_active(const FutureProposalClaim &claim) override;
    bool process_active(
        const FutureProposalClaim &claim,
        ProposalProcessingCompletion completion) override;
    void acknowledge(std::uint64_t token) noexcept override;
    void release(std::uint64_t token) noexcept override;
    void complete(
        const ConfigurationId &configuration) noexcept override;
    std::size_t size() const noexcept override;

private:
    struct State;
    std::unique_ptr<State> state_;
};

class HotStuffEpochConsensusBodyValidator final
    : public EpochConsensusBodyValidator
{
public:
    explicit HotStuffEpochConsensusBodyValidator(
        HotStuffCore &structural_decoder);
    HotStuffEpochConsensusBodyValidator(
        HotStuffCore &structural_decoder,
        const EpochStore &epochs);
    ~HotStuffEpochConsensusBodyValidator() override;

    bool validate_proposal(
        const EpochConsensusEnvelope &envelope,
        const AuthenticatedEpochPeer &peer) override;
    bool validate_vote(
        const EpochConsensusEnvelope &envelope,
        const AuthenticatedEpochPeer &peer) override;
    bool validate_relay(
        const EpochConsensusEnvelope &envelope,
        const AuthenticatedEpochPeer &peer) override;

private:
    struct State;
    std::unique_ptr<State> state_;
};

class HotStuffEpochHandlerRegistry
{
public:
    using StageHandler = std::function<ReplicaStageIngressResult(
        MsgStageEpochDefinition &&,
        const AuthenticatedEpochPeer &,
        const EpochValidationContext &)>;
    using ArmHandler = std::function<ReplicaArmIngressResult(
        MsgArmActivation &&,
        const AuthenticatedEpochPeer &)>;
    using ProposalHandler = std::function<EpochConsensusIngressResult(
        MsgPropose &&,
        const AuthenticatedEpochPeer &)>;
    using VoteHandler = std::function<EpochConsensusIngressResult(
        MsgVote &&,
        const AuthenticatedEpochPeer &)>;
    using RelayHandler = std::function<EpochConsensusIngressResult(
        MsgRelay &&,
        const AuthenticatedEpochPeer &)>;

    virtual ~HotStuffEpochHandlerRegistry() = default;

    virtual void register_stage_handler(
        opcode_t opcode, StageHandler handler) = 0;
    virtual void register_arm_handler(
        opcode_t opcode, ArmHandler handler) = 0;
    virtual void register_proposal_handler(
        opcode_t opcode, ProposalHandler handler) = 0;
    virtual void register_vote_handler(
        opcode_t opcode, VoteHandler handler) = 0;
    virtual void register_relay_handler(
        opcode_t opcode, RelayHandler handler) = 0;
};

class HotStuffEpochHandlerInstaller final
{
public:
    static void install(
        HotStuffEpochHandlerRegistry &registry,
        HotStuffEpochRuntimeAdapter &adapter);
};

AuthenticatedEpochPeer authenticated_epoch_replica(
    ReplicaID replica_id,
    const PeerId &source_peer) noexcept;

const bytearray_t &hotstuff_epoch_processing_payload(
    const BufferedProposal &proposal) noexcept;

} // namespace hotstuff

#endif
