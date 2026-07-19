/**
 * Bounded proposal-side ownership for one adaptive-v2 epoch-change command.
 */

#ifndef HOTSTUFF_EPOCH_CHANGE_INBOX_H_INCLUDED
#define HOTSTUFF_EPOCH_CHANGE_INBOX_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <limits>
#include <memory>
#include <optional>

#include "hotstuff/epoch_change_bundle.h"

namespace hotstuff
{

struct AdaptiveV2CommandInboxLimits
{
    std::size_t maximum_bundle_bytes{2 * 1024 * 1024};
    std::size_t maximum_block_extra_bytes{4096};
    std::uint64_t maximum_reservation_token{
        std::numeric_limits<std::uint64_t>::max()};
};

struct AdaptiveV2SuccessorIdentity
{
    std::uint32_t epoch_number{0};
    uint256_t predecessor_epoch_digest;
    uint256_t epoch_digest;

    bool operator==(
        const AdaptiveV2SuccessorIdentity &other) const noexcept;
    bool operator!=(
        const AdaptiveV2SuccessorIdentity &other) const noexcept;
};

/**
 * Canonical immutable material returned to the proposal producer.
 *
 * This object has no authority to create a proposal, vote, alter a quorum, or
 * select topology.  It is only the already verified block-extra payload and
 * the exact identities needed to account for it.
 */
struct AdaptiveV2CommandMaterial final
{
    AuthorizedEpochChange command;
    bytearray_t canonical_bundle;
    bytearray_t canonical_block_extra;
    uint256_t payload_digest;
    uint256_t envelope_digest;
    AdaptiveV2SuccessorIdentity successor;
};

enum class AdaptiveV2CommandIngestDisposition : std::uint8_t
{
    accepted = 0,
    duplicate,
    rejected,
    limit_exceeded,
    internal_failure,
};

struct AdaptiveV2CommandIngestResult
{
    AdaptiveV2CommandIngestDisposition disposition{
        AdaptiveV2CommandIngestDisposition::rejected};
    std::optional<EpochChangeDisposition> initial_validation;
    std::optional<DefinitionAvailabilityDisposition> definition_staging;
    std::optional<EpochChangeDisposition> final_validation;
    std::shared_ptr<const AdaptiveV2CommandMaterial> material;
};

struct AdaptiveV2ProposalPreparation
{
    ConfigurationId active_configuration;
    std::uint64_t active_generation{0};
    ReplicaID local_replica{0};
    ReplicaID root_replica{0};
    EpochChangeHistoryView history;
};

enum class AdaptiveV2CommandPrepareDisposition : std::uint8_t
{
    reserved = 0,
    covered_by_history,
    unavailable,
    busy,
    retired,
    wrong_configuration,
    wrong_generation,
    not_exact_root,
    conflicting_history,
    token_exhausted,
    internal_failure,
};

struct AdaptiveV2CommandReservation
{
    std::uint64_t token{0};
    ConfigurationId configuration;
    std::uint64_t generation{0};
    std::shared_ptr<const AdaptiveV2CommandMaterial> material;
};

struct AdaptiveV2CommandPrepareResult
{
    AdaptiveV2CommandPrepareDisposition disposition{
        AdaptiveV2CommandPrepareDisposition::unavailable};
    std::optional<AdaptiveV2CommandReservation> reservation;
    std::shared_ptr<const AdaptiveV2CommandMaterial> material;
};

enum class AdaptiveV2CommandInboxState : std::uint8_t
{
    empty = 0,
    available,
    reserved,
    in_flight,
    retired,
};

struct AdaptiveV2CommandInboxSnapshot
{
    AdaptiveV2CommandInboxState state{AdaptiveV2CommandInboxState::empty};
    std::shared_ptr<const AdaptiveV2CommandMaterial> material;
    std::optional<std::uint64_t> reservation_token;
    std::optional<ConfigurationId> reservation_configuration;
    std::optional<std::uint64_t> reservation_generation;
    std::optional<ProposalKey> in_flight_proposal;
};

/**
 * Event-loop-confined, one-command adaptive-v2 proposal inbox.
 *
 * Ingress authenticates and validates the signed command before allowing its
 * bundled definition to enter EpochStore, then revalidates against the exact
 * staged definition.  Proposal preparation only reserves immutable bytes for
 * the exact active root view supplied by the live runtime.  The caller remains
 * solely responsible for normal proposal construction and consensus.
 */
class AdaptiveV2CommandInbox final
{
public:
    explicit AdaptiveV2CommandInbox(
        AdaptiveV2CommandInboxLimits limits = {});
    ~AdaptiveV2CommandInbox();

    AdaptiveV2CommandInbox(const AdaptiveV2CommandInbox &) = delete;
    AdaptiveV2CommandInbox &operator=(
        const AdaptiveV2CommandInbox &) = delete;
    AdaptiveV2CommandInbox(AdaptiveV2CommandInbox &&) = delete;
    AdaptiveV2CommandInbox &operator=(AdaptiveV2CommandInbox &&) = delete;

    AdaptiveV2CommandIngestResult ingest(
        const AdaptiveV2EpochChangeBundle &bundle,
        const EpochDefinition &active_epoch,
        const EpochChangeVerifier &verifier,
        EpochStore &store) noexcept;

    AdaptiveV2CommandPrepareResult prepare_for_proposal(
        const AdaptiveV2ProposalPreparation &preparation) noexcept;

    bool release(std::uint64_t reservation_token) noexcept;

    bool mark_proposed(
        std::uint64_t reservation_token,
        const ProposalKey &proposal) noexcept;

    bool observe_authoritative_commit(
        const ProposalKey &committed_proposal,
        const std::optional<uint256_t> &committed_payload_digest) noexcept;

    bool observe_activation(
        const ConfigurationId &active_configuration,
        std::uint64_t active_generation) noexcept;

    AdaptiveV2CommandInboxSnapshot snapshot() const noexcept;

private:
    struct State;
    std::unique_ptr<State> state_;
};

} // namespace hotstuff

#endif
