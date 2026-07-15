/**
 * Exact proposal-context gate for direct votes and aggregate relays.
 */

#ifndef HOTSTUFF_EXACT_VOTE_HANDLER_H_INCLUDED
#define HOTSTUFF_EXACT_VOTE_HANDLER_H_INCLUDED

#include <memory>
#include <optional>
#include <set>

#include "hotstuff/consensus.h"
#include "hotstuff/proposal_context.h"

namespace hotstuff
{

enum class ExactContributionKind
{
    direct_vote,
    aggregate_relay
};

struct ExactContributionEnvelope
{
    ProposalKey message_key;
    ProposalKey certificate_key;
    ReplicaID authenticated_sender{0};
    std::optional<ReplicaID> claimed_voter;
    std::set<ReplicaID> certified_signers;

    // The adapters retain immutable message ownership across asynchronous
    // verification and delivery. Synthetic gate tests may leave these empty.
    std::shared_ptr<const Vote> direct_vote;
    std::shared_ptr<const VoteRelay> aggregate_relay;
};

ExactContributionEnvelope make_exact_direct_envelope(
    const Vote &vote,
    ReplicaID authenticated_sender);

ExactContributionEnvelope make_exact_relay_envelope(
    const VoteRelay &relay,
    ReplicaID authenticated_sender);

uint256_t exact_contribution_fingerprint(
    ExactContributionKind kind,
    const ExactContributionEnvelope &contribution);

bool passes_exact_contribution_cheap_gate(
    ExactContributionKind kind,
    const ExactContributionEnvelope &contribution,
    const ProposalKey &key,
    const ProposalTreeSnapshot &tree) noexcept;

class ExactVoteHandlerEffects
{
public:
    virtual ~ExactVoteHandlerEffects() = default;

    virtual promise_t start_worker_verification(
        ExactContributionKind kind,
        const ExactContributionEnvelope &contribution) = 0;
    virtual promise_t start_block_delivery(
        const ProposalKey &key) = 0;
    virtual void continue_verified(
        const ProposalContextLease &lease,
        ExactContributionKind kind,
        const ExactContributionEnvelope &contribution) = 0;
};

class ExactVoteHandlerCoordinator final
{
public:
    ExactVoteHandlerCoordinator(
        ProposalContextLifecycle &contexts,
        ExactVoteHandlerEffects &effects) noexcept;
    ExactVoteHandlerCoordinator(
        std::shared_ptr<ProposalContextLifecycle> contexts,
        std::shared_ptr<ExactVoteHandlerEffects> effects);

    promise_t handle_direct(
        const ExactContributionEnvelope &contribution);
    promise_t handle_relay(
        const ExactContributionEnvelope &contribution);

private:
    promise_t handle(
        ExactContributionKind kind,
        const ExactContributionEnvelope &contribution);

    ProposalContextLifecycle &contexts_;
    ExactVoteHandlerEffects &effects_;
    std::shared_ptr<ProposalContextLifecycle> contexts_owner_;
    std::shared_ptr<ExactVoteHandlerEffects> effects_owner_;
};

} // namespace hotstuff

#endif
