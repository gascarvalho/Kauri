#include "hotstuff/epoch_runtime_wiring.h"

#include <algorithm>
#include <exception>
#include <limits>
#include <set>
#include <stdexcept>
#include <utility>
#include <vector>

#include "hotstuff/hotstuff.h"
#include "hotstuff/epoch_live_binding.h"
#include "hotstuff/proposal_body.h"

namespace hotstuff
{
namespace
{

struct PreparedPlan
{
    std::uint64_t token{0};
    uint256_t digest;
    std::vector<ConfigurationId> configurations;
    PreparedEpochLiveRuntime live;
};

bool contains_configuration(
    const PreparedPlan &plan,
    const ConfigurationId &configuration) noexcept
{
    return std::find(
               plan.configurations.begin(),
               plan.configurations.end(),
               configuration) != plan.configurations.end();
}

class StatelessEpochLiveEffects final : public EpochLiveEffects
{
public:
    std::optional<PreparedEpochLiveRuntime> prepare(
        const EpochRuntimePlan &plan) override
    {
        if (next_token_ == 0)
            throw std::overflow_error("stateless live token exhausted");
        return PreparedEpochLiveRuntime{
            next_token_++, plan.canonical_digest};
    }

    void discard(PreparedEpochLiveRuntime) noexcept override {}
    void arm(
        PreparedEpochLiveRuntime,
        const EpochRuntimeUpdate &) noexcept override {}
    void arm_rotation(const EpochRuntimeRotation &) noexcept override {}
    void apply_update(const EpochRuntimeUpdate &) noexcept override {}

private:
    std::uint64_t next_token_{1};
};

} // namespace

struct HotStuffEpochRuntimeTransaction::State
{
    State(
        ProposalAdmissionCoordinator &admission_value,
        ProposalContextLifecycle &contexts_value,
        EpochLiveEffects &live_state_value)
        : admission(admission_value),
          contexts(contexts_value),
          live_state(live_state_value)
    {}

    ProposalAdmissionCoordinator &admission;
    ProposalContextLifecycle &contexts;
    EpochLiveEffects &live_state;
    std::vector<PreparedPlan> prepared;
    std::uint64_t next_token{1};
};

HotStuffEpochRuntimeTransaction::HotStuffEpochRuntimeTransaction(
    ProposalAdmissionCoordinator &admission,
    ProposalContextLifecycle &contexts)
    : owned_live_state_(new StatelessEpochLiveEffects()),
      state_(new State(admission, contexts, *owned_live_state_))
{}

HotStuffEpochRuntimeTransaction::HotStuffEpochRuntimeTransaction(
    ProposalAdmissionCoordinator &admission,
    ProposalContextLifecycle &contexts,
    EpochLiveEffects &live_state)
    : state_(new State(admission, contexts, live_state))
{}

HotStuffEpochRuntimeTransaction::~HotStuffEpochRuntimeTransaction() = default;

std::optional<PreparedEpochRuntime>
HotStuffEpochRuntimeTransaction::prepare(const EpochRuntimePlan &plan)
{
    if (plan.trees.empty() || plan.canonical_stage.empty() ||
        (plan.protocol_mode != EpochProtocolMode::adaptive_v1 &&
         plan.protocol_mode != EpochProtocolMode::adaptive_v2) ||
        plan.epoch_digest == uint256_t{} ||
        plan.canonical_digest == uint256_t{} ||
        DataStream(plan.canonical_stage).get_hash() != plan.canonical_digest)
        return std::nullopt;
    if (plan.protocol_mode == EpochProtocolMode::adaptive_v1 &&
        (plan.stage.protocol_mode != EpochProtocolMode::adaptive_v1 ||
         plan.stage.activation.successor_epoch_number != plan.epoch_number ||
         plan.stage.activation.successor_epoch_digest != plan.epoch_digest))
        return std::nullopt;
    if (plan.protocol_mode == EpochProtocolMode::adaptive_v2 &&
        plan.canonical_digest != plan.epoch_digest)
        return std::nullopt;

    PreparedPlan candidate;
    if (state_->next_token == 0)
        throw std::overflow_error("epoch runtime token exhausted");
    candidate.token = state_->next_token++;
    candidate.digest = plan.canonical_digest;
    candidate.configurations.reserve(plan.trees.size());
    for (const auto &tree : plan.trees)
    {
        if (tree.configuration.epoch_number !=
                plan.epoch_number ||
            tree.configuration.epoch_digest !=
                plan.epoch_digest ||
            tree.configuration.tree_id != tree.tree.tree_id ||
            tree.activation_generation == 0 ||
            tree.tree.members_breadth_first.empty() ||
            tree.leader != tree.tree.members_breadth_first.front())
            return std::nullopt;
        candidate.configurations.push_back(tree.configuration);
    }

    auto live = state_->live_state.prepare(plan);
    if (!live.has_value())
        return std::nullopt;
    candidate.live = *live;
    try
    {
        state_->prepared.push_back(std::move(candidate));
    }
    catch (...)
    {
        state_->live_state.discard(std::move(*live));
        throw;
    }
    const auto &stored = state_->prepared.back();
    return PreparedEpochRuntime{stored.token, stored.digest};
}

void HotStuffEpochRuntimeTransaction::discard(
    PreparedEpochRuntime prepared) noexcept
{
    const auto found = std::find_if(
        state_->prepared.begin(),
        state_->prepared.end(),
        [&prepared](const PreparedPlan &candidate) {
            return candidate.token == prepared.token &&
                   candidate.digest == prepared.canonical_plan_digest;
        });
    if (found != state_->prepared.end())
    {
        state_->live_state.discard(found->live);
        state_->prepared.erase(found);
    }
}

void HotStuffEpochRuntimeTransaction::commit(
    PreparedEpochRuntime prepared,
    const EpochRuntimeUpdate &update) noexcept
{
    const auto found = std::find_if(
        state_->prepared.begin(),
        state_->prepared.end(),
        [&prepared](const PreparedPlan &candidate) {
            return candidate.token == prepared.token &&
                   candidate.digest == prepared.canonical_plan_digest;
        });
    if (found == state_->prepared.end() ||
        !contains_configuration(*found, update.activation.configuration))
        std::terminate();

    try
    {
        state_->contexts.activate_configuration(
            update.activation.configuration);
    }
    catch (...)
    {
        std::terminate();
    }
    if (!state_->admission.activate_without_draining(
            update.activation.configuration))
        std::terminate();
    state_->live_state.arm(found->live, update);
    state_->prepared.erase(found);
}

void HotStuffEpochRuntimeTransaction::rotate(
    const EpochRuntimeRotation &update) noexcept
{
    if (!state_->admission.activate_without_draining(
            update.activation.configuration))
        return;
    state_->contexts.activate_configuration(update.activation.configuration);
    state_->live_state.arm_rotation(update);
}

struct HotStuffRetryableFutureProposalStore::State
{
    struct Reservation
    {
        std::uint64_t token{0};
        ProposalKey key;
    };

    State(
        FutureProposalBuffer &buffer_value,
        ProposalAdmissionCoordinator &admission_value)
        : buffer(buffer_value), admission(admission_value)
    {}

    FutureProposalBuffer &buffer;
    ProposalAdmissionCoordinator &admission;
    std::set<ProposalKey> claimed;
    std::vector<Reservation> reservations;
    std::uint64_t next_token{1};
};

HotStuffRetryableFutureProposalStore::HotStuffRetryableFutureProposalStore(
    FutureProposalBuffer &buffer,
    ProposalAdmissionCoordinator &admission)
    : state_(new State(buffer, admission))
{}

HotStuffRetryableFutureProposalStore::~HotStuffRetryableFutureProposalStore() =
    default;

bool HotStuffRetryableFutureProposalStore::insert(BufferedProposal proposal)
{
    const auto key = proposal.metadata.key();
    if (state_->buffer.contains(key))
        return true;
    return state_->buffer.insert(std::move(proposal));
}

std::optional<FutureProposalClaim>
HotStuffRetryableFutureProposalStore::claim_next(
    const ConfigurationId &configuration)
{
    const auto *const selected = state_->buffer.first_unclaimed(
        configuration, state_->claimed);
    if (selected == nullptr)
        return std::nullopt;

    BufferedProposal proposal = *selected;
    const auto token = state_->next_token;
    if (token == 0)
        throw std::overflow_error("future proposal token exhausted");
    const auto key = proposal.metadata.key();
    const auto claimed = state_->claimed.insert(key);
    if (!claimed.second)
        throw std::logic_error("future proposal is already claimed");
    try
    {
        state_->reservations.push_back(State::Reservation{token, key});
    }
    catch (...)
    {
        state_->claimed.erase(claimed.first);
        throw;
    }
    ++state_->next_token;
    return FutureProposalClaim{token, std::move(proposal)};
}

void HotStuffRetryableFutureProposalStore::process_active(
    const FutureProposalClaim &claim)
{
    const auto found = std::find_if(
        state_->reservations.begin(),
        state_->reservations.end(),
        [&claim](const State::Reservation &reservation) {
            return reservation.token == claim.token &&
                   reservation.key == claim.proposal.metadata.key();
        });
    if (found == state_->reservations.end() ||
        !state_->admission.process_claimed_active(claim.proposal))
        throw std::logic_error("future proposal is not active");
}

void HotStuffRetryableFutureProposalStore::acknowledge(
    std::uint64_t token) noexcept
{
    const auto found = std::find_if(
        state_->reservations.begin(),
        state_->reservations.end(),
        [token](const State::Reservation &reservation) {
            return reservation.token == token;
        });
    if (found == state_->reservations.end())
        return;
    state_->buffer.erase(found->key);
    state_->claimed.erase(found->key);
    state_->reservations.erase(found);
}

void HotStuffRetryableFutureProposalStore::release(
    std::uint64_t token) noexcept
{
    const auto found = std::find_if(
        state_->reservations.begin(),
        state_->reservations.end(),
        [token](const State::Reservation &reservation) {
            return reservation.token == token;
        });
    if (found == state_->reservations.end())
        return;
    state_->claimed.erase(found->key);
    state_->reservations.erase(found);
}

void HotStuffRetryableFutureProposalStore::complete(
    const ConfigurationId &) noexcept
{}

std::size_t HotStuffRetryableFutureProposalStore::size() const noexcept
{
    return state_->buffer.size();
}

AuthenticatedEpochPeer authenticated_epoch_replica(
    ReplicaID replica_id,
    const PeerId &source_peer) noexcept
{
    return {EpochPeerRole::replica, replica_id, source_peer};
}

const bytearray_t &hotstuff_epoch_processing_payload(
    const BufferedProposal &proposal) noexcept
{
    return proposal.processing_payload;
}

struct HotStuffEpochConsensusBodyValidator::State
{
    explicit State(
        HotStuffCore &decoder_value,
        const EpochStore *epochs_value = nullptr)
        : decoder(decoder_value), epochs(epochs_value)
    {}

    bool authenticated_source(
        const AuthenticatedEpochPeer &peer) const noexcept
    {
        if (peer.role != EpochPeerRole::replica ||
            !peer.replica_id.has_value() || peer.source_peer.is_null())
            return false;
        try
        {
            return decoder.get_config().get_peer_id(*peer.replica_id) ==
                   peer.source_peer;
        }
        catch (...)
        {
            return false;
        }
    }

    bool authenticated_proposal_source(
        const EpochConsensusEnvelope &envelope,
        const AuthenticatedEpochPeer &peer) const noexcept
    {
        if (!authenticated_source(peer) ||
            envelope.originator != envelope.proposer)
            return false;

        if (epochs == nullptr)
        {
            return envelope.originator == *peer.replica_id;
        }

        const auto *const epoch = epochs->find_epoch(
            envelope.configuration.epoch_number);
        const auto *const tree = epochs->find_tree(
            envelope.configuration.epoch_number,
            envelope.configuration.tree_id);
        if (epoch == nullptr || tree == nullptr ||
            epoch->epoch_digest() != envelope.configuration.epoch_digest ||
            tree->fanout == 0 || tree->members_breadth_first.empty() ||
            tree->members_breadth_first.front() != envelope.proposer)
            return false;

        const auto local = std::find(
            tree->members_breadth_first.begin(),
            tree->members_breadth_first.end(),
            decoder.get_id());
        if (local == tree->members_breadth_first.end())
            return false;

        const auto local_position = static_cast<std::size_t>(
            std::distance(tree->members_breadth_first.begin(), local));
        const auto expected_source = local_position == 0
            ? tree->members_breadth_first.front()
            : tree->members_breadth_first[
                  (local_position - 1) / tree->fanout];
        // Hierarchical dissemination remains primary. The exact proposer/root
        // is additionally permitted to retransmit the already-admitted
        // proposal directly after the bounded recovery deadline.
        return *peer.replica_id == expected_source ||
               *peer.replica_id ==
                   tree->members_breadth_first.front();
    }

    HotStuffCore &decoder;
    const EpochStore *epochs;
};

HotStuffEpochConsensusBodyValidator::HotStuffEpochConsensusBodyValidator(
    HotStuffCore &structural_decoder)
    : state_(new State(structural_decoder))
{}

HotStuffEpochConsensusBodyValidator::HotStuffEpochConsensusBodyValidator(
    HotStuffCore &structural_decoder,
    const EpochStore &epochs)
    : state_(new State(structural_decoder, &epochs))
{}

HotStuffEpochConsensusBodyValidator::~HotStuffEpochConsensusBodyValidator() =
    default;

bool HotStuffEpochConsensusBodyValidator::validate_proposal(
    const EpochConsensusEnvelope &envelope,
    const AuthenticatedEpochPeer &peer)
{
    if (!state_->authenticated_proposal_source(envelope, peer))
        return false;

    const auto metadata = decode_detached_proposal_body(
        envelope.body, state_->decoder);
    return metadata &&
           metadata->configuration == envelope.configuration &&
           metadata->block_hash == envelope.block_hash &&
           metadata->proposer == envelope.proposer;
}

bool HotStuffEpochConsensusBodyValidator::validate_vote(
    const EpochConsensusEnvelope &envelope,
    const AuthenticatedEpochPeer &peer)
{
    if (!state_->authenticated_source(peer) ||
        envelope.originator != *peer.replica_id)
        return false;
    try
    {
        MsgVote message(DataStream(envelope.body));
        if (!message.postponed_parse(&state_->decoder))
            return false;
        return message.vote.voter == envelope.originator &&
               message.vote.key() == envelope.key() &&
               validate_authenticated_vote(
                   state_->decoder.get_config(),
                   peer.source_peer,
                   message.vote);
    }
    catch (...)
    {
        return false;
    }
}

bool HotStuffEpochConsensusBodyValidator::validate_relay(
    const EpochConsensusEnvelope &envelope,
    const AuthenticatedEpochPeer &peer)
{
    if (!state_->authenticated_source(peer) ||
        envelope.originator != *peer.replica_id)
        return false;
    try
    {
        MsgRelay message(DataStream(envelope.body));
        if (!message.postponed_parse(&state_->decoder))
            return false;
        return message.vote.key() == envelope.key() &&
               validate_relay_envelope(
                   state_->decoder.get_config(), message.vote);
    }
    catch (...)
    {
        return false;
    }
}

void HotStuffEpochHandlerInstaller::install(
    HotStuffEpochHandlerRegistry &registry,
    HotStuffEpochRuntimeAdapter &adapter)
{
    registry.register_stage_handler(
        MsgStageEpochDefinition::opcode,
        [&adapter](
            MsgStageEpochDefinition &&message,
            const AuthenticatedEpochPeer &peer,
            const EpochValidationContext &context) {
            return adapter.handle_stage(
                std::move(message), peer, context);
        });
    registry.register_arm_handler(
        MsgArmActivation::opcode,
        [&adapter](
            MsgArmActivation &&message,
            const AuthenticatedEpochPeer &peer) {
            return adapter.handle_arm(std::move(message), peer);
        });
    registry.register_proposal_handler(
        MsgPropose::opcode,
        [&adapter](
            MsgPropose &&message,
            const AuthenticatedEpochPeer &peer) {
            return adapter.handle_proposal(std::move(message), peer);
        });
    registry.register_vote_handler(
        MsgVote::opcode,
        [&adapter](
            MsgVote &&message,
            const AuthenticatedEpochPeer &peer) {
            return adapter.handle_vote(std::move(message), peer);
        });
    registry.register_relay_handler(
        MsgRelay::opcode,
        [&adapter](
            MsgRelay &&message,
            const AuthenticatedEpochPeer &peer) {
            return adapter.handle_relay(std::move(message), peer);
        });
}

} // namespace hotstuff
