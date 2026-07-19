#include "hotstuff/exact_vote_handler.h"

#include "hotstuff/vote_identity.h"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <exception>
#include <functional>
#include <stdexcept>
#include <type_traits>
#include <utility>
#include <vector>

namespace hotstuff
{
namespace
{

constexpr char kExactContributionFingerprintDomain[] =
    "KAURI_EXACT_CONTRIBUTION_FINGERPRINT_V1";

template<typename UInt>
void append_big_endian(bytearray_t &output, UInt value)
{
    static_assert(std::is_unsigned<UInt>::value,
                  "canonical integers must be unsigned");
    for (std::size_t shift = sizeof(UInt); shift > 0; --shift)
    {
        output.push_back(static_cast<std::uint8_t>(
            value >> ((shift - 1) * 8)));
    }
}

promise_t resolved(bool value)
{
    return promise_t([value](promise_t &promise) {
        promise.resolve(value);
    });
}

promise_t start_prerequisite(
    const std::function<promise_t()> &start)
{
    try
    {
        return start();
    }
    catch (...)
    {
        return resolved(false);
    }
}

template<typename T>
T &require_owner(const std::shared_ptr<T> &owner)
{
    if (owner == nullptr)
        throw std::invalid_argument(
            "exact vote coordinator requires owned effects and lifecycle");
    return *owner;
}

} // namespace

ExactContributionEnvelope make_exact_direct_envelope(
    const Vote &vote,
    ReplicaID authenticated_sender)
{
    ExactContributionEnvelope contribution;
    contribution.message_key = vote.key();
    contribution.certificate_key =
        vote.cert == nullptr
            ? ProposalKey{}
            : vote.cert->get_proposal_key();
    contribution.authenticated_sender = authenticated_sender;
    contribution.claimed_voter = vote.voter;
    contribution.certified_signers = {vote.voter};
    contribution.direct_vote = std::make_shared<const Vote>(vote);
    return contribution;
}

ExactContributionEnvelope make_exact_relay_envelope(
    const VoteRelay &relay,
    ReplicaID authenticated_sender)
{
    ExactContributionEnvelope contribution;
    contribution.message_key = relay.key();
    contribution.certificate_key =
        relay.cert == nullptr
            ? ProposalKey{}
            : relay.cert->get_proposal_key();
    contribution.authenticated_sender = authenticated_sender;
    if (relay.cert != nullptr)
    {
        const auto signers = relay.cert->get_signers();
        const std::set<ReplicaID> unique_signers(
            signers.begin(), signers.end());
        if (unique_signers.size() == signers.size() &&
            signers.size() == relay.cert->get_sigs_n())
            contribution.certified_signers = unique_signers;
    }
    contribution.aggregate_relay =
        std::make_shared<const VoteRelay>(relay);
    return contribution;
}

uint256_t exact_contribution_fingerprint(
    ExactContributionKind kind,
    const ExactContributionEnvelope &contribution)
{
    bytearray_t bytes(
        kExactContributionFingerprintDomain,
        kExactContributionFingerprintDomain +
            sizeof(kExactContributionFingerprintDomain) - 1);
    const auto encoded_kind = static_cast<std::uint8_t>(
        kind == ExactContributionKind::direct_vote ? 0 : 1);
    append_big_endian(bytes, encoded_kind);

    const auto exact_key = canonical_serialize_exact_vote(
        contribution.message_key);
    bytes.insert(bytes.end(), exact_key.begin(), exact_key.end());
    append_big_endian(bytes, contribution.authenticated_sender);

    const auto has_claimed_voter = static_cast<std::uint8_t>(
        contribution.claimed_voter.has_value() ? 1 : 0);
    append_big_endian(bytes, has_claimed_voter);
    if (contribution.claimed_voter.has_value())
        append_big_endian(bytes, *contribution.claimed_voter);

    append_big_endian(
        bytes,
        static_cast<std::uint32_t>(
            contribution.certified_signers.size()));
    for (const auto signer : contribution.certified_signers)
        append_big_endian(bytes, signer);

    return DataStream(bytes).get_hash();
}

bool passes_exact_contribution_cheap_gate(
    ExactContributionKind kind,
    const ExactContributionEnvelope &contribution,
    const ProposalKey &key,
    const ProposalTreeSnapshot &tree) noexcept
{
    if (contribution.message_key != contribution.certificate_key ||
        contribution.message_key != key)
        return false;

    if (kind == ExactContributionKind::direct_vote)
    {
        if (!contribution.claimed_voter.has_value() ||
            *contribution.claimed_voter !=
                contribution.authenticated_sender ||
            contribution.certified_signers.size() != 1 ||
            contribution.certified_signers.count(
                *contribution.claimed_voter) != 1)
            return false;

        const auto direct_child = tree.child_subtrees.find(
            contribution.authenticated_sender);
        if (direct_child != tree.child_subtrees.end())
            return true;

        // Only the exact root may accept the delayed direct-vote fallback.
        // The authenticated voter must still belong to the immutable
        // proposal membership. Non-roots retain the direct-child-only gate.
        return tree.local_replica == tree.root &&
               !tree.parent.has_value() &&
               std::find(
                   tree.assigned_subtree.begin(),
                   tree.assigned_subtree.end(),
                   contribution.authenticated_sender) !=
                   tree.assigned_subtree.end();
    }

    const auto child = tree.child_subtrees.find(
        contribution.authenticated_sender);
    if (child == tree.child_subtrees.end())
        return false;
    if (contribution.claimed_voter.has_value() ||
        contribution.certified_signers.empty())
        return false;
    return std::all_of(
        contribution.certified_signers.begin(),
        contribution.certified_signers.end(),
        [&child](ReplicaID signer) {
            return child->second.count(signer) != 0;
        });
}

ExactVoteHandlerCoordinator::ExactVoteHandlerCoordinator(
    ProposalContextLifecycle &contexts,
    ExactVoteHandlerEffects &effects) noexcept
    : contexts_(contexts), effects_(effects)
{}

ExactVoteHandlerCoordinator::ExactVoteHandlerCoordinator(
    std::shared_ptr<ProposalContextLifecycle> contexts,
    std::shared_ptr<ExactVoteHandlerEffects> effects)
    : contexts_(require_owner(contexts)),
      effects_(require_owner(effects)),
      contexts_owner_(std::move(contexts)),
      effects_owner_(std::move(effects))
{}

promise_t ExactVoteHandlerCoordinator::handle_direct(
    const ExactContributionEnvelope &contribution)
{
    return handle(ExactContributionKind::direct_vote, contribution);
}

promise_t ExactVoteHandlerCoordinator::handle_relay(
    const ExactContributionEnvelope &contribution)
{
    return handle(ExactContributionKind::aggregate_relay, contribution);
}

promise_t ExactVoteHandlerCoordinator::handle(
    ExactContributionKind kind,
    const ExactContributionEnvelope &contribution)
{
    auto lease = contexts_.acquire_open_context(
        contribution.message_key);
    if (!lease.has_value() ||
        !passes_exact_contribution_cheap_gate(
            kind, contribution, lease->key(), lease->tree()))
        return resolved(false);

    auto *effects = &effects_;
    auto *contexts = &contexts_;
    auto effects_owner = effects_owner_;
    auto contexts_owner = contexts_owner_;
    auto verification = start_prerequisite([effects, effects_owner,
                                             kind, contribution]() {
        static_cast<void>(effects_owner);
        return effects->start_worker_verification(kind, contribution);
    });
    auto delivery = start_prerequisite([effects, effects_owner,
                                         contribution]() {
        static_cast<void>(effects_owner);
        return effects->start_block_delivery(contribution.message_key);
    });

    promise_t completion;
    auto prerequisites = promise::all(
        std::vector<promise_t>{verification, delivery});
    prerequisites.then(
        [contexts,
         effects,
         contexts_owner,
         effects_owner,
         lease = std::move(*lease),
         kind,
         contribution,
         completion](
            const promise::values_t &values) mutable {
            static_cast<void>(contexts_owner);
            static_cast<void>(effects_owner);
            bool verified = false;
            bool delivered = false;
            try
            {
                if (values.size() == 2)
                {
                    verified = promise::any_cast<bool>(values[0]);
                    delivered = promise::any_cast<bool>(values[1]);
                }
            }
            catch (...)
            {
                completion.resolve(false);
                return;
            }

            if (!verified || !delivered || !contexts->revalidate(lease))
            {
                completion.resolve(false);
                return;
            }

            try
            {
                effects->continue_verified(lease, kind, contribution);
                completion.resolve(true);
            }
            catch (...)
            {
                completion.resolve(false);
            }
        },
        [completion]() mutable {
            completion.resolve(false);
        });
    return completion;
}

} // namespace hotstuff
