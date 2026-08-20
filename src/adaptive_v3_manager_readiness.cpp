#include "hotstuff/adaptive_v3_manager_readiness.h"

#include <algorithm>
#include <map>
#include <set>
#include <stdexcept>

namespace hotstuff
{
bool AdaptiveV3TransitionProjection::matches_static(
    const ActivationReadyIdentityV1 &identity) const noexcept
{
    if (identity.predecessor_boundary_generation == 0)
        return false;
    const auto predecessor_ordinal = static_cast<std::uint32_t>(
        identity.predecessor_boundary_generation - 1);
    const auto canonical_predecessor_generation =
        checked_activation_generation(
            predecessor_epoch_number, predecessor_ordinal);
    return identity.schema_version == kActivationReadinessSchemaVersionV1 &&
           identity.membership_digest == membership_digest &&
           identity.predecessor_boundary_configuration.epoch_number ==
               predecessor_epoch_number &&
           identity.predecessor_boundary_configuration.epoch_digest ==
               predecessor_epoch_digest &&
           std::binary_search(
               predecessor_tree_ids.begin(), predecessor_tree_ids.end(),
               identity.predecessor_boundary_configuration.tree_id) &&
           canonical_predecessor_generation.has_value() &&
           *canonical_predecessor_generation ==
               identity.predecessor_boundary_generation &&
           identity.successor_configuration == successor_configuration &&
           identity.successor_activation_generation == successor_generation &&
           identity.command_payload_digest == command_payload_digest &&
           identity.activation_delay_blocks == activation_delay_blocks;
}

std::optional<AdaptiveV3TransitionProjection>
make_adaptive_v3_transition_projection(
    const AdaptiveV3EpochChangeBundle &bundle,
    const EpochDefinition &current,
    std::uint64_t cycle_ordinal,
    const std::vector<std::pair<ReplicaID, PubKeyBLS>> &readiness_membership) noexcept
{
    try
    {
        const auto &command = bundle.command();
        const auto &definition = bundle.definition();
        const auto successor_generation = checked_activation_generation(
            command.payload.successor_epoch_number, 0);
        if (bundle.protocol_mode() != EpochProtocolMode::adaptive_v3 ||
            command.protocol_mode != EpochProtocolMode::adaptive_v3 ||
            !successor_generation || current.epoch_number() !=
                command.payload.successor_epoch_number - 1 ||
            current.epoch_digest() != command.payload.predecessor_epoch_digest ||
            definition.epoch_number != command.payload.successor_epoch_number ||
            !definition.epoch_digest || *definition.epoch_digest !=
                command.payload.successor_epoch_digest ||
            readiness_membership.empty() || current.trees().empty())
            return std::nullopt;
        for (std::size_t i = 1; i < readiness_membership.size(); ++i)
            if (readiness_membership[i - 1].first >= readiness_membership[i].first)
                return std::nullopt;
        std::vector<std::uint32_t> predecessor_tree_ids;
        predecessor_tree_ids.reserve(current.trees().size());
        for (const auto &tree : current.trees())
            predecessor_tree_ids.push_back(tree.tree_id);
        std::sort(
            predecessor_tree_ids.begin(), predecessor_tree_ids.end());
        if (std::adjacent_find(
                predecessor_tree_ids.begin(),
                predecessor_tree_ids.end()) != predecessor_tree_ids.end())
            return std::nullopt;
        return AdaptiveV3TransitionProjection{
            current.epoch_number(), current.epoch_digest(),
            std::move(predecessor_tree_ids),
            {definition.epoch_number, 0, *definition.epoch_digest},
            *successor_generation,
            canonical_activation_readiness_membership_digest(readiness_membership),
            epoch_change_payload_digest(command.payload),
            command.payload.activation_delay_blocks,
            DataStream(bundle.canonical_bytes()).get_hash(), cycle_ordinal};
    }
    catch (...) { return std::nullopt; }
}

namespace
{
void validate_config(
    const std::optional<ActivationReadyIdentityV1> &expected,
    const AdaptiveV3ManagerReadinessConfig &config)
{
    const auto member_count = config.membership.size();
    if (member_count < 4 || (member_count - 1) % 3 != 0)
        throw std::invalid_argument(
            "adaptive-v3 readiness membership is not a 3f+1 set");

    for (std::size_t i = 1; i < member_count; ++i)
    {
        if (config.membership[i - 1].first >= config.membership[i].first)
            throw std::invalid_argument(
                "adaptive-v3 readiness membership is not canonical");
    }

    const auto quorum = 2 * ((member_count - 1) / 3) + 1;
    if (config.required_release_count < quorum ||
        config.required_release_count > member_count)
    {
        throw std::invalid_argument(
            "adaptive-v3 readiness release count is outside [Q,N]");
    }

    const auto membership_digest = canonical_activation_readiness_membership_digest(
        config.membership);
    if ((expected && membership_digest != expected->membership_digest) ||
        (config.projection && membership_digest !=
            config.projection->membership_digest))
    {
        throw std::invalid_argument(
            "adaptive-v3 readiness membership digest does not match identity");
    }
}
} // namespace

struct AdaptiveV3ManagerReadinessCollector::State
{
    struct Candidate {
        ActivationReadyIdentityV1 identity;
        std::map<ReplicaID, ActivationReadyObservationV1> observations;
    };

    std::optional<ActivationReadyIdentityV1> expected;
    AdaptiveV3ManagerReadinessConfig config;

    // Node-based storage is intentional. ActivationReadyObservationV1 owns a
    // SigSecBLS whose legacy type is safely copy-constructible but not safely
    // assignable. A map both preserves canonical signer order and avoids the
    // element assignment performed by vector insertion/sorting.
    std::vector<Candidate> candidates;
    std::map<ReplicaID, ActivationReadyObservationV1> signer_observations;
    std::optional<std::size_t> bound_candidate;
    std::set<ReplicaID> quarantined;
    std::optional<ActivationReadinessCertificateV1> released_certificate;

    State(std::optional<ActivationReadyIdentityV1> value,
          AdaptiveV3ManagerReadinessConfig input)
        : expected(std::move(value)), config(std::move(input))
    {
        validate_config(expected, config);
    }

    const PubKeyBLS *key(ReplicaID id) const noexcept
    {
        const auto it = std::lower_bound(
            config.membership.begin(),
            config.membership.end(),
            id,
            [](const auto &member, ReplicaID value) {
                return member.first < value;
            });
        return it == config.membership.end() || it->first != id
                   ? nullptr
                   : &it->second;
    }
};

AdaptiveV3ManagerReadinessCollector::AdaptiveV3ManagerReadinessCollector(
    ActivationReadyIdentityV1 expected,
    AdaptiveV3ManagerReadinessConfig config)
    : state_(new State(std::move(expected), std::move(config)))
{}

AdaptiveV3ManagerReadinessCollector::AdaptiveV3ManagerReadinessCollector(
    AdaptiveV3TransitionProjection projection,
    AdaptiveV3ManagerReadinessConfig config)
    : state_(new State(std::nullopt, [&] {
          if (config.projection && config.projection->canonical_bundle_digest !=
                  projection.canonical_bundle_digest)
              throw std::invalid_argument("adaptive-v3 readiness projection mismatch");
          config.projection.emplace(std::move(projection));
          return std::move(config);
      }()))
{}

AdaptiveV3ManagerReadinessCollector::~AdaptiveV3ManagerReadinessCollector() =
    default;

AdaptiveV3ManagerReadinessDisposition
AdaptiveV3ManagerReadinessCollector::ingest(
    ReplicaID peer,
    const ActivationReadyObservationV1 &observation) noexcept
{
    try
    {
        auto &state = *state_;
        if (peer != observation.signer_replica_id)
            return AdaptiveV3ManagerReadinessDisposition::
                rejected_peer_binding;

        const auto *public_key = state.key(peer);
        if (public_key == nullptr)
            return AdaptiveV3ManagerReadinessDisposition::rejected_nonmember;
        if (state.quarantined.count(peer) != 0)
            return AdaptiveV3ManagerReadinessDisposition::quarantined;
        if (!verify_activation_ready_observation(observation, *public_key))
            return AdaptiveV3ManagerReadinessDisposition::
                rejected_invalid_observation;

        const auto existing = state.signer_observations.find(peer);
        if (existing != state.signer_observations.end())
        {
            if (activation_ready_observation_digest(existing->second) ==
                activation_ready_observation_digest(observation))
                return AdaptiveV3ManagerReadinessDisposition::duplicate;

            for (auto &candidate : state.candidates)
                candidate.observations.erase(peer);
            state.signer_observations.erase(existing);
            state.quarantined.insert(peer);
            return AdaptiveV3ManagerReadinessDisposition::rejected_conflict;
        }

        if (state.expected && observation.identity != *state.expected)
            return AdaptiveV3ManagerReadinessDisposition::
                rejected_wrong_identity;
        if (state.config.projection &&
            !state.config.projection->matches_static(observation.identity))
            return AdaptiveV3ManagerReadinessDisposition::
                rejected_wrong_identity;

        if (state.released_certificate)
            return AdaptiveV3ManagerReadinessDisposition::released;

        auto candidate = std::find_if(state.candidates.begin(),
            state.candidates.end(), [&](const auto &entry) {
                return !(entry.identity != observation.identity);
            });
        if (candidate == state.candidates.end()) {
            // At most N dynamic candidates: a Byzantine spray cannot make
            // manager memory unbounded.
            if (state.candidates.size() >= state.config.membership.size())
                return AdaptiveV3ManagerReadinessDisposition::rejected_wrong_identity;
            state.candidates.push_back(State::Candidate{observation.identity, {}});
            candidate = std::prev(state.candidates.end());
        }
        candidate->observations.emplace(peer, observation);
        state.signer_observations.emplace(peer, observation);

        const auto candidate_index = static_cast<std::size_t>(
            std::distance(state.candidates.begin(), candidate));

        const auto quorum = 2 * ((state.config.membership.size() - 1) / 3) + 1;
        if (!state.bound_candidate &&
            candidate->observations.size() >= quorum)
        {
            std::vector<ActivationReadyObservationV1> q_observations;
            q_observations.reserve(candidate->observations.size());
            for (const auto &entry : candidate->observations)
                q_observations.push_back(entry.second);
            auto q_certificate = make_activation_readiness_certificate(
                candidate->identity, std::move(q_observations));
            if (verify_activation_readiness_certificate(
                    q_certificate, candidate->identity,
                    candidate->identity.membership_digest,
                    state.config.membership))
                state.bound_candidate = candidate_index;
        }

        if (!state.bound_candidate || *state.bound_candidate != candidate_index ||
            candidate->observations.size() < state.config.required_release_count)
            return AdaptiveV3ManagerReadinessDisposition::accepted;

        std::vector<ActivationReadyObservationV1> canonical_observations;
        canonical_observations.reserve(candidate->observations.size());
        for (const auto &entry : candidate->observations)
            canonical_observations.push_back(entry.second);

        auto certificate = make_activation_readiness_certificate(
            candidate->identity, std::move(canonical_observations));
        if (!verify_activation_readiness_certificate(
                certificate,
                candidate->identity,
                candidate->identity.membership_digest,
                state.config.membership))
        {
            return AdaptiveV3ManagerReadinessDisposition::accepted;
        }

        state.released_certificate.emplace(std::move(certificate));
        return AdaptiveV3ManagerReadinessDisposition::released;
    }
    catch (...)
    {
        return AdaptiveV3ManagerReadinessDisposition::
            rejected_invalid_observation;
    }
}

const ActivationReadinessCertificateV1 *
AdaptiveV3ManagerReadinessCollector::certificate() const noexcept
{
    return state_->released_certificate ? &*state_->released_certificate
                                        : nullptr;
}

bool AdaptiveV3ManagerReadinessCollector::released() const noexcept
{
    return state_->released_certificate.has_value();
}

std::size_t
AdaptiveV3ManagerReadinessCollector::accepted_count() const noexcept
{
    if (state_->bound_candidate) {
        return state_->candidates[*state_->bound_candidate].observations.size();
    }
    std::size_t count = 0;
    for (const auto &candidate : state_->candidates)
        count += candidate.observations.size();
    return count;
}

bool AdaptiveV3ManagerReadinessCollector::quarantined(
    ReplicaID id) const noexcept
{
    return state_->quarantined.count(id) != 0;
}
} // namespace hotstuff
