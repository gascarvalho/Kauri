#include "hotstuff/adaptation_manager.h"

#include <algorithm>
#include <stdexcept>
#include <utility>

namespace hotstuff
{
namespace
{

bool is_nonzero(
    const AdaptationCertificateFingerprint &fingerprint) noexcept
{
    return std::any_of(
        fingerprint.begin(), fingerprint.end(),
        [](std::uint8_t value) { return value != 0; });
}

template <typename Value>
bool unique_sorted(std::vector<Value> values)
{
    std::sort(values.begin(), values.end());
    return std::adjacent_find(values.begin(), values.end()) == values.end();
}

std::vector<ReplicaID> sorted_members(
    const std::vector<ReplicaID> &membership)
{
    auto result = membership;
    std::sort(result.begin(), result.end());
    return result;
}

bool contains_tree(
    const std::vector<std::uint32_t> &tree_ids,
    std::uint32_t tree_id) noexcept
{
    return std::find(tree_ids.begin(), tree_ids.end(), tree_id) !=
           tree_ids.end();
}

bool exact_epoch_configuration(
    const ResponseObservation &observation,
    std::uint32_t epoch_number,
    const uint256_t &epoch_digest,
    const std::vector<std::uint32_t> &tree_ids) noexcept
{
    return observation.configuration.epoch_number == epoch_number &&
           observation.configuration.epoch_digest == epoch_digest &&
           contains_tree(tree_ids, observation.configuration.tree_id);
}

} // namespace

struct AdaptationManagerCoordinator::State
{
    State(
        const EpochDefinition &current_epoch,
        ConfigurationId configuration,
        std::uint64_t activation_generation,
        std::vector<PinnedAdaptationIdentity> identities,
        AdaptationPolicy policy,
        TreePlacementInput placement,
        FaultContainmentPolicy containment,
        std::uint32_t faults,
        std::uint64_t exact_activation_height,
        std::uint64_t activation_grace)
        : current_epoch_number(current_epoch.epoch_number()),
          current_epoch_digest(current_epoch.epoch_digest()),
          current_membership_digest(current_epoch.membership_digest()),
          current_configuration(std::move(configuration)),
          current_generation(activation_generation),
          pinned_identities(std::move(identities)),
          adaptation_policy(std::move(policy)),
          placement_input(std::move(placement)),
          containment_policy(std::move(containment)),
          tolerated_faults(faults),
          activation_height(exact_activation_height),
          minimum_activation_grace(activation_grace),
          readiness_seen(pinned_identities.size(), false),
          readiness_height(pinned_identities.size(), 0)
    {
        for (const auto &tree : current_epoch.trees())
            current_tree_ids.push_back(tree.tree_id);
    }

    AdaptationManagerRecordResult result(
        AdaptationManagerRecordDisposition disposition) const noexcept
    {
        return {disposition, manager_state};
    }

    std::optional<std::size_t> identity_for_fingerprint(
        const AdaptationCertificateFingerprint &fingerprint) const noexcept
    {
        for (std::size_t index = 0;
             index < pinned_identities.size();
             ++index)
        {
            if (pinned_identities[index].certificate_fingerprint ==
                fingerprint)
                return index;
        }
        return std::nullopt;
    }

    std::optional<std::size_t> authenticate_replica(
        const AdaptationCertificateFingerprint &fingerprint,
        ReplicaID payload_replica,
        AdaptationManagerRecordDisposition &failure) const noexcept
    {
        const auto identity = identity_for_fingerprint(fingerprint);
        if (!identity)
        {
            failure = AdaptationManagerRecordDisposition::unknown_identity;
            return std::nullopt;
        }
        const auto &pinned = pinned_identities[*identity];
        if (pinned.role != AdaptationIdentityRole::replica)
        {
            failure = AdaptationManagerRecordDisposition::wrong_role;
            return std::nullopt;
        }
        if (pinned.replica_id != payload_replica)
        {
            failure = AdaptationManagerRecordDisposition::
                payload_identity_mismatch;
            return std::nullopt;
        }
        return identity;
    }

    bool all_replicas_ready() const noexcept
    {
        for (std::size_t index = 0;
             index < pinned_identities.size();
             ++index)
        {
            if (pinned_identities[index].role ==
                    AdaptationIdentityRole::replica &&
                !readiness_seen[index])
                return false;
        }
        return true;
    }

    bool activation_grace_satisfied() const noexcept
    {
        for (std::size_t index = 0;
             index < pinned_identities.size();
             ++index)
        {
            if (pinned_identities[index].role !=
                AdaptationIdentityRole::replica)
                continue;
            const auto committed = readiness_height[index];
            if (committed > activation_height ||
                activation_height - committed <
                    minimum_activation_grace)
                return false;
        }
        return true;
    }

    std::optional<std::size_t> responsive_index(
        ReplicaID replica) const noexcept
    {
        const auto found = std::find(
            responsive_replicas.begin(),
            responsive_replicas.end(), replica);
        if (found == responsive_replicas.end())
            return std::nullopt;
        return static_cast<std::size_t>(
            found - responsive_replicas.begin());
    }

    bool all_acknowledged() const noexcept
    {
        return !stage_acknowledged.empty() &&
               std::all_of(
                   stage_acknowledged.begin(),
                   stage_acknowledged.end(),
                   [](bool value) { return value; });
    }

    bool all_activated() const noexcept
    {
        return !activation_reported.empty() &&
               std::all_of(
                   activation_reported.begin(),
                   activation_reported.end(),
                   [](bool value) { return value; });
    }

    std::uint32_t current_epoch_number{0};
    uint256_t current_epoch_digest;
    uint256_t current_membership_digest;
    ConfigurationId current_configuration;
    std::uint64_t current_generation{0};
    std::vector<std::uint32_t> current_tree_ids;
    std::vector<PinnedAdaptationIdentity> pinned_identities;
    AdaptationPolicy adaptation_policy;
    TreePlacementInput placement_input;
    FaultContainmentPolicy containment_policy;
    std::uint32_t tolerated_faults{0};
    std::uint64_t activation_height{0};
    std::uint64_t minimum_activation_grace{0};
    std::uint32_t successor_epoch_number{0};

    AdaptationManagerState manager_state{
        AdaptationManagerState::waiting_for_readiness};
    std::vector<bool> readiness_seen;
    std::vector<std::uint64_t> readiness_height;

    std::vector<AcceptedEvidenceRecord> frozen_evidence;
    std::unique_ptr<AdaptationSnapshot> snapshot;
    std::vector<ReplicaID> responsive_replicas;
    std::unique_ptr<TreePlacementResult> placement;
    std::unique_ptr<StageEpochDefinition> stage;
    std::vector<bool> stage_acknowledged;
    std::vector<bool> activation_reported;
};

AdaptationManagerCoordinator::AdaptationManagerCoordinator(
    const EpochDefinition &current_epoch,
    ConfigurationId current_configuration,
    std::uint64_t current_activation_generation,
    std::vector<PinnedAdaptationIdentity> pinned_identities,
    AdaptationPolicy adaptation_policy,
    TreePlacementInput placement_input,
    FaultContainmentPolicy containment_policy,
    std::uint32_t tolerated_faults,
    std::uint64_t activation_height,
    std::uint64_t minimum_activation_grace)
    : state_(new State(
          current_epoch,
          std::move(current_configuration),
          current_activation_generation,
          std::move(pinned_identities),
          std::move(adaptation_policy),
          std::move(placement_input),
          std::move(containment_policy),
          tolerated_faults,
          activation_height,
          minimum_activation_grace))
{
    if (state_->placement_input.membership.empty() ||
        state_->placement_input.membership.size() >
            kMaximumAdaptationMembers ||
        state_->pinned_identities.empty() ||
        state_->pinned_identities.size() >
            kMaximumAdaptationMembers + 1)
        throw std::invalid_argument(
            "adaptation manager identity bounds are invalid");

    if (state_->current_configuration.epoch_number !=
            state_->current_epoch_number ||
        state_->current_configuration.epoch_digest !=
            state_->current_epoch_digest ||
        !contains_tree(
            state_->current_tree_ids,
            state_->current_configuration.tree_id))
        throw std::invalid_argument(
            "adaptation manager current configuration is not exact");

    auto members = sorted_members(state_->placement_input.membership);
    if (!unique_sorted(members) ||
        canonical_membership_digest(members) !=
            state_->current_membership_digest)
        throw std::invalid_argument(
            "adaptation manager membership does not match the epoch");

    if (current_activation_generation == 0)
        throw std::invalid_argument(
            "adaptation manager generation or fault bound is invalid");
    const auto generation_epoch = static_cast<std::uint32_t>(
        (current_activation_generation - 1) >> 32);
    if (generation_epoch != state_->current_epoch_number ||
        state_->tolerated_faults > (members.size() - 1) / 3)
        throw std::invalid_argument(
            "adaptation manager generation or fault bound is invalid");

    std::vector<ReplicaID> identity_ids;
    std::vector<ReplicaID> replica_ids;
    std::vector<AdaptationCertificateFingerprint> fingerprints;
    identity_ids.reserve(state_->pinned_identities.size());
    replica_ids.reserve(members.size());
    fingerprints.reserve(state_->pinned_identities.size());
    std::size_t manager_count = 0;
    for (const auto &identity : state_->pinned_identities)
    {
        if (!is_nonzero(identity.certificate_fingerprint))
            throw std::invalid_argument(
                "adaptation manager fingerprint is empty");
        identity_ids.push_back(identity.replica_id);
        fingerprints.push_back(identity.certificate_fingerprint);
        switch (identity.role)
        {
        case AdaptationIdentityRole::manager:
            ++manager_count;
            break;
        case AdaptationIdentityRole::replica:
            replica_ids.push_back(identity.replica_id);
            break;
        default:
            throw std::invalid_argument(
                "adaptation manager identity role is invalid");
        }
    }
    std::sort(replica_ids.begin(), replica_ids.end());
    if (manager_count != 1 || replica_ids != members ||
        !unique_sorted(identity_ids) ||
        !unique_sorted(fingerprints))
        throw std::invalid_argument(
            "adaptation manager identities are not uniquely pinned");

    const auto successor = checked_successor_epoch(
        state_->current_epoch_number);
    if (!successor || activation_height == 0 ||
        activation_height < minimum_activation_grace)
        throw std::invalid_argument(
            "adaptation manager activation bounds are invalid");
    state_->successor_epoch_number = *successor;
}

AdaptationManagerCoordinator::~AdaptationManagerCoordinator() = default;

AdaptationManagerRecordResult
AdaptationManagerCoordinator::record_authenticated_readiness(
    const AdaptationCertificateFingerprint &certificate_fingerprint,
    ReplicaID payload_replica_id,
    const ConfigurationId &active_configuration,
    std::uint64_t activation_generation,
    std::uint64_t committed_height) noexcept
{
    AdaptationManagerRecordDisposition failure{
        AdaptationManagerRecordDisposition::unknown_identity};
    const auto identity = state_->authenticate_replica(
        certificate_fingerprint, payload_replica_id, failure);
    if (!identity)
        return state_->result(failure);
    if (active_configuration != state_->current_configuration)
        return state_->result(
            AdaptationManagerRecordDisposition::wrong_configuration);
    if (activation_generation != state_->current_generation)
        return state_->result(
            AdaptationManagerRecordDisposition::wrong_generation);

    if (state_->manager_state !=
        AdaptationManagerState::waiting_for_readiness)
    {
        return state_->result(
            state_->readiness_seen[*identity]
                ? AdaptationManagerRecordDisposition::duplicate
                : AdaptationManagerRecordDisposition::wrong_state);
    }

    if (state_->readiness_seen[*identity])
    {
        if (committed_height <= state_->readiness_height[*identity])
            return state_->result(
                AdaptationManagerRecordDisposition::duplicate);
    }
    state_->readiness_seen[*identity] = true;
    state_->readiness_height[*identity] = committed_height;

    if (state_->all_replicas_ready())
    {
        if (!state_->activation_grace_satisfied())
            return state_->result(
                AdaptationManagerRecordDisposition::wrong_configuration);
        state_->manager_state =
            AdaptationManagerState::collecting_baseline_evidence;
    }
    return state_->result(AdaptationManagerRecordDisposition::accepted);
}

AdaptationManagerRecordResult
AdaptationManagerCoordinator::freeze_baseline_evidence(
    AcceptedEvidenceView accepted_evidence,
    std::uint64_t evidence_cutoff)
{
    if (state_->manager_state !=
        AdaptationManagerState::collecting_baseline_evidence)
        return state_->result(
            AdaptationManagerRecordDisposition::wrong_state);
    if (evidence_cutoff == 0 || accepted_evidence.size == 0 ||
        accepted_evidence.size > kMaximumAdaptationEvidenceRecords ||
        accepted_evidence.data == nullptr)
        return state_->result(
            AdaptationManagerRecordDisposition::stale_evidence_cutoff);

    try
    {
        std::vector<AcceptedEvidenceRecord> frozen;
        frozen.reserve(accepted_evidence.size);
        std::uint64_t previous_sequence = 0;
        for (std::size_t index = 0;
             index < accepted_evidence.size;
             ++index)
        {
            const auto &record = accepted_evidence.data[index];
            if (record.ingestion_sequence == 0 ||
                record.ingestion_sequence <= previous_sequence)
                return state_->result(
                    AdaptationManagerRecordDisposition::incomplete_evidence);
            previous_sequence = record.ingestion_sequence;
            if (record.ingestion_sequence > evidence_cutoff)
                continue;
            if (!exact_epoch_configuration(
                    record.observation,
                    state_->current_epoch_number,
                    state_->current_epoch_digest,
                    state_->current_tree_ids))
                return state_->result(
                    AdaptationManagerRecordDisposition::
                        mixed_evidence_configuration);
            frozen.push_back(record);
        }
        if (frozen.empty())
            return state_->result(
                AdaptationManagerRecordDisposition::stale_evidence_cutoff);

        const AcceptedEvidenceView frozen_view{
            frozen.data(), frozen.size()};
        auto snapshot_value = build_adaptation_snapshot(
            state_->placement_input.membership,
            AdaptationEpochId{
                state_->current_epoch_number,
                state_->current_epoch_digest},
            frozen_view,
            evidence_cutoff,
            state_->adaptation_policy,
            state_->placement_input.generation_seed);

        std::vector<ReplicaID> responsive;
        responsive.reserve(snapshot_value.ranking().size());
        for (const auto &replica : snapshot_value.ranking())
        {
            if (replica.classification ==
                ResponsivenessClass::insufficient_evidence)
                return state_->result(
                    AdaptationManagerRecordDisposition::incomplete_evidence);
            if (replica.classification ==
                    ResponsivenessClass::responsive &&
                replica.eligible)
                responsive.push_back(replica.replica_id);
        }
        const auto required_responsive =
            static_cast<std::size_t>(state_->tolerated_faults) * 2 + 1;
        if (responsive.size() < required_responsive)
            return state_->result(
                AdaptationManagerRecordDisposition::incomplete_evidence);

        auto snapshot = std::make_unique<AdaptationSnapshot>(
            std::move(snapshot_value));
        auto placement_value = build_tree_placement(
            state_->placement_input,
            *snapshot,
            state_->containment_policy);
        auto placement = std::make_unique<TreePlacementResult>(
            std::move(placement_value));

        EpochDefinitionInput definition;
        definition.epoch_number = state_->successor_epoch_number;
        definition.previous_epoch_digest = state_->current_epoch_digest;
        definition.membership_digest = state_->current_membership_digest;
        definition.trees = placement->trees();
        definition.activation_height = state_->activation_height;
        definition.generation_seed =
            state_->placement_input.generation_seed;
        definition.policy_version =
            state_->placement_input.policy_version;
        definition.evidence_snapshot_id = snapshot->snapshot_id();
        definition.evidence_cutoff = evidence_cutoff;
        definition.epoch_digest.reset();
        const auto successor_digest = compute_epoch_digest(definition);
        definition.epoch_digest = successor_digest;

        auto stage = std::make_unique<StageEpochDefinition>(
            StageEpochDefinition{
                kEpochWireSchemaVersion,
                EpochProtocolMode::adaptive_v1,
                EpochActivationIdentity{
                    state_->current_epoch_number,
                    state_->current_epoch_digest,
                    state_->successor_epoch_number,
                    successor_digest,
                    state_->activation_height},
                std::move(definition)});
        std::vector<bool> acknowledgements(
            responsive.size(), false);
        std::vector<bool> activations(responsive.size(), false);

        state_->frozen_evidence.swap(frozen);
        state_->responsive_replicas.swap(responsive);
        state_->stage_acknowledged.swap(acknowledgements);
        state_->activation_reported.swap(activations);
        state_->snapshot = std::move(snapshot);
        state_->placement = std::move(placement);
        state_->stage = std::move(stage);
        state_->manager_state =
            AdaptationManagerState::containment_generated;
        return state_->result(
            AdaptationManagerRecordDisposition::accepted);
    }
    catch (...)
    {
        return state_->result(
            AdaptationManagerRecordDisposition::incomplete_evidence);
    }
}

std::optional<AdaptationManagerDecisionView>
AdaptationManagerCoordinator::containment_decision() const noexcept
{
    if (state_->manager_state ==
            AdaptationManagerState::waiting_for_readiness ||
        state_->manager_state ==
            AdaptationManagerState::collecting_baseline_evidence ||
        !state_->snapshot || !state_->placement || !state_->stage)
        return std::nullopt;
    return AdaptationManagerDecisionView{
        state_->snapshot.get(),
        &state_->responsive_replicas,
        state_->placement.get(),
        state_->stage.get()};
}

AdaptationManagerRecordResult
AdaptationManagerCoordinator::record_authenticated_stage_ack(
    const AdaptationCertificateFingerprint &certificate_fingerprint,
    const StageAck &acknowledgement) noexcept
{
    AdaptationManagerRecordDisposition failure{
        AdaptationManagerRecordDisposition::unknown_identity};
    const auto identity = state_->authenticate_replica(
        certificate_fingerprint,
        acknowledgement.replica_id,
        failure);
    if (!identity)
        return state_->result(failure);
    if (!state_->stage ||
        acknowledgement.wire_schema_version !=
            kEpochWireSchemaVersion ||
        acknowledgement.protocol_mode !=
            EpochProtocolMode::adaptive_v1 ||
        acknowledgement.activation != state_->stage->activation)
        return state_->result(
            AdaptationManagerRecordDisposition::wrong_configuration);

    const auto required = state_->responsive_index(
        acknowledgement.replica_id);
    if (!required)
        return state_->result(
            AdaptationManagerRecordDisposition::
                acknowledgement_not_required);
    if (state_->stage_acknowledged[*required])
        return state_->result(
            AdaptationManagerRecordDisposition::duplicate);
    if (state_->manager_state !=
        AdaptationManagerState::containment_generated)
        return state_->result(
            AdaptationManagerRecordDisposition::wrong_state);

    state_->stage_acknowledged[*required] = true;
    return state_->result(AdaptationManagerRecordDisposition::accepted);
}

std::optional<ArmActivation>
AdaptationManagerCoordinator::arm_activation() const
{
    if (!state_->stage)
        return std::nullopt;
    if (state_->manager_state ==
        AdaptationManagerState::containment_generated)
    {
        if (!state_->all_acknowledged())
            return std::nullopt;
        state_->manager_state =
            AdaptationManagerState::containment_armed;
    }
    if (state_->manager_state !=
            AdaptationManagerState::containment_armed &&
        state_->manager_state !=
            AdaptationManagerState::waiting_for_containment_activation &&
        state_->manager_state !=
            AdaptationManagerState::containment_converged)
        return std::nullopt;
    return ArmActivation{
        kEpochWireSchemaVersion,
        EpochProtocolMode::adaptive_v1,
        state_->stage->activation};
}

AdaptationManagerRecordResult
AdaptationManagerCoordinator::record_authenticated_activation(
    const AdaptationCertificateFingerprint &certificate_fingerprint,
    const ActivationStatus &status) noexcept
{
    AdaptationManagerRecordDisposition failure{
        AdaptationManagerRecordDisposition::unknown_identity};
    const auto identity = state_->authenticate_replica(
        certificate_fingerprint, status.replica_id, failure);
    if (!identity)
        return state_->result(failure);
    if (!state_->stage ||
        status.wire_schema_version != kEpochWireSchemaVersion ||
        status.protocol_mode != EpochProtocolMode::adaptive_v1 ||
        status.activation != state_->stage->activation)
        return state_->result(
            AdaptationManagerRecordDisposition::wrong_configuration);
    if (status.recovery_need != ActivationRecoveryNeed::none)
        return state_->result(
            AdaptationManagerRecordDisposition::wrong_state);

    const auto required = state_->responsive_index(status.replica_id);
    if (!required)
        return state_->result(
            AdaptationManagerRecordDisposition::
                acknowledgement_not_required);
    if (state_->activation_reported[*required])
        return state_->result(
            AdaptationManagerRecordDisposition::duplicate);
    if (state_->manager_state !=
            AdaptationManagerState::containment_armed &&
        state_->manager_state !=
            AdaptationManagerState::waiting_for_containment_activation)
        return state_->result(
            AdaptationManagerRecordDisposition::wrong_state);

    state_->activation_reported[*required] = true;
    if (state_->all_activated())
        state_->manager_state =
            AdaptationManagerState::containment_converged;
    else
        state_->manager_state =
            AdaptationManagerState::waiting_for_containment_activation;
    return state_->result(AdaptationManagerRecordDisposition::accepted);
}

AdaptationManagerState
AdaptationManagerCoordinator::state() const noexcept
{
    return state_->manager_state;
}

bool AdaptationManagerCoordinator::converged() const noexcept
{
    return state_->manager_state ==
           AdaptationManagerState::containment_converged;
}

} // namespace hotstuff
