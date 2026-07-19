#include "hotstuff/epoch_change_inbox.h"

#include <algorithm>
#include <limits>
#include <stdexcept>
#include <utility>
#include <vector>

namespace hotstuff
{
namespace
{

struct ExactTreeView
{
    std::uint32_t tree_id{0};
    ReplicaID root{0};
};

std::vector<ExactTreeView> exact_tree_views(
    const std::vector<EpochTreeDefinition> &trees)
{
    if (trees.empty())
        throw std::invalid_argument(
            "adaptive-v2 inbox requires at least one exact tree");

    std::vector<ExactTreeView> result;
    result.reserve(trees.size());
    for (const auto &tree : trees)
    {
        if (tree.members_breadth_first.empty())
            throw std::invalid_argument(
                "adaptive-v2 inbox tree has no exact root");
        result.push_back(
            ExactTreeView{tree.tree_id, tree.members_breadth_first.front()});
    }
    return result;
}

bool contains_tree(
    const std::vector<ExactTreeView> &trees,
    std::uint32_t tree_id) noexcept
{
    return std::any_of(
        trees.begin(), trees.end(), [tree_id](const ExactTreeView &tree) {
            return tree.tree_id == tree_id;
        });
}

const ExactTreeView *tree_for_generation(
    const std::vector<ExactTreeView> &trees,
    std::uint32_t epoch_number,
    std::uint64_t generation) noexcept
{
    if (generation == 0 || trees.empty())
        return nullptr;
    const auto packed = generation - 1;
    if ((packed >> 32) != epoch_number)
        return nullptr;
    const auto ordinal = static_cast<std::uint32_t>(packed);
    return &trees[ordinal % trees.size()];
}

bool exact_defer(
    const EpochChangeValidationResult &validation,
    const AuthorizedEpochChange &command) noexcept
{
    return validation.disposition ==
               EpochChangeDisposition::defer_missing_definition &&
           validation.recovery_request.has_value() &&
           validation.recovery_request->protocol_mode ==
               EpochProtocolMode::adaptive_v2 &&
           validation.recovery_request->successor_epoch_digest ==
               command.payload.successor_epoch_digest;
}

bool may_stage(
    const EpochChangeValidationResult &validation,
    const AuthorizedEpochChange &command) noexcept
{
    return validation.disposition == EpochChangeDisposition::accepted ||
           validation.disposition == EpochChangeDisposition::duplicate ||
           exact_defer(validation, command);
}

bool exact_predecessor_configuration(
    const ConfigurationId &configuration,
    std::uint32_t predecessor_epoch_number,
    const uint256_t &predecessor_epoch_digest) noexcept
{
    return configuration.epoch_number == predecessor_epoch_number &&
           configuration.epoch_digest == predecessor_epoch_digest;
}

} // namespace

bool AdaptiveV2SuccessorIdentity::operator==(
    const AdaptiveV2SuccessorIdentity &other) const noexcept
{
    return epoch_number == other.epoch_number &&
           predecessor_epoch_digest == other.predecessor_epoch_digest &&
           epoch_digest == other.epoch_digest;
}

bool AdaptiveV2SuccessorIdentity::operator!=(
    const AdaptiveV2SuccessorIdentity &other) const noexcept
{
    return !(*this == other);
}

struct AdaptiveV2CommandInbox::State
{
    struct Record
    {
        std::shared_ptr<const AdaptiveV2CommandMaterial> material;
        std::uint32_t predecessor_epoch_number{0};
        std::vector<ExactTreeView> predecessor_trees;
        std::vector<ExactTreeView> successor_trees;
        AdaptiveV2CommandInboxState disposition{
            AdaptiveV2CommandInboxState::available};
        std::optional<std::uint64_t> reservation_token;
        std::optional<ConfigurationId> reservation_configuration;
        std::optional<std::uint64_t> reservation_generation;
        std::optional<ProposalKey> in_flight_proposal;
        std::optional<ConfigurationId> latest_configuration;
        std::optional<std::uint64_t> latest_generation;
    };

    explicit State(AdaptiveV2CommandInboxLimits configured_limits)
        : limits(std::move(configured_limits))
    {}

    AdaptiveV2CommandInboxLimits limits;
    std::unique_ptr<Record> record;
    std::uint64_t next_reservation_token{1};
};

AdaptiveV2CommandInbox::AdaptiveV2CommandInbox(
    AdaptiveV2CommandInboxLimits limits)
    : state_(std::make_unique<State>(std::move(limits)))
{
    if (state_->limits.maximum_bundle_bytes == 0 ||
        state_->limits.maximum_block_extra_bytes == 0 ||
        state_->limits.maximum_reservation_token == 0)
    {
        throw std::invalid_argument(
            "adaptive-v2 inbox limits must be nonzero");
    }
}

AdaptiveV2CommandInbox::~AdaptiveV2CommandInbox() = default;

AdaptiveV2CommandIngestResult AdaptiveV2CommandInbox::ingest(
    const AdaptiveV2EpochChangeBundle &bundle,
    const EpochDefinition &active_epoch,
    const EpochChangeVerifier &verifier,
    EpochStore &store) noexcept
{
    AdaptiveV2CommandIngestResult result;
    try
    {
        if (bundle.canonical_bytes().size() >
            state_->limits.maximum_bundle_bytes)
        {
            result.disposition =
                AdaptiveV2CommandIngestDisposition::limit_exceeded;
            return result;
        }

        const auto initial = verifier.validate(
            bundle.command(), active_epoch, store, EpochChangeHistoryView{});
        result.initial_validation = initial.disposition;
        if (!may_stage(initial, bundle.command()))
            return result;

        const auto payload_digest =
            epoch_change_payload_digest(bundle.command().payload);
        const auto envelope_digest =
            epoch_change_envelope_digest(bundle.command());
        if (initial.payload_digest != payload_digest ||
            initial.envelope_digest != envelope_digest)
        {
            return result;
        }

        if (state_->record != nullptr)
        {
            if (state_->record->material->canonical_bundle ==
                bundle.canonical_bytes())
            {
                result.disposition =
                    AdaptiveV2CommandIngestDisposition::duplicate;
                result.material = state_->record->material;
            }
            return result;
        }

        auto block_extra =
            encode_epoch_change_block_extra(bundle.command());
        if (block_extra.size() >
            state_->limits.maximum_block_extra_bytes)
        {
            result.disposition =
                AdaptiveV2CommandIngestDisposition::limit_exceeded;
            return result;
        }

        auto prepared = std::make_unique<State::Record>();
        prepared->predecessor_epoch_number = active_epoch.epoch_number();
        prepared->predecessor_trees = exact_tree_views(active_epoch.trees());
        prepared->successor_trees =
            exact_tree_views(bundle.definition().trees);
        prepared->material =
            std::make_shared<const AdaptiveV2CommandMaterial>(
                AdaptiveV2CommandMaterial{
                    bundle.command(),
                    bundle.canonical_bytes(),
                    std::move(block_extra),
                    payload_digest,
                    envelope_digest,
                    AdaptiveV2SuccessorIdentity{
                        bundle.command().payload.successor_epoch_number,
                        bundle.command().payload.predecessor_epoch_digest,
                        bundle.command().payload.successor_epoch_digest}});

        const auto staged = store.stage_available_v2(
            bundle.definition(), active_epoch);
        result.definition_staging = staged.disposition;
        if ((staged.disposition !=
                 DefinitionAvailabilityDisposition::staged &&
             staged.disposition !=
                 DefinitionAvailabilityDisposition::duplicate) ||
            staged.definition == nullptr)
        {
            return result;
        }

        const auto final_validation = verifier.validate(
            bundle.command(), active_epoch, store, EpochChangeHistoryView{});
        result.final_validation = final_validation.disposition;
        if (final_validation.disposition !=
                EpochChangeDisposition::accepted ||
            final_validation.successor_definition != staged.definition ||
            final_validation.payload_digest != payload_digest ||
            final_validation.envelope_digest != envelope_digest ||
            staged.definition->epoch_number() !=
                prepared->material->successor.epoch_number ||
            staged.definition->previous_epoch_digest() !=
                prepared->material->successor.predecessor_epoch_digest ||
            staged.definition->epoch_digest() !=
                prepared->material->successor.epoch_digest)
        {
            return result;
        }

        result.disposition = AdaptiveV2CommandIngestDisposition::accepted;
        result.material = prepared->material;
        state_->record = std::move(prepared);
        return result;
    }
    catch (...)
    {
        result.disposition =
            AdaptiveV2CommandIngestDisposition::internal_failure;
        result.material.reset();
        return result;
    }
}

AdaptiveV2CommandPrepareResult
AdaptiveV2CommandInbox::prepare_for_proposal(
    const AdaptiveV2ProposalPreparation &preparation) noexcept
{
    AdaptiveV2CommandPrepareResult result;
    try
    {
        if (state_->record == nullptr)
            return result;
        auto &record = *state_->record;
        result.material = record.material;

        const auto &successor = record.material->successor;
        if (!exact_predecessor_configuration(
                preparation.active_configuration,
                record.predecessor_epoch_number,
                successor.predecessor_epoch_digest))
        {
            result.disposition =
                AdaptiveV2CommandPrepareDisposition::wrong_configuration;
            return result;
        }

        const auto *const exact_tree = tree_for_generation(
            record.predecessor_trees,
            record.predecessor_epoch_number,
            preparation.active_generation);
        if (exact_tree == nullptr ||
            exact_tree->tree_id != preparation.active_configuration.tree_id)
        {
            result.disposition =
                AdaptiveV2CommandPrepareDisposition::wrong_generation;
            return result;
        }
        if (preparation.local_replica != preparation.root_replica ||
            preparation.root_replica != exact_tree->root)
        {
            result.disposition =
                AdaptiveV2CommandPrepareDisposition::not_exact_root;
            return result;
        }

        const auto &history = preparation.history;
        if ((history.ancestry_payload_digest &&
             *history.ancestry_payload_digest !=
                 record.material->payload_digest) ||
            (history.committed_payload_digest &&
             *history.committed_payload_digest !=
                 record.material->payload_digest))
        {
            result.disposition =
                AdaptiveV2CommandPrepareDisposition::conflicting_history;
            return result;
        }
        if (history.ancestry_payload_digest ||
            history.committed_payload_digest)
        {
            result.disposition =
                AdaptiveV2CommandPrepareDisposition::covered_by_history;
            return result;
        }

        if (record.latest_generation &&
            (preparation.active_generation < *record.latest_generation ||
             (preparation.active_generation == *record.latest_generation &&
              record.latest_configuration &&
              preparation.active_configuration !=
                  *record.latest_configuration)))
        {
            result.disposition =
                AdaptiveV2CommandPrepareDisposition::wrong_generation;
            return result;
        }

        if (record.disposition == AdaptiveV2CommandInboxState::reserved)
        {
            result.disposition =
                AdaptiveV2CommandPrepareDisposition::busy;
            return result;
        }
        if (record.disposition == AdaptiveV2CommandInboxState::retired)
        {
            result.disposition =
                AdaptiveV2CommandPrepareDisposition::retired;
            return result;
        }
        if (record.disposition != AdaptiveV2CommandInboxState::available &&
            record.disposition != AdaptiveV2CommandInboxState::in_flight)
        {
            result.disposition =
                AdaptiveV2CommandPrepareDisposition::internal_failure;
            return result;
        }
        if (state_->next_reservation_token == 0)
        {
            result.disposition =
                AdaptiveV2CommandPrepareDisposition::token_exhausted;
            return result;
        }

        const auto token = state_->next_reservation_token;
        state_->next_reservation_token =
            token == state_->limits.maximum_reservation_token
                ? 0
                : token + 1;
        record.disposition = AdaptiveV2CommandInboxState::reserved;
        record.reservation_token = token;
        record.reservation_configuration =
            preparation.active_configuration;
        record.reservation_generation = preparation.active_generation;
        record.in_flight_proposal.reset();
        record.latest_configuration = preparation.active_configuration;
        record.latest_generation = preparation.active_generation;

        result.disposition =
            AdaptiveV2CommandPrepareDisposition::reserved;
        result.reservation = AdaptiveV2CommandReservation{
            token,
            preparation.active_configuration,
            preparation.active_generation,
            record.material};
        return result;
    }
    catch (...)
    {
        result.disposition =
            AdaptiveV2CommandPrepareDisposition::internal_failure;
        result.reservation.reset();
        return result;
    }
}

bool AdaptiveV2CommandInbox::release(
    std::uint64_t reservation_token) noexcept
{
    if (reservation_token == 0 || state_->record == nullptr)
        return false;
    auto &record = *state_->record;
    if (record.disposition != AdaptiveV2CommandInboxState::reserved ||
        !record.reservation_token ||
        *record.reservation_token != reservation_token)
    {
        return false;
    }
    record.disposition = AdaptiveV2CommandInboxState::available;
    record.reservation_token.reset();
    record.reservation_configuration.reset();
    record.reservation_generation.reset();
    return true;
}

bool AdaptiveV2CommandInbox::mark_proposed(
    std::uint64_t reservation_token,
    const ProposalKey &proposal) noexcept
{
    if (reservation_token == 0 || state_->record == nullptr ||
        proposal.block_hash == uint256_t{})
    {
        return false;
    }
    auto &record = *state_->record;
    if (record.disposition != AdaptiveV2CommandInboxState::reserved ||
        !record.reservation_token ||
        *record.reservation_token != reservation_token ||
        !record.reservation_configuration ||
        proposal.configuration != *record.reservation_configuration)
    {
        return false;
    }
    record.disposition = AdaptiveV2CommandInboxState::in_flight;
    record.in_flight_proposal = proposal;
    record.reservation_token.reset();
    record.reservation_configuration.reset();
    record.reservation_generation.reset();
    return true;
}

bool AdaptiveV2CommandInbox::observe_authoritative_commit(
    const ProposalKey &committed_proposal,
    const std::optional<uint256_t> &committed_payload_digest) noexcept
{
    if (state_->record == nullptr || !committed_payload_digest ||
        committed_proposal.block_hash == uint256_t{})
    {
        return false;
    }
    auto &record = *state_->record;
    if (*committed_payload_digest != record.material->payload_digest ||
        !exact_predecessor_configuration(
            committed_proposal.configuration,
            record.predecessor_epoch_number,
            record.material->successor.predecessor_epoch_digest) ||
        !contains_tree(
            record.predecessor_trees,
            committed_proposal.configuration.tree_id))
    {
        return false;
    }

    record.disposition = AdaptiveV2CommandInboxState::retired;
    record.reservation_token.reset();
    record.reservation_configuration.reset();
    record.reservation_generation.reset();
    record.in_flight_proposal.reset();
    return true;
}

bool AdaptiveV2CommandInbox::observe_activation(
    const ConfigurationId &active_configuration,
    std::uint64_t active_generation) noexcept
{
    if (state_->record == nullptr ||
        state_->record->disposition !=
            AdaptiveV2CommandInboxState::retired)
    {
        return false;
    }
    const auto &record = *state_->record;
    const auto &successor = record.material->successor;
    if (active_configuration.epoch_number != successor.epoch_number ||
        active_configuration.epoch_digest != successor.epoch_digest)
    {
        return false;
    }
    const auto *const exact_tree = tree_for_generation(
        record.successor_trees,
        successor.epoch_number,
        active_generation);
    if (exact_tree == nullptr ||
        exact_tree->tree_id != active_configuration.tree_id)
    {
        return false;
    }
    state_->record.reset();
    return true;
}

AdaptiveV2CommandInboxSnapshot
AdaptiveV2CommandInbox::snapshot() const noexcept
{
    AdaptiveV2CommandInboxSnapshot result;
    if (state_->record == nullptr)
        return result;
    const auto &record = *state_->record;
    result.state = record.disposition;
    result.material = record.material;
    result.reservation_token = record.reservation_token;
    result.reservation_configuration = record.reservation_configuration;
    result.reservation_generation = record.reservation_generation;
    result.in_flight_proposal = record.in_flight_proposal;
    return result;
}

} // namespace hotstuff
