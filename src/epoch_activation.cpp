#include "hotstuff/epoch_activation.h"

#include "hotstuff/adaptive_v3_activation_readiness.h"

#include <algorithm>
#include <limits>
#include <set>
#include <stdexcept>
#include <utility>

#include "hotstuff/configuration.h"

namespace hotstuff
{
namespace
{

bool definition_matches_activation(
    const EpochDefinitionInput &definition,
    const EpochActivationIdentity &activation)
{
    if (definition.epoch_number != activation.successor_epoch_number ||
        definition.previous_epoch_digest !=
            activation.predecessor_epoch_digest ||
        definition.activation_height != activation.activation_height)
    {
        return false;
    }
    const auto digest = compute_epoch_digest(definition);
    return digest == activation.successor_epoch_digest &&
           (!definition.epoch_digest ||
            *definition.epoch_digest == digest);
}

bool has_tree_zero(const EpochDefinitionInput &definition)
{
    return std::any_of(
        definition.trees.begin(), definition.trees.end(),
        [](const auto &tree) { return tree.tree_id == 0; });
}

bool is_exact_successor(
    const EpochDefinition &active,
    const EpochActivationIdentity &activation)
{
    const auto successor = checked_successor_epoch(active.epoch_number());
    return successor &&
           activation.predecessor_epoch_number == active.epoch_number() &&
           activation.predecessor_epoch_digest == active.epoch_digest() &&
           activation.successor_epoch_number == *successor;
}

} // namespace

struct EpochAckTracker::State
{
    State(
        EpochActivationIdentity activation_value,
        std::vector<ReplicaID> membership_value,
        std::uint32_t tolerated_faults)
        : activation(std::move(activation_value))
    {
        if (membership_value.empty())
        {
            throw std::invalid_argument(
                "epoch acknowledgement membership is empty");
        }
        std::sort(membership_value.begin(), membership_value.end());
        if (std::adjacent_find(
                membership_value.begin(), membership_value.end()) !=
            membership_value.end())
        {
            throw std::invalid_argument(
                "epoch acknowledgement membership contains duplicates");
        }

        const auto faults = static_cast<std::uint64_t>(tolerated_faults);
        const auto minimum_membership = faults * 3 + 1;
        const auto threshold = faults * 2 + 1;
        if (minimum_membership > membership_value.size() ||
            threshold > membership_value.size())
        {
            throw std::invalid_argument(
                "epoch acknowledgement fault bound exceeds membership");
        }

        membership.insert(
            membership_value.begin(), membership_value.end());
        required = static_cast<std::size_t>(threshold);
    }

    const EpochActivationIdentity activation;
    std::set<ReplicaID> membership;
    std::set<ReplicaID> accepted;
    std::size_t required{0};
};

EpochAckTracker::EpochAckTracker(
    EpochActivationIdentity staged_activation,
    std::vector<ReplicaID> fixed_membership,
    std::uint32_t tolerated_faults)
    : state_(new State(
          std::move(staged_activation),
          std::move(fixed_membership),
          tolerated_faults))
{
}

EpochAckTracker::~EpochAckTracker() = default;

StageAckRecordResult EpochAckTracker::record(
    const AuthenticatedReporter &authenticated_reporter,
    const StageAck &acknowledgement)
{
    auto result = [&](StageAckDisposition disposition) {
        return StageAckRecordResult{
            disposition,
            state_->accepted.size(),
            state_->required,
            ready()};
    };

    if (acknowledgement.wire_schema_version != kEpochWireSchemaVersion)
    {
        return result(StageAckDisposition::unsupported_schema);
    }
    if (acknowledgement.protocol_mode != EpochProtocolMode::adaptive_v1)
    {
        return result(StageAckDisposition::wrong_mode);
    }
    if (authenticated_reporter.replica_id != acknowledgement.replica_id)
    {
        return result(StageAckDisposition::reporter_mismatch);
    }
    if (state_->membership.count(authenticated_reporter.replica_id) == 0)
    {
        return result(StageAckDisposition::foreign_replica);
    }
    if (acknowledgement.activation != state_->activation)
    {
        return result(StageAckDisposition::wrong_activation);
    }
    if (state_->accepted.count(acknowledgement.replica_id) != 0)
    {
        return result(StageAckDisposition::duplicate);
    }

    state_->accepted.insert(acknowledgement.replica_id);
    return result(StageAckDisposition::accepted);
}

ArmActivation EpochAckTracker::build_arm() const
{
    if (!ready())
    {
        throw std::logic_error(
            "epoch activation cannot be armed before acknowledgement quorum");
    }
    return {
        kEpochWireSchemaVersion,
        EpochProtocolMode::adaptive_v1,
        state_->activation};
}

std::size_t EpochAckTracker::acknowledgement_count() const noexcept
{
    return state_->accepted.size();
}

std::size_t EpochAckTracker::required_acknowledgements() const noexcept
{
    return state_->required;
}

bool EpochAckTracker::ready() const noexcept
{
    return acknowledgement_count() >= required_acknowledgements();
}

std::optional<std::uint32_t> checked_successor_epoch(
    std::uint32_t current_epoch) noexcept
{
    if (current_epoch == std::numeric_limits<std::uint32_t>::max())
    {
        return std::nullopt;
    }
    return current_epoch + 1;
}

std::optional<std::uint64_t> checked_activation_generation(
    std::uint32_t epoch_number,
    std::uint64_t rotation_ordinal) noexcept
{
    if (rotation_ordinal > std::numeric_limits<std::uint32_t>::max())
    {
        return std::nullopt;
    }
    const auto packed =
        (static_cast<std::uint64_t>(epoch_number) << 32) |
        rotation_ordinal;
    if (packed == std::numeric_limits<std::uint64_t>::max())
    {
        return std::nullopt;
    }
    return packed + 1;
}

bool ActivationRecord::operator==(
    const ActivationRecord &other) const noexcept
{
    return predecessor_epoch_number == other.predecessor_epoch_number &&
           predecessor_epoch_digest == other.predecessor_epoch_digest &&
           successor_epoch_number == other.successor_epoch_number &&
           successor_epoch_digest == other.successor_epoch_digest &&
           payload_digest == other.payload_digest &&
           command_commit_height == other.command_commit_height &&
           activation_delay_blocks == other.activation_delay_blocks &&
           activation_height == other.activation_height;
}

bool ActivationRecord::operator!=(
    const ActivationRecord &other) const noexcept
{
    return !(*this == other);
}

struct ReplicaEpochActivation::State
{
    struct CommitProof
    {
        std::uint64_t height;
        uint256_t digest;
    };

    State(
        EpochStore &epoch_store,
        const EpochDefinition &active_epoch,
        ReplicaID replica,
        std::uint32_t tree_id,
        std::uint32_t ordinal)
        : store(epoch_store),
          active_definition(&active_epoch),
          local_replica(replica),
          active_tree_id(tree_id),
          rotation_ordinal(ordinal)
    {
        if (store.find_epoch(active_epoch.epoch_number()) != &active_epoch)
        {
            throw std::invalid_argument(
                "active epoch must be owned by the supplied store");
        }
        const auto *const active_tree = store.find_tree(
            active_epoch.epoch_number(), active_tree_id);
        if (active_tree == nullptr)
        {
            throw std::invalid_argument("active epoch tree does not exist");
        }
        if (std::find(
                active_tree->members_breadth_first.begin(),
                active_tree->members_breadth_first.end(),
                local_replica) == active_tree->members_breadth_first.end())
        {
            throw std::invalid_argument(
                "local replica is not in the active tree");
        }
        if (!checked_activation_generation(
                active_epoch.epoch_number(), rotation_ordinal))
        {
            throw std::overflow_error(
                "initial epoch activation generation overflows");
        }
        if (active_epoch.epoch_number() != 0 &&
            active_epoch.schema_version() ==
                kEpochDefinitionSchemaVersionV1)
        {
            completed_activation = EpochActivationIdentity{
                active_epoch.epoch_number() - 1,
                active_epoch.previous_epoch_digest(),
                active_epoch.epoch_number(),
                active_epoch.epoch_digest(),
                active_epoch.activation_height()};
        }
    }

    ConfigurationId active_configuration() const
    {
        return {
            active_definition->epoch_number(),
            active_tree_id,
            active_definition->epoch_digest()};
    }

    EpochActivationEffect effect() const
    {
        const auto generation = checked_activation_generation(
            active_definition->epoch_number(), rotation_ordinal);
        if (!generation)
        {
            throw std::logic_error(
                "stored epoch activation generation is invalid");
        }
        return {
            active_definition,
            active_configuration(),
            rotation_ordinal,
            *generation};
    }

    bool active_is_completed_activation() const noexcept
    {
        return completed_activation &&
               active_definition->epoch_number() ==
                   completed_activation->successor_epoch_number &&
               active_definition->epoch_digest() ==
                   completed_activation->successor_epoch_digest;
    }

    bool is_exact_completed_commit(
        std::uint64_t height,
        const uint256_t &digest) const noexcept
    {
        return active_is_completed_activation() &&
               height == completed_activation->activation_height &&
               digest == completed_activation->predecessor_epoch_digest;
    }

    const EpochDefinition *exact_staged_definition() const noexcept
    {
        if (!expected_activation)
        {
            return nullptr;
        }
        const auto *const definition = store.find_epoch(
            expected_activation->successor_epoch_number);
        if (definition == nullptr ||
            definition->epoch_digest() !=
                expected_activation->successor_epoch_digest ||
            definition->previous_epoch_digest() !=
                expected_activation->predecessor_epoch_digest ||
            definition->activation_height() !=
                expected_activation->activation_height)
        {
            return nullptr;
        }
        return definition;
    }

    bool active_is_completed_v2_activation() const noexcept
    {
        return completed_v2_activation_record &&
               active_definition->epoch_number() ==
                   completed_v2_activation_record->successor_epoch_number &&
               active_definition->epoch_digest() ==
                   completed_v2_activation_record->successor_epoch_digest;
    }

    const EpochDefinition *exact_staged_v2_definition() const noexcept
    {
        if (!pending_v2_activation_record)
            return nullptr;
        const auto &record = *pending_v2_activation_record;
        const auto *const definition = store.find_epoch_by_digest(
            record.successor_epoch_digest);
        if (definition == nullptr ||
            definition->schema_version() !=
                kEpochDefinitionSchemaVersionV2 ||
            definition->activation_height() != 0 ||
            definition->epoch_number() != record.successor_epoch_number ||
            definition->previous_epoch_digest() !=
                record.predecessor_epoch_digest ||
            store.find_tree(record.successor_epoch_number, 0) == nullptr)
        {
            return nullptr;
        }
        return definition;
    }

    EpochActivationResult waiting_result() const
    {
        return {
            ActivationTransition::waiting,
            ActivationBlockReason::none,
            std::nullopt};
    }

    EpochActivationResult blocked_result() const
    {
        return {
            ActivationTransition::blocked,
            block_reason,
            std::nullopt};
    }

    EpochActivationResult active_result(
        ActivationTransition transition) const
    {
        return {
            transition,
            ActivationBlockReason::none,
            std::optional<EpochActivationEffect>(effect())};
    }

    EpochActivationResult prospective_active_result(
        const EpochDefinition &definition) const
    {
        const auto generation = checked_activation_generation(
            definition.epoch_number(), 0);
        if (!generation)
        {
            throw std::logic_error(
                "prospective epoch activation generation is invalid");
        }
        return {
            ActivationTransition::activated,
            ActivationBlockReason::none,
            EpochActivationEffect{
                &definition,
                ConfigurationId{
                    definition.epoch_number(),
                    0,
                    definition.epoch_digest()},
                0,
                *generation}};
    }

    EpochActivationResult preview_v2_post_block(
        std::uint64_t height,
        const uint256_t &predecessor_digest) const
    {
        if (permanent_block)
            return blocked_result();
        if (!pending_v2_activation_record)
        {
            if (active_is_completed_v2_activation())
                return active_result(ActivationTransition::already_active);
            return waiting_result();
        }

        const auto &record = *pending_v2_activation_record;
        if (block_reason == ActivationBlockReason::missing_definition &&
            exact_staged_v2_definition() == nullptr)
        {
            return {
                ActivationTransition::blocked,
                ActivationBlockReason::missing_definition,
                std::nullopt};
        }
        if (predecessor_digest != record.predecessor_epoch_digest)
        {
            return {
                ActivationTransition::blocked,
                ActivationBlockReason::predecessor_digest_mismatch,
                std::nullopt};
        }
        if (height < record.activation_height)
            return waiting_result();
        if (height > record.activation_height)
        {
            return {
                ActivationTransition::blocked,
                ActivationBlockReason::missed_activation_height,
                std::nullopt};
        }
        const auto *const definition = exact_staged_v2_definition();
        if (definition == nullptr)
        {
            return {
                ActivationTransition::blocked,
                ActivationBlockReason::missing_definition,
                std::nullopt};
        }
        return prospective_active_result(*definition);
    }

    EpochActivationResult apply_v2_post_block(
        std::uint64_t height,
        const uint256_t &predecessor_digest)
    {
        const auto preview = preview_v2_post_block(
            height, predecessor_digest);
        if (preview.transition == ActivationTransition::blocked)
        {
            block_reason = preview.blocked_reason;
            if (preview.blocked_reason !=
                ActivationBlockReason::missing_definition)
            {
                permanent_block = true;
            }
            return blocked_result();
        }
        if (preview.transition != ActivationTransition::activated)
            return preview;

        const auto *const definition = exact_staged_v2_definition();
        if (definition == nullptr)
        {
            block_reason = ActivationBlockReason::missing_definition;
            permanent_block = true;
            return blocked_result();
        }
        draining_configuration = active_configuration();
        active_definition = definition;
        active_tree_id = 0;
        rotation_ordinal = 0;
        completed_v2_activation_record.emplace(
            *pending_v2_activation_record);
        pending_v2_activation_record.reset();
        block_reason = ActivationBlockReason::none;
        permanent_block = false;
        return active_result(ActivationTransition::activated);
    }

    EpochActivationResult preview_proof(
        const std::optional<CommitProof> &proof) const
    {
        if (!expected_activation && active_is_completed_activation())
        {
            return active_result(ActivationTransition::already_active);
        }
        if (permanent_block)
        {
            return blocked_result();
        }
        if (!expected_activation || !proof)
        {
            return waiting_result();
        }
        if (proof->height > expected_activation->activation_height)
        {
            return {
                ActivationTransition::blocked,
                ActivationBlockReason::missed_activation_height,
                std::nullopt};
        }
        if (proof->height < expected_activation->activation_height)
        {
            return waiting_result();
        }
        if (proof->digest != expected_activation->predecessor_epoch_digest)
        {
            return {
                ActivationTransition::blocked,
                ActivationBlockReason::predecessor_digest_mismatch,
                std::nullopt};
        }
        const auto *const definition = exact_staged_definition();
        if (definition == nullptr)
        {
            return {
                ActivationTransition::blocked,
                ActivationBlockReason::missing_definition,
                std::nullopt};
        }
        if (!armed_activation || *armed_activation != *expected_activation)
        {
            return {
                ActivationTransition::blocked,
                ActivationBlockReason::missing_arm,
                std::nullopt};
        }
        return prospective_active_result(*definition);
    }

    EpochActivationResult evaluate_proof()
    {
        if (!expected_activation && active_is_completed_activation())
        {
            return active_result(ActivationTransition::already_active);
        }
        if (permanent_block)
        {
            return blocked_result();
        }
        if (!expected_activation || !commit_proof)
        {
            return waiting_result();
        }

        if (commit_proof->height > expected_activation->activation_height)
        {
            block_reason = ActivationBlockReason::missed_activation_height;
            permanent_block = true;
            return blocked_result();
        }
        if (commit_proof->height < expected_activation->activation_height)
        {
            return waiting_result();
        }
        if (commit_proof->digest !=
            expected_activation->predecessor_epoch_digest)
        {
            block_reason =
                ActivationBlockReason::predecessor_digest_mismatch;
            permanent_block = true;
            return blocked_result();
        }

        const auto *const definition = exact_staged_definition();
        if (definition == nullptr)
        {
            block_reason = ActivationBlockReason::missing_definition;
            return blocked_result();
        }
        if (!armed_activation ||
            *armed_activation != *expected_activation)
        {
            block_reason = ActivationBlockReason::missing_arm;
            return blocked_result();
        }
        if (store.find_tree(definition->epoch_number(), 0) == nullptr)
        {
            throw std::logic_error(
                "staged epoch does not contain activation tree zero");
        }

        draining_configuration = active_configuration();
        active_definition = definition;
        active_tree_id = 0;
        rotation_ordinal = 0;
        completed_activation = *expected_activation;
        expected_activation.reset();
        armed_activation.reset();
        commit_proof.reset();
        block_reason = ActivationBlockReason::none;
        permanent_block = false;
        return active_result(ActivationTransition::activated);
    }

    EpochStore &store;
    const EpochDefinition *active_definition;
    const ReplicaID local_replica;
    std::uint32_t active_tree_id;
    std::uint32_t rotation_ordinal;
    std::optional<EpochActivationIdentity> completed_activation;
    std::optional<EpochActivationIdentity> expected_activation;
    std::optional<EpochActivationIdentity> armed_activation;
    std::optional<CommitProof> commit_proof;
    std::optional<ActivationRecord> pending_v2_activation_record;
    std::optional<ActivationRecord> completed_v2_activation_record;
    std::optional<ConfigurationId> draining_configuration;
    ActivationBlockReason block_reason{ActivationBlockReason::none};
    bool permanent_block{false};
};

ReplicaEpochActivation::ReplicaEpochActivation(
    EpochStore &store,
    const EpochDefinition &active_epoch,
    ReplicaID local_replica,
    std::uint32_t active_tree_id,
    std::uint32_t rotation_ordinal)
    : state_(new State(
          store,
          active_epoch,
          local_replica,
          active_tree_id,
          rotation_ordinal))
{
}

ReplicaEpochActivation::~ReplicaEpochActivation() = default;

ActivationRecordResult ReplicaEpochActivation::record_committed_v2(
    const AuthorizedEpochChange &prevalidated_command,
    std::uint64_t command_commit_height)
{
    const auto reject = [this](
                            ActivationRecordDisposition disposition,
                            ActivationBlockReason reason) {
        state_->block_reason = reason;
        state_->permanent_block = true;
        return ActivationRecordResult{
            disposition, std::nullopt};
    };

    if (prevalidated_command.schema_version !=
        kEpochChangeSchemaVersionV1)
    {
        return reject(
            ActivationRecordDisposition::unsupported_schema,
            ActivationBlockReason::invalid_activation_record);
    }
    if (prevalidated_command.protocol_mode !=
        EpochProtocolMode::adaptive_v2)
    {
        return reject(
            ActivationRecordDisposition::wrong_mode,
            ActivationBlockReason::invalid_activation_record);
    }
    if (state_->active_definition->schema_version() !=
        kEpochDefinitionSchemaVersionV2)
    {
        return reject(
            ActivationRecordDisposition::wrong_mode,
            ActivationBlockReason::invalid_activation_record);
    }

    const auto &payload = prevalidated_command.payload;
    const auto payload_digest = epoch_change_payload_digest(payload);
    const auto is_same_command =
        [&payload, &payload_digest](const ActivationRecord &record) {
        return record.payload_digest == payload_digest &&
               record.successor_epoch_number ==
                   payload.successor_epoch_number &&
               record.predecessor_epoch_digest ==
                   payload.predecessor_epoch_digest &&
               record.successor_epoch_digest ==
                   payload.successor_epoch_digest &&
               record.activation_delay_blocks ==
                   payload.activation_delay_blocks;
    };
    if (state_->pending_v2_activation_record &&
        is_same_command(*state_->pending_v2_activation_record))
    {
        if (state_->block_reason ==
                ActivationBlockReason::missing_definition &&
            state_->exact_staged_v2_definition() != nullptr)
        {
            state_->block_reason = ActivationBlockReason::none;
            state_->permanent_block = false;
        }
        return {
            ActivationRecordDisposition::duplicate,
            state_->pending_v2_activation_record};
    }
    if (state_->completed_v2_activation_record &&
        is_same_command(*state_->completed_v2_activation_record))
    {
        return {
            ActivationRecordDisposition::duplicate,
            state_->completed_v2_activation_record};
    }
    if (state_->pending_v2_activation_record)
    {
        return reject(
            ActivationRecordDisposition::conflicting_record,
            ActivationBlockReason::conflicting_activation_record);
    }
    if (state_->permanent_block)
    {
        return {
            ActivationRecordDisposition::conflicting_record,
            std::nullopt};
    }

    const auto *const predecessor = state_->active_definition;
    if (payload.predecessor_epoch_digest != predecessor->epoch_digest())
    {
        return reject(
            ActivationRecordDisposition::wrong_predecessor,
            ActivationBlockReason::predecessor_digest_mismatch);
    }
    const auto successor_epoch = checked_successor_epoch(
        predecessor->epoch_number());
    if (!successor_epoch ||
        payload.successor_epoch_number != *successor_epoch)
    {
        return reject(
            ActivationRecordDisposition::wrong_successor,
            ActivationBlockReason::invalid_activation_record);
    }
    if (payload.activation_delay_blocks >
        std::numeric_limits<std::uint64_t>::max() - command_commit_height)
    {
        return reject(
            ActivationRecordDisposition::activation_height_overflow,
            ActivationBlockReason::activation_height_overflow);
    }

    const auto activation_height =
        command_commit_height + payload.activation_delay_blocks;
    const ActivationRecord record{
        predecessor->epoch_number(),
        predecessor->epoch_digest(),
        payload.successor_epoch_number,
        payload.successor_epoch_digest,
        payload_digest,
        command_commit_height,
        payload.activation_delay_blocks,
        activation_height};
    const auto *const successor = state_->store.find_epoch_by_digest(
        payload.successor_epoch_digest);
    if (successor == nullptr)
    {
        state_->pending_v2_activation_record.emplace(record);
        state_->block_reason = ActivationBlockReason::missing_definition;
        state_->permanent_block = false;
        return {
            ActivationRecordDisposition::missing_definition,
            state_->pending_v2_activation_record};
    }
    if (successor->schema_version() !=
            kEpochDefinitionSchemaVersionV2 ||
        successor->activation_height() != 0 ||
        successor->epoch_number() != payload.successor_epoch_number ||
        successor->previous_epoch_digest() !=
            payload.predecessor_epoch_digest ||
        state_->store.find_epoch(payload.successor_epoch_number) !=
            successor ||
        state_->store.find_tree(payload.successor_epoch_number, 0) ==
            nullptr)
    {
        return reject(
            ActivationRecordDisposition::mismatched_definition,
            ActivationBlockReason::invalid_activation_record);
    }

    state_->pending_v2_activation_record.emplace(record);
    return {
        ActivationRecordDisposition::recorded,
        state_->pending_v2_activation_record};
}

void ReplicaEpochActivation::fail_committed_v2(
    ActivationBlockReason reason) noexcept
{
    if (reason == ActivationBlockReason::none || state_->permanent_block ||
        state_->active_definition->schema_version() !=
            kEpochDefinitionSchemaVersionV2)
        return;
    state_->block_reason = reason;
    state_->permanent_block = true;
}

std::optional<ActivationRecord>
ReplicaEpochActivation::committed_v2_record() const
{
    if (state_->pending_v2_activation_record)
        return state_->pending_v2_activation_record;
    return state_->completed_v2_activation_record;
}

EpochActivationResult
ReplicaEpochActivation::preview_v2_post_block_commit(
    std::uint64_t height,
    const uint256_t &predecessor_digest) const
{
    return state_->preview_v2_post_block(height, predecessor_digest);
}

EpochActivationResult ReplicaEpochActivation::on_v2_post_block_commit(
    std::uint64_t height,
    const uint256_t &predecessor_digest)
{
    return state_->apply_v2_post_block(height, predecessor_digest);
}

EpochActivationResult
ReplicaEpochActivation::preview_v3_certified_activation(
    const ConfigurationId &predecessor_configuration,
    std::uint64_t predecessor_generation,
    const ConfigurationId &successor_configuration,
    std::uint64_t successor_generation) const
{
    const auto current = state_->effect();
    if (current.configuration == successor_configuration &&
        current.generation == successor_generation)
        return state_->active_result(ActivationTransition::already_active);
    const auto predecessor_packed = predecessor_generation == 0
        ? std::optional<std::uint64_t>{}
        : checked_activation_generation(
              predecessor_configuration.epoch_number,
              static_cast<std::uint32_t>(predecessor_generation - 1));
    const bool same_predecessor_epoch =
        current.configuration.epoch_number ==
            predecessor_configuration.epoch_number &&
        current.configuration.epoch_digest ==
            predecessor_configuration.epoch_digest;
    const bool exact_current_generation =
        current.generation == predecessor_generation;
    if (state_->permanent_block || !predecessor_packed ||
        *predecessor_packed != predecessor_generation ||
        !same_predecessor_epoch ||
        predecessor_generation > current.generation ||
        (exact_current_generation &&
         current.configuration != predecessor_configuration) ||
        state_->store.find_tree(
            predecessor_configuration.epoch_number,
            predecessor_configuration.tree_id) == nullptr ||
        predecessor_configuration.epoch_number ==
            std::numeric_limits<std::uint32_t>::max() ||
        successor_configuration.epoch_number !=
            predecessor_configuration.epoch_number + 1 ||
        successor_configuration.tree_id != 0 ||
        successor_configuration.epoch_digest ==
            predecessor_configuration.epoch_digest)
        return {
            ActivationTransition::blocked,
            ActivationBlockReason::conflicting_activation_record,
            std::nullopt};

    const auto canonical_successor_generation =
        checked_activation_generation(
            successor_configuration.epoch_number, 0);
    if (!canonical_successor_generation ||
        *canonical_successor_generation != successor_generation)
        return {
            ActivationTransition::blocked,
            ActivationBlockReason::invalid_activation_record,
            std::nullopt};

    const auto *const successor = state_->store.find_epoch_by_digest(
        successor_configuration.epoch_digest);
    if (successor == nullptr)
        return {
            ActivationTransition::blocked,
            ActivationBlockReason::missing_definition,
            std::nullopt};
    if (successor->schema_version() !=
            kEpochDefinitionSchemaVersionV2 ||
        successor->activation_height() != 0 ||
        successor->epoch_number() != successor_configuration.epoch_number ||
        successor->previous_epoch_digest() !=
            predecessor_configuration.epoch_digest ||
        successor->membership_digest() !=
            current.definition->membership_digest() ||
        state_->store.find_epoch(successor->epoch_number()) != successor ||
        state_->store.find_tree(successor->epoch_number(), 0) == nullptr)
        return {
            ActivationTransition::blocked,
            ActivationBlockReason::invalid_activation_record,
            std::nullopt};
    return state_->prospective_active_result(*successor);
}

EpochActivationResult ReplicaEpochActivation::apply_v3_certified_activation(
    const ConfigurationId &predecessor_configuration,
    std::uint64_t predecessor_generation,
    const ConfigurationId &successor_configuration,
    std::uint64_t successor_generation)
{
    const auto preview = preview_v3_certified_activation(
        predecessor_configuration,
        predecessor_generation,
        successor_configuration,
        successor_generation);
    if (preview.transition != ActivationTransition::activated)
    {
        if (preview.transition == ActivationTransition::blocked)
        {
            state_->block_reason = preview.blocked_reason;
            state_->permanent_block =
                preview.blocked_reason !=
                ActivationBlockReason::missing_definition;
        }
        return preview;
    }

    const auto *const successor = state_->store.find_epoch_by_digest(
        successor_configuration.epoch_digest);
    if (successor == nullptr)
    {
        state_->block_reason = ActivationBlockReason::missing_definition;
        state_->permanent_block = false;
        return state_->blocked_result();
    }
    state_->draining_configuration = state_->active_configuration();
    state_->active_definition = successor;
    state_->active_tree_id = 0;
    state_->rotation_ordinal = 0;
    state_->block_reason = ActivationBlockReason::none;
    state_->permanent_block = false;
    return state_->active_result(ActivationTransition::activated);
}

ReplicaStageResult ReplicaEpochActivation::stage(
    const StageEpochDefinition &message,
    const EpochValidationContext &validation_context)
{
    auto result = [&](ReplicaStageDisposition disposition,
                      bool acknowledge) {
        std::optional<StageAck> acknowledgement;
        if (acknowledge)
        {
            acknowledgement = StageAck{
                kEpochWireSchemaVersion,
                EpochProtocolMode::adaptive_v1,
                state_->local_replica,
                message.activation};
        }
        return ReplicaStageResult{disposition, std::move(acknowledgement)};
    };

    if (message.wire_schema_version != kEpochWireSchemaVersion)
    {
        return result(ReplicaStageDisposition::unsupported_schema, false);
    }
    if (message.protocol_mode != EpochProtocolMode::adaptive_v1)
    {
        return result(ReplicaStageDisposition::wrong_mode, false);
    }

    const auto successor = checked_successor_epoch(
        state_->active_definition->epoch_number());
    if (!successor ||
        message.activation.successor_epoch_number != *successor ||
        message.definition.epoch_number != *successor)
    {
        return result(ReplicaStageDisposition::wrong_successor, false);
    }
    if (message.activation.predecessor_epoch_number !=
            state_->active_definition->epoch_number() ||
        message.activation.predecessor_epoch_digest !=
            state_->active_definition->epoch_digest())
    {
        return result(ReplicaStageDisposition::wrong_predecessor, false);
    }
    if (!definition_matches_activation(
            message.definition, message.activation) ||
        !has_tree_zero(message.definition))
    {
        return result(ReplicaStageDisposition::divergent_definition, false);
    }
    if (state_->expected_activation &&
        *state_->expected_activation != message.activation)
    {
        return result(ReplicaStageDisposition::divergent_definition, false);
    }

    const auto *const existing = state_->store.find_epoch(*successor);
    if (existing != nullptr)
    {
        if (existing->epoch_digest() !=
                message.activation.successor_epoch_digest ||
            existing->previous_epoch_digest() !=
                message.activation.predecessor_epoch_digest ||
            existing->activation_height() !=
                message.activation.activation_height)
        {
            return result(
                ReplicaStageDisposition::divergent_definition, false);
        }
        state_->expected_activation = message.activation;
        return result(ReplicaStageDisposition::duplicate, true);
    }

    const auto &definition = state_->store.stage(
        message.definition, validation_context);
    if (definition.epoch_digest() !=
        message.activation.successor_epoch_digest)
    {
        throw std::logic_error(
            "validated staged epoch changed its canonical digest");
    }
    state_->expected_activation = message.activation;
    return result(ReplicaStageDisposition::staged, true);
}

ReplicaArmDisposition ReplicaEpochActivation::arm(
    const ArmActivation &message)
{
    if (message.wire_schema_version != kEpochWireSchemaVersion)
    {
        return ReplicaArmDisposition::unsupported_schema;
    }
    if (message.protocol_mode != EpochProtocolMode::adaptive_v1)
    {
        return ReplicaArmDisposition::wrong_mode;
    }
    if (!state_->expected_activation)
    {
        return ReplicaArmDisposition::missing_definition;
    }
    if (message.activation != *state_->expected_activation)
    {
        return ReplicaArmDisposition::wrong_activation;
    }
    if (state_->exact_staged_definition() == nullptr)
    {
        return ReplicaArmDisposition::missing_definition;
    }
    if (state_->armed_activation)
    {
        return *state_->armed_activation == message.activation
                   ? ReplicaArmDisposition::duplicate
                   : ReplicaArmDisposition::wrong_activation;
    }

    state_->armed_activation = message.activation;
    return ReplicaArmDisposition::armed;
}

bool ReplicaEpochActivation::restore_expectation(
    const ActivationStatus &status)
{
    if (status.wire_schema_version != kEpochWireSchemaVersion ||
        status.protocol_mode != EpochProtocolMode::adaptive_v1 ||
        status.replica_id != state_->local_replica ||
        status.recovery_need == ActivationRecoveryNeed::none ||
        state_->permanent_block ||
        !is_exact_successor(*state_->active_definition, status.activation))
    {
        return false;
    }
    if (state_->expected_activation &&
        *state_->expected_activation != status.activation)
    {
        return false;
    }
    if (status.recovery_need == ActivationRecoveryNeed::exact_arm)
    {
        const auto *const definition = state_->store.find_epoch(
            status.activation.successor_epoch_number);
        if (definition == nullptr ||
            definition->epoch_digest() !=
                status.activation.successor_epoch_digest)
        {
            return false;
        }
    }

    state_->expected_activation = status.activation;
    return true;
}

EpochActivationResult ReplicaEpochActivation::on_predecessor_commit(
    std::uint64_t height,
    const uint256_t &digest)
{
    if (!state_->expected_activation &&
        state_->active_is_completed_activation())
    {
        return state_->is_exact_completed_commit(height, digest)
                   ? state_->active_result(
                         ActivationTransition::already_active)
                   : state_->waiting_result();
    }
    if (state_->permanent_block)
    {
        return state_->blocked_result();
    }
    if (!state_->expected_activation)
    {
        return state_->waiting_result();
    }
    if (state_->commit_proof)
    {
        if (state_->commit_proof->height != height ||
            state_->commit_proof->digest != digest)
        {
            return state_->blocked_result();
        }
    }
    else
    {
        if (height < state_->expected_activation->activation_height)
        {
            return state_->waiting_result();
        }
        state_->commit_proof = State::CommitProof{height, digest};
    }
    return state_->evaluate_proof();
}

EpochActivationResult ReplicaEpochActivation::replay_blocked_commit()
{
    return state_->evaluate_proof();
}

EpochActivationResult ReplicaEpochActivation::preview_predecessor_commit(
    std::uint64_t height,
    const uint256_t &digest) const
{
    if (!state_->expected_activation &&
        state_->active_is_completed_activation())
    {
        return state_->is_exact_completed_commit(height, digest)
                   ? state_->active_result(
                         ActivationTransition::already_active)
                   : state_->waiting_result();
    }
    if (state_->commit_proof &&
        (state_->commit_proof->height != height ||
         state_->commit_proof->digest != digest))
    {
        return state_->blocked_result();
    }
    const auto proof = state_->commit_proof
                           ? state_->commit_proof
                           : std::optional<State::CommitProof>(
                                 State::CommitProof{height, digest});
    return state_->preview_proof(proof);
}

EpochActivationResult ReplicaEpochActivation::preview_blocked_commit() const
{
    return state_->preview_proof(state_->commit_proof);
}

EpochActivationEffect ReplicaEpochActivation::rotate_to_tree(
    std::uint32_t tree_id)
{
    if (state_->store.find_tree(
            state_->active_definition->epoch_number(), tree_id) == nullptr)
    {
        throw std::invalid_argument("epoch rotation tree does not exist");
    }
    if (state_->rotation_ordinal ==
        std::numeric_limits<std::uint32_t>::max())
    {
        throw std::overflow_error("epoch rotation ordinal is exhausted");
    }

    const auto next_ordinal = state_->rotation_ordinal + 1;
    const auto generation = checked_activation_generation(
        state_->active_definition->epoch_number(), next_ordinal);
    if (!generation)
    {
        throw std::overflow_error("epoch rotation generation overflows");
    }

    state_->draining_configuration = state_->active_configuration();
    state_->active_tree_id = tree_id;
    state_->rotation_ordinal = next_ordinal;
    return state_->effect();
}

EpochActivationEffect ReplicaEpochActivation::active_effect() const
{
    return state_->effect();
}

std::optional<ActivationStatus>
ReplicaEpochActivation::active_status() const
{
    if (state_->expected_activation || state_->permanent_block ||
        !state_->active_is_completed_activation())
        return std::nullopt;

    return ActivationStatus{
        kEpochWireSchemaVersion,
        EpochProtocolMode::adaptive_v1,
        state_->local_replica,
        *state_->completed_activation,
        ActivationRecoveryNeed::none};
}

std::optional<ActivationStatus>
ReplicaEpochActivation::recovery_status() const
{
    if (!state_->expected_activation || state_->permanent_block)
    {
        return std::nullopt;
    }

    ActivationRecoveryNeed need = ActivationRecoveryNeed::none;
    if (state_->exact_staged_definition() == nullptr)
    {
        need = ActivationRecoveryNeed::exact_definition;
    }
    else if (!state_->armed_activation ||
             *state_->armed_activation != *state_->expected_activation)
    {
        need = ActivationRecoveryNeed::exact_arm;
    }
    if (state_->block_reason == ActivationBlockReason::none ||
        need == ActivationRecoveryNeed::none)
    {
        return std::nullopt;
    }

    return ActivationStatus{
        kEpochWireSchemaVersion,
        EpochProtocolMode::adaptive_v1,
        state_->local_replica,
        *state_->expected_activation,
        need};
}

ActivationBlockReason ReplicaEpochActivation::blocked_reason() const noexcept
{
    return state_->block_reason;
}

bool ReplicaEpochActivation::admits_new_proposals() const noexcept
{
    return state_->block_reason == ActivationBlockReason::none;
}

bool ReplicaEpochActivation::may_drain_exact_context(
    const ConfigurationId &configuration) const noexcept
{
    return configuration == state_->active_configuration() ||
           (state_->draining_configuration &&
            configuration == *state_->draining_configuration);
}

namespace
{

AdaptiveV3ActivationReadinessWireError adaptive_v3_wire_error(
    ActivationReadinessWireError error) noexcept
{
    switch (error)
    {
    case ActivationReadinessWireError::none:
        return AdaptiveV3ActivationReadinessWireError::none;
    case ActivationReadinessWireError::invalid_limits:
        return AdaptiveV3ActivationReadinessWireError::invalid_limits;
    case ActivationReadinessWireError::payload_too_large:
        return AdaptiveV3ActivationReadinessWireError::payload_too_large;
    case ActivationReadinessWireError::truncated:
        return AdaptiveV3ActivationReadinessWireError::truncated;
    case ActivationReadinessWireError::trailing_bytes:
        return AdaptiveV3ActivationReadinessWireError::trailing_bytes;
    case ActivationReadinessWireError::invalid_domain:
        return AdaptiveV3ActivationReadinessWireError::invalid_domain;
    case ActivationReadinessWireError::unsupported_schema:
        return AdaptiveV3ActivationReadinessWireError::unsupported_schema;
    case ActivationReadinessWireError::noncanonical_encoding:
        return AdaptiveV3ActivationReadinessWireError::noncanonical_encoding;
    case ActivationReadinessWireError::invalid_identity:
        return AdaptiveV3ActivationReadinessWireError::invalid_identity;
    case ActivationReadinessWireError::too_many_observations:
        return AdaptiveV3ActivationReadinessWireError::too_many_members;
    case ActivationReadinessWireError::invalid_observation:
    case ActivationReadinessWireError::invalid_certificate:
    case ActivationReadinessWireError::invalid_acknowledgement:
        return AdaptiveV3ActivationReadinessWireError::noncanonical_encoding;
    case ActivationReadinessWireError::allocation_failure:
        return AdaptiveV3ActivationReadinessWireError::allocation_failure;
    case ActivationReadinessWireError::internal_failure:
        return AdaptiveV3ActivationReadinessWireError::internal_failure;
    }
    return AdaptiveV3ActivationReadinessWireError::internal_failure;
}

bool zero_digest(const uint256_t &digest) noexcept
{
    return digest == uint256_t{};
}

} // namespace

const std::string &
adaptive_v3_activation_ready_observation_domain() noexcept
{
    return activation_ready_observation_domain();
}

const std::string &
adaptive_v3_activation_readiness_certificate_domain() noexcept
{
    return activation_readiness_certificate_domain();
}

bytearray_t encode_adaptive_v3_activation_ready_observation(
    const AdaptiveV3ActivationReadyObservation &observation,
    const AdaptiveV3ActivationReadinessLimits &limits)
{
    return encode_activation_ready_observation(observation, limits);
}

AdaptiveV3ActivationReadyObservationDecodeResult
decode_adaptive_v3_activation_ready_observation(
    const bytearray_t &payload,
    const AdaptiveV3ActivationReadinessLimits &limits) noexcept
{
    auto decoded = decode_activation_ready_observation(payload, limits);
    return {
        adaptive_v3_wire_error(decoded.error),
        std::move(decoded.value)};
}

bytearray_t encode_adaptive_v3_activation_readiness_certificate(
    const AdaptiveV3ActivationReadinessCertificate &certificate,
    const AdaptiveV3ActivationReadinessLimits &limits)
{
    return encode_activation_readiness_certificate(certificate, limits);
}

AdaptiveV3ActivationReadinessCertificateDecodeResult
decode_adaptive_v3_activation_readiness_certificate(
    const bytearray_t &payload,
    const AdaptiveV3ActivationReadinessLimits &limits) noexcept
{
    auto decoded = decode_activation_readiness_certificate(payload, limits);
    return {
        adaptive_v3_wire_error(decoded.error),
        std::move(decoded.value)};
}

AdaptiveV3ActivationReadinessCertificate
make_adaptive_v3_activation_readiness_certificate(
    const AdaptiveV3ActivationReadyIdentity &identity,
    std::vector<AdaptiveV3ActivationReadyObservation> observations,
    const AdaptiveV3ActivationReadinessLimits &limits)
{
    if (limits.maximum_payload_bytes == 0 || limits.maximum_members == 0 ||
        observations.size() > limits.maximum_members)
        throw std::invalid_argument(
            "adaptive-v3 readiness certificate exceeds fixed limits");
    auto certificate = make_activation_readiness_certificate(
        identity, std::move(observations));
    static_cast<void>(
        encode_activation_readiness_certificate(certificate, limits));
    return certificate;
}

struct AdaptiveV3CertifiedActivationGate::State
{
    State(
        AdaptiveV3ActivationSchedule schedule_value,
        ReplicaID replica,
        std::shared_ptr<const PrivKeyBLS> private_key,
        std::vector<AdaptiveV3ReadinessMember> membership,
        std::size_t supplied_quorum)
        : schedule(std::move(schedule_value)),
          local_replica(replica),
          local_private_key(std::move(private_key)),
          active_configuration_value{
              schedule.predecessor_epoch_number,
              0,
              schedule.predecessor_epoch_digest},
          scheduled_height(schedule.activation_height)
    {
        if (local_private_key == nullptr || membership.empty() ||
            zero_digest(schedule.membership_digest) ||
            zero_digest(schedule.predecessor_epoch_digest) ||
            zero_digest(schedule.successor_epoch_digest) ||
            zero_digest(schedule.command_payload_digest) ||
            zero_digest(schedule.command_block_hash) ||
            schedule.predecessor_epoch_number ==
                std::numeric_limits<std::uint32_t>::max() ||
            schedule.successor_epoch_number !=
                schedule.predecessor_epoch_number + 1 ||
            schedule.predecessor_epoch_digest ==
                schedule.successor_epoch_digest ||
            schedule.command_block_height == 0 ||
            schedule.activation_delay_blocks == 0 ||
            schedule.command_block_height >
                std::numeric_limits<std::uint64_t>::max() -
                    schedule.activation_delay_blocks ||
            schedule.activation_height !=
                schedule.command_block_height +
                    schedule.activation_delay_blocks)
            throw std::invalid_argument(
                "adaptive-v3 activation schedule is invalid");

        const auto successor_generation = checked_activation_generation(
            schedule.successor_epoch_number, 0);
        if (!successor_generation ||
            *successor_generation !=
                schedule.successor_activation_generation)
            throw std::invalid_argument(
                "adaptive-v3 successor generation is not canonical");

        const auto member_count = membership.size();
        if (member_count < 4 || (member_count - 1) % 3 != 0)
            throw std::invalid_argument(
                "adaptive-v3 membership is not an exact 3f+1 set");
        const auto derived_quorum = 2 * ((member_count - 1) / 3) + 1;
        if (supplied_quorum != derived_quorum)
            throw std::invalid_argument(
                "adaptive-v3 supplied quorum differs from fixed quorum");
        quorum = derived_quorum;

        std::vector<ReplicaID> replica_ids;
        replica_ids.reserve(member_count);
        public_members.reserve(member_count);
        for (const auto &member : membership)
        {
            if (!replica_ids.empty() &&
                replica_ids.back() >= member.replica_id)
                throw std::invalid_argument(
                    "adaptive-v3 membership is not canonical");
            replica_ids.push_back(member.replica_id);
            public_members.emplace_back(
                member.replica_id, member.public_key);
        }
        if (canonical_membership_digest(replica_ids) !=
            schedule.membership_digest)
            throw std::invalid_argument(
                "adaptive-v3 schedule membership digest mismatch");
        readiness_membership_digest =
            canonical_activation_readiness_membership_digest(
                public_members);

        const auto local = std::lower_bound(
            public_members.begin(), public_members.end(), local_replica,
            [](const auto &member, ReplicaID id) {
                return member.first < id;
            });
        if (local == public_members.end() ||
            local->first != local_replica ||
            local->second.to_bytes() !=
                PubKeyBLS(*local_private_key).to_bytes())
            throw std::invalid_argument(
                "adaptive-v3 local signing key is not in membership");

        const auto predecessor_generation = checked_activation_generation(
            schedule.predecessor_epoch_number, 0);
        if (!predecessor_generation)
            throw std::overflow_error(
                "adaptive-v3 predecessor generation overflows");
        active_generation_value = *predecessor_generation;
    }

    bool identity_matches_schedule(
        const AdaptiveV3ActivationReadyIdentity &identity) const noexcept
    {
        if (identity.schema_version !=
                kAdaptiveV3ActivationReadinessSchemaVersionV1 ||
            identity.membership_digest != readiness_membership_digest ||
            identity.predecessor_boundary_configuration.epoch_number !=
                schedule.predecessor_epoch_number ||
            identity.predecessor_boundary_configuration.epoch_digest !=
                schedule.predecessor_epoch_digest ||
            identity.successor_configuration != ConfigurationId{
                schedule.successor_epoch_number,
                0,
                schedule.successor_epoch_digest} ||
            identity.successor_activation_generation !=
                schedule.successor_activation_generation ||
            identity.command_payload_digest !=
                schedule.command_payload_digest ||
            identity.command_block_height != schedule.command_block_height ||
            identity.command_block_hash != schedule.command_block_hash ||
            identity.activation_delay_blocks !=
                schedule.activation_delay_blocks ||
            identity.activation_height != schedule.activation_height ||
            zero_digest(identity.activation_boundary_block_hash))
            return false;

        if (identity.predecessor_boundary_generation == 0)
            return false;
        const auto packed = identity.predecessor_boundary_generation - 1;
        return static_cast<std::uint32_t>(packed >> 32) ==
                   schedule.predecessor_epoch_number;
    }

    bool matches_latched_predecessor(
        const ConfigurationId &configuration,
        std::uint64_t generation) const noexcept
    {
        return !predecessor_latched ||
               (latched_predecessor_configuration.has_value() &&
                configuration ==
                    *latched_predecessor_configuration &&
                latched_predecessor_generation.has_value() &&
                generation == *latched_predecessor_generation);
    }

    void latch_predecessor(
        const ConfigurationId &configuration,
        std::uint64_t generation) noexcept
    {
        active_configuration_value = configuration;
        active_generation_value = generation;
        latched_predecessor_configuration = configuration;
        latched_predecessor_generation = generation;
        predecessor_latched = true;
    }

    void activate(std::uint64_t committed_height) noexcept
    {
        active_configuration_value = {
            schedule.successor_epoch_number,
            0,
            schedule.successor_epoch_digest};
        active_generation_value = schedule.successor_activation_generation;
        activation_state = AdaptiveV3CertifiedActivationState::active;
        applied_height = committed_height;
    }

    AdaptiveV3ActivationSchedule schedule;
    ReplicaID local_replica;
    std::shared_ptr<const PrivKeyBLS> local_private_key;
    std::vector<std::pair<ReplicaID, PubKeyBLS>> public_members;
    uint256_t readiness_membership_digest;
    std::size_t quorum{0};
    AdaptiveV3CertifiedActivationState activation_state{
        AdaptiveV3CertifiedActivationState::awaiting_boundary};
    ConfigurationId active_configuration_value;
    std::uint64_t active_generation_value{0};
    bool predecessor_latched{false};
    std::optional<ConfigurationId> latched_predecessor_configuration;
    std::optional<std::uint64_t> latched_predecessor_generation;
    bool fence_engaged{false};
    std::optional<std::uint64_t> scheduled_height;
    std::optional<std::uint64_t> latest_predecessor_commit_height;
    std::optional<std::uint64_t> applied_height;
    std::optional<AdaptiveV3ActivationReadyIdentity> prepared_identity;
    std::optional<AdaptiveV3ActivationReadinessCertificate>
        buffered_certificate;
    std::optional<uint256_t> accepted_certificate_digest;
};

AdaptiveV3CertifiedActivationGate::AdaptiveV3CertifiedActivationGate(
    AdaptiveV3ActivationSchedule schedule,
    ReplicaID local_replica,
    std::shared_ptr<const PrivKeyBLS> local_private_key,
    std::vector<AdaptiveV3ReadinessMember> membership,
    std::size_t fixed_quorum)
    : state_(new State(
          std::move(schedule),
          local_replica,
          std::move(local_private_key),
          std::move(membership),
          fixed_quorum))
{}

AdaptiveV3CertifiedActivationGate::~AdaptiveV3CertifiedActivationGate() =
    default;

AdaptiveV3BoundaryResult
AdaptiveV3CertifiedActivationGate::observe_predecessor_commit(
    std::uint64_t committed_height,
    const ConfigurationId &configuration,
    std::uint64_t generation,
    const uint256_t &block_hash,
    std::uint64_t source_sequence,
    std::uint64_t monotonic_raw_ns) noexcept
{
    try
    {
        if (state_->activation_state ==
            AdaptiveV3CertifiedActivationState::blocked)
            return {AdaptiveV3BoundaryDisposition::rejected, std::nullopt};
        if (configuration.epoch_number !=
                state_->schedule.predecessor_epoch_number ||
            configuration.epoch_digest !=
                state_->schedule.predecessor_epoch_digest ||
            generation == 0 || zero_digest(block_hash))
            return {AdaptiveV3BoundaryDisposition::rejected, std::nullopt};
        const auto packed_generation = generation - 1;
        if (static_cast<std::uint32_t>(packed_generation >> 32) !=
            configuration.epoch_number)
            return {AdaptiveV3BoundaryDisposition::rejected, std::nullopt};

        if (state_->activation_state ==
            AdaptiveV3CertifiedActivationState::active)
            return {
                AdaptiveV3BoundaryDisposition::already_prepared,
                std::nullopt};

        if (state_->activation_state ==
            AdaptiveV3CertifiedActivationState::prepared)
        {
            if (committed_height >= state_->schedule.activation_height)
                state_->latest_predecessor_commit_height = committed_height;
            return {
                AdaptiveV3BoundaryDisposition::already_prepared,
                std::nullopt};
        }

        if (committed_height < state_->schedule.activation_height)
        {
            if (!state_->matches_latched_predecessor(
                    configuration, generation))
                return {
                    AdaptiveV3BoundaryDisposition::rejected,
                    std::nullopt};
            state_->latch_predecessor(configuration, generation);
            state_->latest_predecessor_commit_height = committed_height;
            return {AdaptiveV3BoundaryDisposition::waiting, std::nullopt};
        }
        if (committed_height != state_->schedule.activation_height ||
            source_sequence == 0)
            return {AdaptiveV3BoundaryDisposition::rejected, std::nullopt};

        // The exact committed boundary proposal may belong to an older
        // predecessor tree/generation that was already in the pipeline when
        // the transition command committed.  Bind the signed identity to
        // that proposal, while retaining the locally active predecessor
        // solely for the pre-fence vote-authority check.
        if (!state_->predecessor_latched)
            state_->latch_predecessor(configuration, generation);
        state_->latest_predecessor_commit_height = committed_height;

        AdaptiveV3ActivationReadyIdentity identity;
        identity.membership_digest = state_->readiness_membership_digest;
        identity.predecessor_boundary_configuration = configuration;
        identity.predecessor_boundary_generation = generation;
        identity.successor_configuration = {
            state_->schedule.successor_epoch_number,
            0,
            state_->schedule.successor_epoch_digest};
        identity.successor_activation_generation =
            state_->schedule.successor_activation_generation;
        identity.command_payload_digest =
            state_->schedule.command_payload_digest;
        identity.command_block_height =
            state_->schedule.command_block_height;
        identity.command_block_hash = state_->schedule.command_block_hash;
        identity.activation_delay_blocks =
            state_->schedule.activation_delay_blocks;
        identity.activation_height = state_->schedule.activation_height;
        identity.activation_boundary_block_hash = block_hash;

        // Fence publication precedes the signing call.  If signing fails the
        // replica remains safely fenced and terminally blocked.
        state_->fence_engaged = true;
        state_->activation_state =
            AdaptiveV3CertifiedActivationState::prepared;
        state_->prepared_identity = identity;
        auto observation = sign_activation_ready_observation(
            identity,
            state_->local_replica,
            source_sequence,
            monotonic_raw_ns,
            *state_->local_private_key);

        if (state_->buffered_certificate.has_value() &&
            state_->buffered_certificate->identity == identity)
        {
            state_->accepted_certificate_digest =
                state_->buffered_certificate->certificate_digest;
            state_->activate(committed_height);
            state_->buffered_certificate.reset();
            return {
                AdaptiveV3BoundaryDisposition::
                    activated_from_buffered_certificate,
                std::move(observation)};
        }
        state_->buffered_certificate.reset();
        return {
            AdaptiveV3BoundaryDisposition::prepared,
            std::move(observation)};
    }
    catch (...)
    {
        if (state_->fence_engaged)
            state_->activation_state =
                AdaptiveV3CertifiedActivationState::blocked;
        return {AdaptiveV3BoundaryDisposition::rejected, std::nullopt};
    }
}

AdaptiveV3CertificateDisposition
AdaptiveV3CertifiedActivationGate::observe_certificate(
    const AdaptiveV3ActivationReadinessCertificate &certificate) noexcept
{
    try
    {
        if (state_->activation_state ==
            AdaptiveV3CertifiedActivationState::blocked)
            return AdaptiveV3CertificateDisposition::terminal;
        const bool already_active = state_->activation_state ==
            AdaptiveV3CertifiedActivationState::active;

        if (certificate.schema_version !=
                kAdaptiveV3ActivationReadinessSchemaVersionV1 ||
            certificate.observations.empty() ||
            certificate.observations.size() > state_->public_members.size())
            return AdaptiveV3CertificateDisposition::rejected_noncanonical;
        for (std::size_t index = 1;
             index < certificate.observations.size(); ++index)
            if (certificate.observations[index - 1].signer_replica_id >=
                certificate.observations[index].signer_replica_id)
                return AdaptiveV3CertificateDisposition::
                    rejected_noncanonical;

        if (certificate.identity.predecessor_boundary_configuration
                    .epoch_number <
                state_->schedule.predecessor_epoch_number ||
            certificate.identity.successor_configuration.epoch_number <
                state_->schedule.successor_epoch_number)
            return AdaptiveV3CertificateDisposition::rejected_stale;
        if (!state_->identity_matches_schedule(certificate.identity))
            return AdaptiveV3CertificateDisposition::rejected_wrong_identity;
        for (const auto &observation : certificate.observations)
            if (observation.identity != certificate.identity)
                return AdaptiveV3CertificateDisposition::
                    rejected_mixed_identity;
        if (state_->prepared_identity.has_value() &&
            certificate.identity != *state_->prepared_identity)
            return AdaptiveV3CertificateDisposition::rejected_wrong_identity;
        if (certificate.observations.size() < state_->quorum)
            return AdaptiveV3CertificateDisposition::rejected_below_quorum;

        for (const auto &observation : certificate.observations)
        {
            const auto member = std::lower_bound(
                state_->public_members.begin(),
                state_->public_members.end(),
                observation.signer_replica_id,
                [](const auto &entry, ReplicaID id) {
                    return entry.first < id;
                });
            if (member == state_->public_members.end() ||
                member->first != observation.signer_replica_id)
                return AdaptiveV3CertificateDisposition::rejected_nonmember;
            if (!verify_activation_ready_observation(
                    observation, member->second))
                return AdaptiveV3CertificateDisposition::
                    rejected_invalid_signature;
        }

        if (!verify_activation_readiness_certificate(
                certificate,
                certificate.identity,
                state_->readiness_membership_digest,
                state_->public_members))
            return AdaptiveV3CertificateDisposition::rejected_noncanonical;

        if (already_active)
            return state_->accepted_certificate_digest.has_value() &&
                           *state_->accepted_certificate_digest ==
                               certificate.certificate_digest &&
                           state_->prepared_identity.has_value() &&
                           *state_->prepared_identity == certificate.identity
                       ? AdaptiveV3CertificateDisposition::duplicate
                       : AdaptiveV3CertificateDisposition::terminal;

        if (state_->activation_state ==
            AdaptiveV3CertifiedActivationState::awaiting_boundary)
        {
            state_->buffered_certificate = certificate;
            return AdaptiveV3CertificateDisposition::buffered_early;
        }

        const auto applied = state_->latest_predecessor_commit_height
                                 .value_or(state_->schedule.activation_height);
        state_->accepted_certificate_digest = certificate.certificate_digest;
        state_->activate(applied);
        return AdaptiveV3CertificateDisposition::accepted;
    }
    catch (...)
    {
        return AdaptiveV3CertificateDisposition::rejected_noncanonical;
    }
}

AdaptiveV3CertifiedActivationState
AdaptiveV3CertifiedActivationGate::state() const noexcept
{
    return state_->activation_state;
}

const ConfigurationId &
AdaptiveV3CertifiedActivationGate::active_configuration() const noexcept
{
    return state_->active_configuration_value;
}

std::uint64_t
AdaptiveV3CertifiedActivationGate::active_generation() const noexcept
{
    return state_->active_generation_value;
}

bool AdaptiveV3CertifiedActivationGate::vote_fence_engaged() const noexcept
{
    return state_->fence_engaged;
}

bool AdaptiveV3CertifiedActivationGate::may_authorize_vote(
    const ConfigurationId &configuration,
    std::uint64_t generation) const noexcept
{
    if (state_->activation_state ==
        AdaptiveV3CertifiedActivationState::prepared ||
        state_->activation_state ==
            AdaptiveV3CertifiedActivationState::blocked)
        return false;
    return configuration == state_->active_configuration_value &&
           generation == state_->active_generation_value;
}

std::optional<std::uint64_t>
AdaptiveV3CertifiedActivationGate::scheduled_readiness_height()
    const noexcept
{
    return state_->scheduled_height;
}

std::optional<std::uint64_t>
AdaptiveV3CertifiedActivationGate::certificate_apply_committed_height()
    const noexcept
{
    return state_->applied_height;
}

} // namespace hotstuff
