#include "hotstuff/epoch_runtime.h"

#include <algorithm>
#include <limits>
#include <map>
#include <stdexcept>
#include <utility>

#include "hotstuff/hotstuff.h"

namespace hotstuff
{

AdaptiveV2CommitCadence::AdaptiveV2CommitCadence(std::size_t period)
    : period_(period)
{
    if (period_ == 0)
        throw std::invalid_argument(
            "adaptive-v2 commit rotation period must be positive");
}

bool AdaptiveV2CommitCadence::observe(
    const std::optional<ProposalKey> &committed_key,
    const ConfigurationId &active_configuration) noexcept
{
    if (!committed_key.has_value() ||
        committed_key->configuration != active_configuration)
        return false;

    if (observed_commits_ < period_)
        ++observed_commits_;
    return observed_commits_ == period_;
}

void AdaptiveV2CommitCadence::reset() noexcept
{
    observed_commits_ = 0;
}

std::size_t AdaptiveV2CommitCadence::period() const noexcept
{
    return period_;
}

std::size_t AdaptiveV2CommitCadence::observed_commits() const noexcept
{
    return observed_commits_;
}

AdaptiveV2RotationCoordinator::AdaptiveV2RotationCoordinator(
    std::size_t period,
    AdaptiveV2RotationEffects &effects)
    : cadence_(period), effects_(effects)
{}

bool AdaptiveV2RotationCoordinator::matches_expected_view(
    const ConfigurationId &expected_configuration,
    std::uint64_t expected_generation) const noexcept
{
    const auto active = effects_.active_view();
    return active.has_value() &&
           active->configuration == expected_configuration &&
           active->generation == expected_generation;
}

AdaptiveV2RotationResult
AdaptiveV2RotationCoordinator::compare_and_rotate(
    const ConfigurationId &expected_configuration,
    std::uint64_t expected_generation) noexcept
{
    if (!matches_expected_view(
            expected_configuration, expected_generation))
        return {AdaptiveV2RotationDisposition::stale_view, std::nullopt};

    const auto next_tree = effects_.next_tree_id();
    if (!next_tree.has_value() ||
        !matches_expected_view(
            expected_configuration, expected_generation))
        return {
            next_tree.has_value()
                ? AdaptiveV2RotationDisposition::stale_view
                : AdaptiveV2RotationDisposition::rejected,
            std::nullopt};

    const auto rotation = effects_.rotate_to_tree(*next_tree);
    if (rotation.error != EpochIngressError::none ||
        !rotation.update.has_value())
        return {AdaptiveV2RotationDisposition::rejected, std::nullopt};

    cadence_.reset();
    return {
        AdaptiveV2RotationDisposition::rotated,
        std::move(rotation.update)};
}

AdaptiveV2RotationResult AdaptiveV2RotationCoordinator::on_commit(
    const std::optional<ProposalKey> &committed_key,
    const ConfigurationId &expected_configuration,
    std::uint64_t expected_generation) noexcept
{
    try
    {
        const std::lock_guard<std::mutex> lock(mutex_);
        if (!matches_expected_view(
                expected_configuration, expected_generation))
            return {
                AdaptiveV2RotationDisposition::stale_view,
                std::nullopt};
        if (!cadence_.observe(
                committed_key, expected_configuration))
            return {
                AdaptiveV2RotationDisposition::not_due,
                std::nullopt};
        return compare_and_rotate(
            expected_configuration, expected_generation);
    }
    catch (...)
    {
        return {AdaptiveV2RotationDisposition::rejected, std::nullopt};
    }
}

AdaptiveV2RotationResult AdaptiveV2RotationCoordinator::on_timeout(
    const ConfigurationId &expected_configuration,
    std::uint64_t expected_generation) noexcept
{
    try
    {
        const std::lock_guard<std::mutex> lock(mutex_);
        return compare_and_rotate(
            expected_configuration, expected_generation);
    }
    catch (...)
    {
        return {AdaptiveV2RotationDisposition::rejected, std::nullopt};
    }
}

void AdaptiveV2RotationCoordinator::reset_for_activation() noexcept
{
    try
    {
        const std::lock_guard<std::mutex> lock(mutex_);
        cadence_.reset();
    }
    catch (...)
    {}
}

std::size_t AdaptiveV2RotationCoordinator::period() const noexcept
{
    try
    {
        const std::lock_guard<std::mutex> lock(mutex_);
        return cadence_.period();
    }
    catch (...)
    {
        return 0;
    }
}

std::size_t AdaptiveV2RotationCoordinator::observed_commits() const noexcept
{
    try
    {
        const std::lock_guard<std::mutex> lock(mutex_);
        return cadence_.observed_commits();
    }
    catch (...)
    {
        return 0;
    }
}

namespace
{

bool valid_limits(const EpochWireLimits &limits) noexcept
{
    return limits.maximum_payload_bytes != 0 &&
           limits.maximum_trees != 0 &&
           limits.maximum_members_per_tree != 0 &&
           limits.maximum_string_bytes != 0;
}

bool authenticated_manager(const AuthenticatedEpochPeer &peer) noexcept
{
    return peer.role == EpochPeerRole::manager;
}

bool authenticated_replica(const AuthenticatedEpochPeer &peer) noexcept
{
    return peer.role == EpochPeerRole::replica && peer.replica_id.has_value();
}

template <typename Message>
EpochWireError preflight_control(
    const Message &message,
    const EpochWireLimits &limits) noexcept
{
    if (!valid_limits(limits))
        return EpochWireError::invalid_limits;
    if (message.serialized.size() > limits.maximum_payload_bytes)
        return EpochWireError::payload_too_large;
    return EpochWireError::none;
}

template <typename Message>
EpochConsensusWireError preflight_consensus(
    const Message &message,
    const EpochWireLimits &limits) noexcept
{
    if (!valid_limits(limits))
        return EpochConsensusWireError::invalid_limits;
    if (message.serialized.size() > limits.maximum_payload_bytes)
        return EpochConsensusWireError::payload_too_large;
    return EpochConsensusWireError::none;
}

ReplicaStageIngressResult rejected_stage(
    EpochIngressError error,
    EpochWireError wire_error = EpochWireError::none)
{
    return {error, wire_error, std::nullopt, std::nullopt};
}

ReplicaArmIngressResult rejected_arm(
    EpochIngressError error,
    EpochWireError wire_error = EpochWireError::none)
{
    return {error, wire_error, std::nullopt};
}

EpochConsensusIngressResult rejected_consensus(
    EpochIngressError error,
    EpochConsensusWireError wire_error = EpochConsensusWireError::none)
{
    return {
        error,
        wire_error,
        EpochConsensusPermission::rejected_identity,
        std::nullopt,
        std::nullopt};
}

const EpochTreeDefinition *find_tree(
    const EpochActivationEffect &effect,
    std::uint32_t tree_id) noexcept
{
    if (effect.definition == nullptr)
        return nullptr;
    for (const auto &tree : effect.definition->trees())
        if (tree.tree_id == tree_id)
            return &tree;
    return nullptr;
}

EpochRuntimeUpdate runtime_update(const EpochActivationEffect &effect)
{
    const auto *const tree = find_tree(effect, effect.configuration.tree_id);
    if (tree == nullptr || tree->members_breadth_first.empty())
        throw std::logic_error("active epoch tree has no prepared leader");
    return {
        effect,
        LeaderViewId{
            effect.configuration,
            effect.generation,
            tree->members_breadth_first.front()}};
}

bool permanent_block(ActivationBlockReason reason) noexcept
{
    return reason == ActivationBlockReason::predecessor_digest_mismatch ||
           reason == ActivationBlockReason::missed_activation_height;
}

class CandidatePreparation final
{
public:
    CandidatePreparation(
        EpochRuntimeTransaction &transaction,
        PreparedEpochRuntime prepared) noexcept
        : transaction_(&transaction), prepared_(std::move(prepared))
    {}

    ~CandidatePreparation()
    {
        if (prepared_)
            transaction_->discard(std::move(*prepared_));
    }

    PreparedEpochRuntime release() noexcept
    {
        auto prepared = std::move(*prepared_);
        prepared_.reset();
        return prepared;
    }

    CandidatePreparation(const CandidatePreparation &) = delete;
    CandidatePreparation &operator=(const CandidatePreparation &) = delete;

private:
    EpochRuntimeTransaction *transaction_;
    std::optional<PreparedEpochRuntime> prepared_;
};

class FutureProposalClaimOwner final
{
public:
    FutureProposalClaimOwner(
        RetryableFutureProposalStore &store,
        std::uint64_t token) noexcept
        : store_(&store), token_(token)
    {}

    ~FutureProposalClaimOwner()
    {
        if (store_ != nullptr)
            store_->release(token_);
    }

    void acknowledge() noexcept
    {
        store_->acknowledge(token_);
        store_ = nullptr;
    }

    void release_ownership() noexcept
    {
        store_ = nullptr;
    }

    FutureProposalClaimOwner(const FutureProposalClaimOwner &) = delete;
    FutureProposalClaimOwner &operator=(
        const FutureProposalClaimOwner &) = delete;

private:
    RetryableFutureProposalStore *store_;
    std::uint64_t token_;
};

EpochRuntimePlan make_runtime_plan(
    EpochProtocolMode protocol_mode,
    std::uint32_t epoch_number,
    const uint256_t &epoch_digest,
    const std::vector<EpochTreeDefinition> &trees,
    bytearray_t canonical_stage)
{
    if (protocol_mode != EpochProtocolMode::adaptive_v1 &&
        protocol_mode != EpochProtocolMode::adaptive_v2)
        throw std::invalid_argument(
            "runtime plan requires an adaptive protocol mode");
    if (epoch_digest == uint256_t{} || canonical_stage.empty())
        throw std::invalid_argument(
            "runtime plan requires an exact epoch identity");

    EpochRuntimePlan plan;
    plan.protocol_mode = protocol_mode;
    plan.epoch_number = epoch_number;
    plan.epoch_digest = epoch_digest;
    plan.canonical_stage = std::move(canonical_stage);
    plan.canonical_digest = DataStream(plan.canonical_stage).get_hash();
    const auto generation = checked_activation_generation(
        epoch_number, 0);
    if (!generation)
        throw std::overflow_error("epoch activation generation overflows");
    plan.trees.reserve(trees.size());
    for (const auto &tree : trees)
    {
        if (tree.members_breadth_first.empty())
            throw std::invalid_argument("epoch runtime tree is empty");
        plan.trees.push_back(EpochTreeRuntimeInput{
            ConfigurationId{
                epoch_number,
                tree.tree_id,
                epoch_digest},
            tree,
            tree.members_breadth_first.front(),
            *generation});
    }
    return plan;
}

EpochRuntimePlan make_runtime_plan(
    StageEpochDefinition stage,
    bytearray_t canonical_stage)
{
    auto plan = make_runtime_plan(
        stage.protocol_mode,
        stage.activation.successor_epoch_number,
        stage.activation.successor_epoch_digest,
        stage.definition.trees,
        std::move(canonical_stage));
    plan.stage = std::move(stage);
    return plan;
}

EpochRuntimePlan make_runtime_plan(const EpochDefinition &definition)
{
    if (definition.schema_version() !=
            kEpochDefinitionSchemaVersionV2 ||
        definition.activation_height() != 0)
        throw std::invalid_argument(
            "committed v2 runtime requires a schedule-free definition");

    auto plan = make_runtime_plan(
        EpochProtocolMode::adaptive_v2,
        definition.epoch_number(),
        definition.epoch_digest(),
        definition.trees(),
        definition.canonical_serialization());
    if (plan.canonical_digest != definition.epoch_digest())
        throw std::logic_error(
            "stored v2 definition digest is not canonical");
    return plan;
}

template <typename Message>
EpochConsensusWireDecodeResult decode_message(
    const Message &message,
    EpochConsensusWireKind kind,
    EpochProtocolMode mode,
    const EpochWireLimits &limits)
{
    const auto preflight = preflight_consensus(message, limits);
    if (preflight != EpochConsensusWireError::none)
        return {preflight, std::nullopt};
    return decode_epoch_consensus_envelope(
        static_cast<bytearray_t>(message.serialized),
        kind,
        mode,
        limits);
}

EpochBufferedProposalIdentity proposal_identity(
    const BufferedProposal &proposal)
{
    return {
        proposal.metadata.key(),
        proposal.view_generation,
        proposal.wire_digest};
}

} // namespace

AuthenticatedEpochPeer AuthenticatedEpochPeer::manager() noexcept
{
    return {EpochPeerRole::manager, std::nullopt, PeerId{}};
}

AuthenticatedEpochPeer AuthenticatedEpochPeer::replica(
    ReplicaID replica_id) noexcept
{
    return {EpochPeerRole::replica, replica_id, PeerId{}};
}

ProposalKey EpochConsensusEnvelope::key() const
{
    return {configuration, block_hash};
}

bytearray_t encode_epoch_consensus_envelope(
    const EpochConsensusEnvelope &envelope,
    const EpochWireLimits &limits)
{
    if (!valid_limits(limits))
        throw std::invalid_argument("epoch consensus wire limits are invalid");
    if (envelope.protocol_mode != EpochProtocolMode::adaptive_v1 &&
        envelope.protocol_mode != EpochProtocolMode::adaptive_v2)
        throw std::invalid_argument(
            "epoch consensus wire requires an adaptive protocol mode");
    if (envelope.body.size() >
        std::numeric_limits<std::uint32_t>::max())
        throw std::length_error("epoch consensus body is too large");

    constexpr std::size_t fixed_size =
        sizeof(std::uint32_t) + sizeof(std::uint8_t) * 2 +
        sizeof(std::uint32_t) * 2 + 32 + sizeof(std::uint64_t) + 32 +
        sizeof(ReplicaID) * 2 + sizeof(std::uint32_t);
    if (envelope.body.size() > limits.maximum_payload_bytes ||
        fixed_size > limits.maximum_payload_bytes - envelope.body.size())
        throw std::length_error("epoch consensus payload exceeds limit");

    DataStream stream;
    stream << htole(envelope.wire_schema_version)
           << static_cast<std::uint8_t>(envelope.protocol_mode)
           << static_cast<std::uint8_t>(envelope.kind)
           << htole(envelope.configuration.epoch_number)
           << htole(envelope.configuration.tree_id)
           << envelope.configuration.epoch_digest
           << htole(envelope.view_generation)
           << envelope.block_hash
           << htole(envelope.originator)
           << htole(envelope.proposer)
           << htole(static_cast<std::uint32_t>(envelope.body.size()))
           << envelope.body;
    return static_cast<bytearray_t>(std::move(stream));
}

EpochConsensusWireDecodeResult decode_epoch_consensus_envelope(
    const bytearray_t &payload,
    EpochConsensusWireKind expected_kind,
    EpochProtocolMode expected_mode,
    const EpochWireLimits &limits) noexcept
{
    if (!valid_limits(limits))
        return {EpochConsensusWireError::invalid_limits, std::nullopt};
    if (payload.size() > limits.maximum_payload_bytes)
        return {EpochConsensusWireError::payload_too_large, std::nullopt};

    try
    {
        DataStream stream(payload);
        EpochConsensusEnvelope envelope;
        std::uint32_t schema = 0;
        std::uint8_t mode = 0;
        std::uint8_t kind = 0;
        stream >> schema;
        schema = letoh(schema);
        if (schema != kEpochConsensusWireSchemaVersion)
            return {
                EpochConsensusWireError::unsupported_schema,
                std::nullopt};
        stream >> mode;
        if ((expected_mode != EpochProtocolMode::adaptive_v1 &&
             expected_mode != EpochProtocolMode::adaptive_v2) ||
            mode != static_cast<std::uint8_t>(expected_mode))
            return {EpochConsensusWireError::mode_mismatch, std::nullopt};
        stream >> kind;
        if (kind != static_cast<std::uint8_t>(expected_kind))
            return {EpochConsensusWireError::unexpected_kind, std::nullopt};

        envelope.wire_schema_version = schema;
        envelope.protocol_mode = static_cast<EpochProtocolMode>(mode);
        envelope.kind = static_cast<EpochConsensusWireKind>(kind);
        stream >> envelope.configuration.epoch_number
               >> envelope.configuration.tree_id
               >> envelope.configuration.epoch_digest
               >> envelope.view_generation
               >> envelope.block_hash
               >> envelope.originator
               >> envelope.proposer;
        envelope.configuration.epoch_number =
            letoh(envelope.configuration.epoch_number);
        envelope.configuration.tree_id =
            letoh(envelope.configuration.tree_id);
        envelope.view_generation = letoh(envelope.view_generation);
        envelope.originator = letoh(envelope.originator);
        envelope.proposer = letoh(envelope.proposer);
        if (envelope.view_generation == 0)
            return {
                EpochConsensusWireError::invalid_generation,
                std::nullopt};

        std::uint32_t body_size = 0;
        stream >> body_size;
        body_size = letoh(body_size);
        if (body_size > stream.size())
            return {EpochConsensusWireError::truncated, std::nullopt};
        const auto *const body = stream.get_data_inplace(body_size);
        envelope.body.assign(body, body + body_size);
        if (stream.size() != 0)
            return {
                EpochConsensusWireError::trailing_bytes,
                std::nullopt};
        return {EpochConsensusWireError::none, std::move(envelope)};
    }
    catch (const std::bad_alloc &)
    {
        return {
            EpochConsensusWireError::payload_too_large,
            std::nullopt};
    }
    catch (...)
    {
        return {EpochConsensusWireError::truncated, std::nullopt};
    }
}

std::optional<std::uint64_t> buffered_proposal_view_generation(
    const BufferedProposal &proposal) noexcept
{
    if (proposal.view_generation == 0)
        return std::nullopt;
    return proposal.view_generation;
}

struct HotStuffEpochRuntimeAdapter::State
{
    struct IdentityRecord
    {
        EpochBufferedProposalIdentity identity;
        bool processed{false};
    };

    State(
        ReplicaEpochActivation &activation_value,
        ProposalContextLifecycle &contexts_value,
        ProposalAdmissionCoordinator &admission_value,
        RetryableFutureProposalStore &future_value,
        EpochConsensusBodyValidator &validator_value,
        EpochProtocolMode mode_value,
        EpochWireLimits limits_value,
        EpochRuntimeTransaction &transaction_value)
        : activation(activation_value),
          contexts(contexts_value),
          admission(admission_value),
          future(future_value),
          validator(validator_value),
          mode(mode_value),
          limits(limits_value),
          transaction(transaction_value)
    {}

    ~State()
    {
        stopped = true;
        discard_retained();
    }

    void discard_retained() noexcept
    {
        if (!retained)
            return;
        auto prepared = std::move(*retained);
        retained.reset();
        transaction.discard(std::move(prepared));
    }

    PreparedEpochRuntime consume_retained() noexcept
    {
        auto prepared = std::move(*retained);
        retained.reset();
        return prepared;
    }

    bool admissible_generation(
        const EpochConsensusEnvelope &envelope) const noexcept
    {
        const auto active = activation.active_effect();
        if (envelope.configuration == active.configuration)
            return envelope.view_generation == active.generation;
        if (mode == EpochProtocolMode::adaptive_v2 &&
            active.definition != nullptr &&
            envelope.configuration.epoch_number ==
                active.configuration.epoch_number &&
            envelope.configuration.epoch_digest ==
                active.configuration.epoch_digest &&
            active.rotation_ordinal !=
                std::numeric_limits<std::uint32_t>::max())
        {
            const auto &trees = active.definition->trees();
            if (trees.size() > 1)
            {
                const auto current = std::find_if(
                    trees.begin(),
                    trees.end(),
                    [&active](const EpochTreeDefinition &tree) {
                        return tree.tree_id ==
                               active.configuration.tree_id;
                    });
                if (current != trees.end())
                {
                    auto next = current;
                    ++next;
                    if (next == trees.end())
                        next = trees.begin();
                    const auto generation =
                        checked_activation_generation(
                            active.configuration.epoch_number,
                            static_cast<std::uint64_t>(
                                active.rotation_ordinal) +
                                1);
                    if (next->tree_id !=
                            active.configuration.tree_id &&
                        envelope.configuration.tree_id ==
                            next->tree_id &&
                        generation.has_value() &&
                        envelope.view_generation == *generation)
                        return true;
                }
            }
        }
        return std::any_of(
            staged_configurations.begin(),
            staged_configurations.end(),
            [&envelope](const EpochTreeRuntimeInput &tree) {
                return envelope.configuration == tree.configuration &&
                       envelope.view_generation ==
                           tree.activation_generation;
            });
    }

    bool existing_generation(
        const EpochConsensusEnvelope &envelope) const noexcept
    {
        return existing_effect(envelope).has_value();
    }

    std::optional<EpochActivationEffect> existing_effect(
        const EpochConsensusEnvelope &envelope) const noexcept
    {
        const auto active = activation.active_effect();
        if (envelope.configuration == active.configuration &&
            envelope.view_generation == active.generation)
            return active;
        if (draining_effect &&
            envelope.configuration == draining_effect->configuration &&
            envelope.view_generation == draining_effect->generation)
            return draining_effect;
        return std::nullopt;
    }

    void retire_staged_epoch(
        const ConfigurationId &configuration) noexcept
    {
        if (staged_configurations.empty())
            return;
        const auto &staged = staged_configurations.front().configuration;
        if (staged.epoch_number == configuration.epoch_number &&
            staged.epoch_digest == configuration.epoch_digest)
            staged_configurations.clear();
    }

    std::size_t release_and_count_remaining(
        const ConfigurationId &configuration)
    {
        std::vector<std::uint64_t> tokens;
        tokens.reserve(future.size());
        try
        {
            while (auto claim = future.claim_next(configuration))
            {
                FutureProposalClaimOwner owner(future, claim->token);
                tokens.push_back(claim->token);
                owner.release_ownership();
            }
        }
        catch (...)
        {
            for (const auto token : tokens)
                future.release(token);
            throw;
        }
        for (const auto token : tokens)
            future.release(token);
        return tokens.size() + 1;
    }

    ReplicaEpochActivation &activation;
    ProposalContextLifecycle &contexts;
    ProposalAdmissionCoordinator &admission;
    RetryableFutureProposalStore &future;
    EpochConsensusBodyValidator &validator;
    EpochProtocolMode mode;
    EpochWireLimits limits;
    EpochRuntimeTransaction &transaction;
    std::optional<PreparedEpochRuntime> retained;
    std::vector<EpochTreeRuntimeInput> staged_configurations;
    std::optional<EpochActivationEffect> draining_effect;
    std::optional<ConfigurationId> future_drain_configuration;
    std::optional<ConfigurationId> completed_drain_configuration;
    std::optional<std::size_t> remaining_hint;
    std::map<ProposalKey, IdentityRecord> identities;
    bool stopped{false};
};

HotStuffEpochRuntimeAdapter::HotStuffEpochRuntimeAdapter(
    ReplicaEpochActivation &activation,
    ProposalContextLifecycle &contexts,
    ProposalAdmissionCoordinator &admission,
    RetryableFutureProposalStore &future_proposals,
    EpochConsensusBodyValidator &body_validator,
    EpochProtocolMode expected_mode,
    EpochWireLimits wire_limits,
    EpochRuntimeTransaction &transaction)
    : state_(new State(
          activation,
          contexts,
          admission,
          future_proposals,
          body_validator,
          expected_mode,
          wire_limits,
          transaction))
{}

HotStuffEpochRuntimeAdapter::~HotStuffEpochRuntimeAdapter() = default;

ReplicaStageIngressResult HotStuffEpochRuntimeAdapter::handle_stage(
    MsgStageEpochDefinition &&message,
    const AuthenticatedEpochPeer &authenticated_peer,
    const EpochValidationContext &validation_context)
{
    if (!authenticated_manager(authenticated_peer))
        return rejected_stage(EpochIngressError::unauthorized_peer);
    const auto preflight = preflight_control(message, state_->limits);
    if (preflight != EpochWireError::none)
        return rejected_stage(EpochIngressError::wire_rejected, preflight);

    const auto canonical = static_cast<bytearray_t>(message.serialized);
    const auto decoded = decode_stage_epoch_definition(
        canonical, state_->mode, state_->limits);
    if (!decoded)
        return rejected_stage(
            EpochIngressError::wire_rejected, decoded.error);

    try
    {
        auto plan = make_runtime_plan(*decoded.value, canonical);
        auto prepared = state_->transaction.prepare(plan);
        if (!prepared)
            return rejected_stage(
                EpochIngressError::runtime_preparation_failed);
        CandidatePreparation candidate(
            state_->transaction, std::move(*prepared));

        std::optional<ReplicaStageResult> staged;
        try
        {
            staged.emplace(state_->activation.stage(
                plan.stage, validation_context));
        }
        catch (...)
        {
            return rejected_stage(EpochIngressError::validation_failed);
        }

        if (staged->disposition == ReplicaStageDisposition::staged)
        {
            state_->discard_retained();
            state_->retained = candidate.release();
        }
        else if (staged->disposition == ReplicaStageDisposition::duplicate)
        {
            if (!state_->retained)
                state_->retained = candidate.release();
        }
        else
        {
            return {
                EpochIngressError::state_rejected,
                EpochWireError::none,
                staged->disposition,
                std::nullopt};
        }
        if (plan.trees.empty())
            return rejected_stage(EpochIngressError::validation_failed);
        state_->staged_configurations = plan.trees;
        return {
            EpochIngressError::none,
            EpochWireError::none,
            staged->disposition,
            staged->acknowledgement};
    }
    catch (...)
    {
        return rejected_stage(EpochIngressError::runtime_preparation_failed);
    }
}

EpochIngressError HotStuffEpochRuntimeAdapter::prepare_committed_v2(
    const EpochDefinition &successor_definition) noexcept
{
    if (state_->mode != EpochProtocolMode::adaptive_v2 || state_->stopped)
        return EpochIngressError::state_rejected;

    try
    {
        const auto active = state_->activation.active_effect();
        if (active.definition == nullptr ||
            active.definition->schema_version() !=
                kEpochDefinitionSchemaVersionV2 ||
            successor_definition.schema_version() !=
                kEpochDefinitionSchemaVersionV2 ||
            successor_definition.activation_height() != 0 ||
            active.configuration.epoch_number ==
                std::numeric_limits<std::uint32_t>::max() ||
            successor_definition.epoch_number() !=
                active.configuration.epoch_number + 1 ||
            successor_definition.previous_epoch_digest() !=
                active.configuration.epoch_digest ||
            successor_definition.membership_digest() !=
                active.definition->membership_digest())
            return EpochIngressError::validation_failed;

        const auto retained_matches = [this, &successor_definition]() {
            return state_->retained.has_value() &&
                   !state_->staged_configurations.empty() &&
                   state_->staged_configurations.size() ==
                       successor_definition.trees().size() &&
                   std::all_of(
                       state_->staged_configurations.begin(),
                       state_->staged_configurations.end(),
                       [&successor_definition](const auto &tree) {
                           return tree.configuration.epoch_number ==
                                      successor_definition.epoch_number() &&
                                  tree.configuration.epoch_digest ==
                                      successor_definition.epoch_digest();
                       });
        };
        if (state_->retained)
            return retained_matches() ? EpochIngressError::none
                                      : EpochIngressError::state_rejected;

        auto plan = make_runtime_plan(successor_definition);
        auto staged_configurations = plan.trees;
        auto prepared = state_->transaction.prepare(plan);
        if (!prepared)
            return EpochIngressError::runtime_preparation_failed;
        CandidatePreparation candidate(
            state_->transaction, std::move(*prepared));

        state_->staged_configurations.swap(staged_configurations);
        state_->retained.emplace(candidate.release());
        return EpochIngressError::none;
    }
    catch (...)
    {
        return EpochIngressError::runtime_preparation_failed;
    }
}

void HotStuffEpochRuntimeAdapter::fail_committed_v2(
    ActivationBlockReason reason) noexcept
{
    if (state_->mode != EpochProtocolMode::adaptive_v2 ||
        reason == ActivationBlockReason::none)
        return;
    state_->discard_retained();
    state_->staged_configurations.clear();
    state_->activation.fail_committed_v2(reason);
}

ReplicaArmIngressResult HotStuffEpochRuntimeAdapter::handle_arm(
    MsgArmActivation &&message,
    const AuthenticatedEpochPeer &authenticated_peer)
{
    if (!authenticated_manager(authenticated_peer))
        return rejected_arm(EpochIngressError::unauthorized_peer);
    const auto preflight = preflight_control(message, state_->limits);
    if (preflight != EpochWireError::none)
        return rejected_arm(EpochIngressError::wire_rejected, preflight);
    const auto decoded = decode_arm_activation(
        static_cast<bytearray_t>(message.serialized),
        state_->mode,
        state_->limits);
    if (!decoded)
        return rejected_arm(
            EpochIngressError::wire_rejected, decoded.error);
    const auto disposition = state_->activation.arm(*decoded.value);
    const auto accepted =
        disposition == ReplicaArmDisposition::armed ||
        disposition == ReplicaArmDisposition::duplicate;
    return {
        accepted ? EpochIngressError::none
                 : EpochIngressError::state_rejected,
        EpochWireError::none,
        disposition};
}

namespace
{

template <typename AdapterState>
EpochCommitIngressResult finish_commit(
    AdapterState &state,
    const EpochActivationEffect &previous,
    const EpochActivationResult &preview,
    const EpochActivationResult &actual) noexcept
{
    EpochCommitIngressResult result;
    result.transition = actual.transition;
    result.blocked_reason = actual.blocked_reason;
    if (actual.transition == ActivationTransition::blocked &&
        permanent_block(actual.blocked_reason))
    {
        state.discard_retained();
        state.staged_configurations.clear();
    }
    if (preview.transition != ActivationTransition::activated ||
        actual.transition != ActivationTransition::activated ||
        !actual.effect)
        return result;

    try
    {
        const auto update = runtime_update(*actual.effect);
        auto prepared = state.consume_retained();
        state.transaction.commit(std::move(prepared), update);
        state.retire_staged_epoch(update.activation.configuration);
        state.draining_effect.emplace(previous);
        state.future_drain_configuration = update.activation.configuration;
        state.completed_drain_configuration.reset();
        state.remaining_hint.reset();
        result.update.emplace(update);
        return result;
    }
    catch (...)
    {
        result.error = EpochIngressError::state_rejected;
        result.update.reset();
        return result;
    }
}

} // namespace

EpochCommitIngressResult HotStuffEpochRuntimeAdapter::on_predecessor_commit(
    std::uint64_t height,
    const uint256_t &predecessor_digest) noexcept
{
    try
    {
        const auto preview = state_->activation.preview_predecessor_commit(
            height, predecessor_digest);
        if (preview.transition == ActivationTransition::activated &&
            !state_->retained)
            return {
                EpochIngressError::missing_prepared_runtime,
                ActivationTransition::waiting,
                ActivationBlockReason::none,
                std::nullopt};
        const auto previous = state_->activation.active_effect();
        const auto actual = state_->activation.on_predecessor_commit(
            height, predecessor_digest);
        return finish_commit(*state_, previous, preview, actual);
    }
    catch (...)
    {
        return {
            EpochIngressError::state_rejected,
            ActivationTransition::waiting,
            ActivationBlockReason::none,
            std::nullopt};
    }
}

EpochCommitIngressResult
HotStuffEpochRuntimeAdapter::on_v2_post_block_commit(
    std::uint64_t height,
    const uint256_t &predecessor_digest) noexcept
{
    if (state_->mode != EpochProtocolMode::adaptive_v2)
        return {
            EpochIngressError::state_rejected,
            ActivationTransition::waiting,
            ActivationBlockReason::none,
            std::nullopt};

    try
    {
        const auto preview =
            state_->activation.preview_v2_post_block_commit(
                height, predecessor_digest);
        const auto has_exact_runtime = [&]() {
            return state_->retained && preview.effect &&
                   std::any_of(
                       state_->staged_configurations.begin(),
                       state_->staged_configurations.end(),
                       [&preview](const auto &tree) {
                           return tree.configuration ==
                                  preview.effect->configuration;
                       });
        };
        if (preview.transition == ActivationTransition::activated &&
            !has_exact_runtime())
        {
            state_->discard_retained();
            state_->staged_configurations.clear();
            return {
                EpochIngressError::missing_prepared_runtime,
                ActivationTransition::waiting,
                ActivationBlockReason::none,
                std::nullopt};
        }
        const auto previous = state_->activation.active_effect();
        const auto actual = state_->activation.on_v2_post_block_commit(
            height, predecessor_digest);
        return finish_commit(*state_, previous, preview, actual);
    }
    catch (...)
    {
        return {
            EpochIngressError::state_rejected,
            ActivationTransition::waiting,
            ActivationBlockReason::none,
            std::nullopt};
    }
}

EpochCommitIngressResult HotStuffEpochRuntimeAdapter::replay_blocked_commit()
    noexcept
{
    try
    {
        const auto preview = state_->activation.preview_blocked_commit();
        if (preview.transition == ActivationTransition::activated &&
            !state_->retained)
            return {
                EpochIngressError::missing_prepared_runtime,
                ActivationTransition::waiting,
                ActivationBlockReason::none,
                std::nullopt};
        const auto previous = state_->activation.active_effect();
        const auto actual = state_->activation.replay_blocked_commit();
        return finish_commit(*state_, previous, preview, actual);
    }
    catch (...)
    {
        return {
            EpochIngressError::state_rejected,
            ActivationTransition::waiting,
            ActivationBlockReason::none,
            std::nullopt};
    }
}

EpochRotationResult HotStuffEpochRuntimeAdapter::rotate_to_tree(
    std::uint32_t tree_id) noexcept
{
    try
    {
        const auto previous = state_->activation.active_effect();
        if (previous.configuration.tree_id == tree_id)
            return {EpochIngressError::none, std::nullopt};
        const auto effect = state_->activation.rotate_to_tree(tree_id);
        const auto update = runtime_update(effect);
        state_->transaction.rotate(EpochRuntimeRotation{
            update.activation, update.leader_view});
        state_->retire_staged_epoch(update.activation.configuration);
        state_->draining_effect.emplace(previous);
        if (state_->mode == EpochProtocolMode::adaptive_v2)
        {
            state_->future_drain_configuration =
                update.activation.configuration;
            state_->completed_drain_configuration.reset();
            state_->remaining_hint.reset();
        }
        return {EpochIngressError::none, update};
    }
    catch (...)
    {
        return {EpochIngressError::state_rejected, std::nullopt};
    }
}

EpochConsensusIngressResult HotStuffEpochRuntimeAdapter::handle_proposal(
    MsgPropose &&message,
    const AuthenticatedEpochPeer &authenticated_peer) const noexcept
{
    if (!authenticated_replica(authenticated_peer))
        return rejected_consensus(EpochIngressError::unauthorized_peer);
    try
    {
        const auto preflight = preflight_consensus(message, state_->limits);
        if (preflight != EpochConsensusWireError::none)
            return rejected_consensus(
                EpochIngressError::wire_rejected, preflight);
        const auto raw = static_cast<bytearray_t>(message.serialized);
        const auto decoded = decode_epoch_consensus_envelope(
            raw,
            EpochConsensusWireKind::proposal,
            state_->mode,
            state_->limits);
        if (!decoded)
            return rejected_consensus(
                EpochIngressError::wire_rejected, decoded.error);
        const auto &envelope = *decoded.value;
        if (!state_->admissible_generation(envelope))
            return rejected_consensus(EpochIngressError::state_rejected);
        if (!state_->validator.validate_proposal(
                envelope, authenticated_peer))
            return rejected_consensus(
                EpochIngressError::wire_rejected,
                EpochConsensusWireError::invalid_body);
        if (!state_->activation.admits_new_proposals())
            return {
                EpochIngressError::none,
                EpochConsensusWireError::none,
                EpochConsensusPermission::paused,
                envelope,
                std::nullopt};

        BufferedProposal proposal{
            ProposalMetadata{
                envelope.configuration,
                envelope.block_hash,
                envelope.proposer},
            raw,
            authenticated_peer.source_peer,
            envelope.view_generation,
            DataStream(raw).get_hash(),
            envelope.body};
        auto retryable = proposal;
        const auto admission = state_->admission.receive(std::move(proposal));
        if (admission.disposition == ProposalDisposition::buffered_future)
        {
            if (!state_->future.insert(retryable))
                return rejected_consensus(EpochIngressError::state_rejected);
            state_->identities[admission.key] =
                State::IdentityRecord{proposal_identity(retryable), false};
        }
        else if (admission.disposition == ProposalDisposition::admitted_active)
        {
            state_->identities[admission.key] =
                State::IdentityRecord{proposal_identity(retryable), true};
        }
        const bool accepted =
            admission.disposition == ProposalDisposition::buffered_future ||
            admission.disposition == ProposalDisposition::admitted_active ||
            admission.disposition == ProposalDisposition::duplicate;
        return {
            accepted ? EpochIngressError::none
                     : EpochIngressError::state_rejected,
            EpochConsensusWireError::none,
            accepted ? EpochConsensusPermission::admit_or_buffer
                     : EpochConsensusPermission::rejected_identity,
            envelope,
            admission.disposition};
    }
    catch (const std::bad_alloc &)
    {
        return rejected_consensus(
            EpochIngressError::wire_rejected,
            EpochConsensusWireError::payload_too_large);
    }
    catch (...)
    {
        return rejected_consensus(EpochIngressError::state_rejected);
    }
}

EpochConsensusIngressResult
HotStuffEpochRuntimeAdapter::handle_existing_proposal(
    MsgPropose &&message,
    const AuthenticatedEpochPeer &authenticated_peer,
    const ProposalContextLease &lease) const noexcept
{
    if (!authenticated_replica(authenticated_peer))
        return rejected_consensus(EpochIngressError::unauthorized_peer);
    try
    {
        const auto decoded = decode_message(
            message,
            EpochConsensusWireKind::proposal,
            state_->mode,
            state_->limits);
        if (!decoded)
            return rejected_consensus(
                EpochIngressError::wire_rejected, decoded.error);
        const auto &envelope = *decoded.value;
        if (!state_->contexts.revalidate(lease) ||
            lease.key() != envelope.key() ||
            !state_->activation.may_drain_exact_context(
                envelope.configuration) ||
            !state_->existing_generation(envelope))
            return rejected_consensus(EpochIngressError::state_rejected);
        if (!state_->validator.validate_proposal(
                envelope, authenticated_peer))
            return rejected_consensus(
                EpochIngressError::wire_rejected,
                EpochConsensusWireError::invalid_body);
        return {
            EpochIngressError::none,
            EpochConsensusWireError::none,
            EpochConsensusPermission::drain_existing,
            envelope,
            std::nullopt};
    }
    catch (...)
    {
        return rejected_consensus(EpochIngressError::state_rejected);
    }
}

namespace
{

template <typename AdapterState, typename Message, typename Validate>
EpochConsensusIngressResult handle_contribution(
    AdapterState &state,
    Message &&message,
    const AuthenticatedEpochPeer &peer,
    EpochConsensusWireKind kind,
    Validate &&validate) noexcept
{
    if (!authenticated_replica(peer))
        return rejected_consensus(EpochIngressError::unauthorized_peer);
    try
    {
        const auto decoded = decode_message(
            message, kind, state.mode, state.limits);
        if (!decoded)
            return rejected_consensus(
                EpochIngressError::wire_rejected, decoded.error);
        const auto &envelope = *decoded.value;
        const auto effect = state.existing_effect(envelope);
        const auto active = state.activation.active_effect();
        const bool draining = effect &&
            (effect->configuration != active.configuration ||
             effect->generation != active.generation);
        const auto *const tree = effect
            ? find_tree(*effect, envelope.configuration.tree_id)
            : nullptr;
        if (!effect ||
            envelope.originator != *peer.replica_id ||
            tree == nullptr || tree->members_breadth_first.empty() ||
            envelope.proposer != tree->members_breadth_first.front() ||
            (draining &&
             !state.contexts.acquire_open_context(
                 envelope.key()).has_value()))
            return rejected_consensus(EpochIngressError::state_rejected);
        if (!validate(envelope, peer))
            return rejected_consensus(
                EpochIngressError::wire_rejected,
                EpochConsensusWireError::invalid_body);
        return {
            EpochIngressError::none,
            EpochConsensusWireError::none,
            EpochConsensusPermission::accept_contribution,
            envelope,
            std::nullopt};
    }
    catch (...)
    {
        return rejected_consensus(EpochIngressError::state_rejected);
    }
}

} // namespace

EpochConsensusIngressResult HotStuffEpochRuntimeAdapter::handle_vote(
    MsgVote &&message,
    const AuthenticatedEpochPeer &authenticated_peer) const noexcept
{
    return handle_contribution(
        *state_,
        std::move(message),
        authenticated_peer,
        EpochConsensusWireKind::vote,
        [this](const auto &envelope, const auto &peer) {
            return state_->validator.validate_vote(envelope, peer);
        });
}

EpochConsensusIngressResult HotStuffEpochRuntimeAdapter::handle_relay(
    MsgRelay &&message,
    const AuthenticatedEpochPeer &authenticated_peer) const noexcept
{
    return handle_contribution(
        *state_,
        std::move(message),
        authenticated_peer,
        EpochConsensusWireKind::relay,
        [this](const auto &envelope, const auto &peer) {
            return state_->validator.validate_relay(envelope, peer);
        });
}

std::optional<EpochBufferedProposalIdentity>
HotStuffEpochRuntimeAdapter::buffered_proposal_identity(
    const ProposalKey &key) const noexcept
{
    const auto found = state_->identities.find(key);
    if (found == state_->identities.end() || found->second.processed)
        return std::nullopt;
    return found->second.identity;
}

std::optional<EpochBufferedProposalIdentity>
HotStuffEpochRuntimeAdapter::processed_proposal_identity(
    const ProposalKey &key) const noexcept
{
    const auto found = state_->identities.find(key);
    if (found == state_->identities.end() || !found->second.processed)
        return std::nullopt;
    return found->second.identity;
}

EpochFutureDrainResult
HotStuffEpochRuntimeAdapter::drain_activated_futures() noexcept
{
    if (state_->stopped)
        return {EpochFutureDrainStatus::stopped, 0, state_->future.size()};
    if (!state_->future_drain_configuration ||
        state_->activation.active_effect().configuration !=
            *state_->future_drain_configuration)
        return {
            EpochFutureDrainStatus::inactive_configuration,
            0,
            state_->future.size()};
    const auto configuration = *state_->future_drain_configuration;
    if (state_->completed_drain_configuration == configuration)
        return {EpochFutureDrainStatus::complete, 0, 0};

    std::size_t processed = 0;
    for (;;)
    {
        std::optional<FutureProposalClaim> claim;
        try
        {
            claim = state_->future.claim_next(configuration);
        }
        catch (const std::bad_alloc &)
        {
            return {
                EpochFutureDrainStatus::allocation_failed,
                processed,
                state_->remaining_hint.value_or(state_->future.size())};
        }
        catch (...)
        {
            return {
                EpochFutureDrainStatus::process_failed,
                processed,
                state_->future.size()};
        }

        if (!claim)
        {
            state_->future.complete(configuration);
            state_->completed_drain_configuration = configuration;
            state_->remaining_hint.reset();
            return {EpochFutureDrainStatus::complete, processed, 0};
        }

        FutureProposalClaimOwner owner(state_->future, claim->token);
        try
        {
            const auto active = state_->activation.active_effect();
            if (active.configuration != configuration)
                return {
                    EpochFutureDrainStatus::inactive_configuration,
                    processed,
                    state_->future.size()};
            if (state_->mode == EpochProtocolMode::adaptive_v2 &&
                claim->proposal.view_generation != active.generation)
            {
                const auto key = claim->proposal.metadata.key();
                owner.acknowledge();
                state_->identities.erase(key);
                continue;
            }
            state_->future.process_active(*claim);
            owner.acknowledge();
            const auto identity = state_->identities.find(
                claim->proposal.metadata.key());
            if (identity != state_->identities.end())
                identity->second.processed = true;
            ++processed;
        }
        catch (const std::bad_alloc &)
        {
            return {
                EpochFutureDrainStatus::allocation_failed,
                processed,
                state_->remaining_hint.value_or(state_->future.size())};
        }
        catch (...)
        {
            try
            {
                const auto remaining = state_->release_and_count_remaining(
                    configuration);
                state_->remaining_hint = remaining;
                return {
                    EpochFutureDrainStatus::process_failed,
                    processed,
                    remaining};
            }
            catch (...)
            {
                return {
                    EpochFutureDrainStatus::allocation_failed,
                    processed,
                    state_->future.size()};
            }
        }
    }
}

struct ManagerEpochAckEndpoint::State
{
    State(
        EpochAckTracker &tracker_value,
        EpochProtocolMode mode_value,
        EpochWireLimits limits_value)
        : tracker(tracker_value), mode(mode_value), limits(limits_value)
    {}

    EpochAckTracker &tracker;
    EpochProtocolMode mode;
    EpochWireLimits limits;
    bool arm_emitted{false};
};

ManagerEpochAckEndpoint::ManagerEpochAckEndpoint(
    EpochAckTracker &tracker,
    EpochProtocolMode expected_mode,
    EpochWireLimits wire_limits)
    : state_(new State(tracker, expected_mode, wire_limits))
{}

ManagerEpochAckEndpoint::~ManagerEpochAckEndpoint() = default;

ManagerAckIngressResult ManagerEpochAckEndpoint::handle_ack(
    MsgStageAck &&message,
    const AuthenticatedEpochPeer &authenticated_peer)
{
    if (!authenticated_replica(authenticated_peer))
        return {
            EpochIngressError::unauthorized_peer,
            EpochWireError::none,
            std::nullopt,
            state_->tracker.acknowledgement_count(),
            state_->tracker.required_acknowledgements(),
            std::nullopt};
    const auto preflight = preflight_control(message, state_->limits);
    if (preflight != EpochWireError::none)
        return {
            EpochIngressError::wire_rejected,
            preflight,
            std::nullopt,
            state_->tracker.acknowledgement_count(),
            state_->tracker.required_acknowledgements(),
            std::nullopt};
    const auto decoded = decode_stage_ack(
        static_cast<bytearray_t>(message.serialized),
        state_->mode,
        state_->limits);
    if (!decoded)
        return {
            EpochIngressError::wire_rejected,
            decoded.error,
            std::nullopt,
            state_->tracker.acknowledgement_count(),
            state_->tracker.required_acknowledgements(),
            std::nullopt};

    const auto recorded = state_->tracker.record(
        AuthenticatedReporter{*authenticated_peer.replica_id},
        *decoded.value);
    std::optional<ArmActivation> arm;
    if (recorded.ready && !state_->arm_emitted &&
        recorded.disposition == StageAckDisposition::accepted)
    {
        arm = state_->tracker.build_arm();
        state_->arm_emitted = true;
    }
    const auto accepted =
        recorded.disposition == StageAckDisposition::accepted ||
        recorded.disposition == StageAckDisposition::duplicate;
    return {
        accepted ? EpochIngressError::none
                 : EpochIngressError::state_rejected,
        EpochWireError::none,
        recorded.disposition,
        recorded.accepted_acknowledgements,
        recorded.required_acknowledgements,
        std::move(arm)};
}

} // namespace hotstuff
