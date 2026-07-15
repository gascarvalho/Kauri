#include "hotstuff/proposal_admission.h"

#include <utility>

namespace hotstuff
{
namespace
{

bool is_zero(const uint256_t &value)
{
    return value == uint256_t{};
}

ProposalAdmissionResult result(
    ProposalDisposition disposition,
    const ProposalKey &key)
{
    return ProposalAdmissionResult{disposition, key};
}

template<typename Predicate>
std::size_t erase_keys_if(
    std::set<ProposalKey> &keys,
    Predicate predicate)
{
    std::size_t erased = 0;
    for (auto key = keys.begin(); key != keys.end();)
    {
        if (!predicate(*key))
        {
            ++key;
            continue;
        }
        key = keys.erase(key);
        ++erased;
    }
    return erased;
}

} // namespace

ProposalAdmissionCoordinator::ProposalAdmissionCoordinator(
    const EpochStore &epochs,
    ConfigurationId active_configuration,
    FutureProposalBuffer &future_proposals,
    ProposalAdmissionEffects &effects)
    : epochs_(epochs),
      active_configuration_(std::move(active_configuration)),
      future_proposals_(future_proposals),
      effects_(effects)
{
}

ProposalAdmissionResult ProposalAdmissionCoordinator::validate(
    const BufferedProposal &proposal) const
{
    const auto &metadata = proposal.metadata;
    const auto &configuration = metadata.configuration;
    const auto key = metadata.key();

    if (is_zero(configuration.epoch_digest) ||
        is_zero(metadata.block_hash))
    {
        return result(ProposalDisposition::rejected_malformed, key);
    }

    const auto *epoch = epochs_.find_epoch(configuration.epoch_number);
    if (epoch == nullptr)
    {
        return result(
            ProposalDisposition::rejected_unknown_configuration, key);
    }
    if (epoch->epoch_digest() != configuration.epoch_digest)
    {
        return result(ProposalDisposition::rejected_digest_mismatch, key);
    }

    const auto *tree = epochs_.find_tree(
        configuration.epoch_number, configuration.tree_id);
    if (tree == nullptr || tree->members_breadth_first.empty())
    {
        return result(
            ProposalDisposition::rejected_unknown_configuration, key);
    }
    if (is_configuration_retired(configuration) ||
        configuration.epoch_number < active_configuration_.epoch_number)
    {
        return result(
            ProposalDisposition::rejected_stale_configuration, key);
    }
    if (metadata.proposer != tree->members_breadth_first.front())
    {
        return result(ProposalDisposition::rejected_invalid_proposer, key);
    }

    return result(ProposalDisposition::admitted_active, key);
}

ProposalAdmissionResult ProposalAdmissionCoordinator::receive(
    BufferedProposal proposal)
{
    const auto validation = validate(proposal);
    if (validation.disposition != ProposalDisposition::admitted_active)
    {
        return validation;
    }

    const auto key = proposal.metadata.key();
    if (!received_.insert(key).second)
    {
        return result(ProposalDisposition::duplicate, key);
    }

    effects_.relay_once(proposal);
    if (proposal.metadata.configuration != active_configuration_)
    {
        if (!future_proposals_.insert(std::move(proposal)))
        {
            return result(ProposalDisposition::duplicate, key);
        }
        return result(ProposalDisposition::buffered_future, key);
    }

    admitted_.insert(key);
    effects_.process_active(proposal);
    return result(ProposalDisposition::admitted_active, key);
}

std::vector<ProposalAdmissionResult>
ProposalAdmissionCoordinator::activate(
    const ConfigurationId &configuration)
{
    if (!activate_without_draining(configuration))
    {
        return {};
    }

    auto proposals = future_proposals_.drain(configuration);
    std::vector<ProposalAdmissionResult> results;
    results.reserve(proposals.size());
    for (const auto &proposal : proposals)
    {
        const auto key = proposal.metadata.key();
        if (!admitted_.insert(key).second)
        {
            continue;
        }
        effects_.process_active(proposal);
        results.push_back(
            result(ProposalDisposition::admitted_active, key));
    }
    return results;
}

bool ProposalAdmissionCoordinator::activate_without_draining(
    const ConfigurationId &configuration) noexcept
{
    if (!is_known_exact_configuration(configuration) ||
        is_configuration_retired(configuration) ||
        configuration.epoch_number < active_configuration_.epoch_number)
        return false;

    active_configuration_ = configuration;
    return true;
}

bool ProposalAdmissionCoordinator::process_claimed_active(
    const BufferedProposal &proposal)
{
    const auto validation = validate(proposal);
    if (validation.disposition != ProposalDisposition::admitted_active ||
        proposal.metadata.configuration != active_configuration_)
        return false;

    const auto key = proposal.metadata.key();
    if (admitted_.count(key) != 0)
        return true;

    const auto inserted = admitted_.insert(key);
    try
    {
        effects_.process_active(proposal);
    }
    catch (...)
    {
        admitted_.erase(inserted.first);
        throw;
    }
    return true;
}

bool ProposalAdmissionCoordinator::authorize_local_vote(
    const ProposalKey &key)
{
    if (admitted_.find(key) == admitted_.end() ||
        !locally_authorized_.insert(key).second)
    {
        return false;
    }

    effects_.local_vote_authorized(key);
    return true;
}

bool ProposalAdmissionCoordinator::retire_proposal(
    const ProposalKey &key)
{
    bool retired = future_proposals_.erase(key);
    retired = admitted_.erase(key) != 0 || retired;
    retired = locally_authorized_.erase(key) != 0 || retired;
    return retired;
}

std::size_t ProposalAdmissionCoordinator::retire_configuration(
    const ConfigurationId &configuration)
{
    if (configuration.epoch_number < first_live_epoch_ ||
        !retired_configurations_.insert(configuration).second)
        return 0;

    const auto matches = [&configuration](const ProposalKey &key) {
        return key.configuration == configuration;
    };
    const auto retired = erase_keys_if(received_, matches);
    erase_keys_if(admitted_, matches);
    erase_keys_if(locally_authorized_, matches);
    future_proposals_.purge(configuration);
    return retired;
}

std::size_t ProposalAdmissionCoordinator::advance_retirement_floor(
    std::uint32_t first_live_epoch)
{
    if (first_live_epoch <= first_live_epoch_)
        return 0;
    first_live_epoch_ = first_live_epoch;

    const auto below_floor = [first_live_epoch](const ProposalKey &key) {
        return key.configuration.epoch_number < first_live_epoch;
    };
    erase_keys_if(received_, below_floor);
    erase_keys_if(admitted_, below_floor);
    erase_keys_if(locally_authorized_, below_floor);
    future_proposals_.purge_before_epoch(first_live_epoch);

    std::size_t compacted_tombstones = 0;
    for (auto configuration = retired_configurations_.begin();
         configuration != retired_configurations_.end();)
    {
        if (configuration->epoch_number >= first_live_epoch_)
        {
            ++configuration;
            continue;
        }
        configuration = retired_configurations_.erase(configuration);
        ++compacted_tombstones;
    }
    return compacted_tombstones;
}

bool ProposalAdmissionCoordinator::contains_admitted(
    const ProposalKey &key) const
{
    return admitted_.count(key) != 0;
}

ProposalAdmissionStorageStats
ProposalAdmissionCoordinator::storage_stats() const
{
    return ProposalAdmissionStorageStats{
        received_.size(),
        admitted_.size(),
        locally_authorized_.size(),
        retired_configurations_.size()};
}

const ConfigurationId &
ProposalAdmissionCoordinator::active_configuration() const noexcept
{
    return active_configuration_;
}

bool ProposalAdmissionCoordinator::is_known_exact_configuration(
    const ConfigurationId &configuration) const noexcept
{
    if (configuration.epoch_digest == uint256_t{})
    {
        return false;
    }
    const auto *epoch = epochs_.find_epoch(configuration.epoch_number);
    if (epoch == nullptr || epoch->epoch_digest() != configuration.epoch_digest)
    {
        return false;
    }
    return epochs_.find_tree(
               configuration.epoch_number, configuration.tree_id) != nullptr;
}

bool ProposalAdmissionCoordinator::is_configuration_retired(
    const ConfigurationId &configuration) const noexcept
{
    return configuration.epoch_number < first_live_epoch_ ||
           retired_configurations_.count(configuration) != 0;
}

} // namespace hotstuff
