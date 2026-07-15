#include "hotstuff/future_proposal_buffer.h"

#include <utility>

namespace hotstuff
{

ProposalKey ProposalMetadata::key() const
{
    return ProposalKey{configuration, block_hash};
}

void ProposalMetadata::serialize(DataStream &stream) const
{
    stream << configuration.epoch_number
           << configuration.tree_id
           << configuration.epoch_digest
           << block_hash
           << proposer;
}

void ProposalMetadata::unserialize(DataStream &stream)
{
    stream >> configuration.epoch_number
           >> configuration.tree_id
           >> configuration.epoch_digest
           >> block_hash
           >> proposer;
}

bool FutureProposalBuffer::insert(BufferedProposal proposal)
{
    const auto key = proposal.metadata.key();
    if (!keys_.insert(key).second)
    {
        return false;
    }

    proposals_.push_back(std::move(proposal));
    return true;
}

bool FutureProposalBuffer::contains(const ProposalKey &key) const
{
    return keys_.find(key) != keys_.end();
}

const BufferedProposal *FutureProposalBuffer::first_unclaimed(
    const ConfigurationId &configuration,
    const std::set<ProposalKey> &claimed) const noexcept
{
    for (const auto &proposal : proposals_)
        if (proposal.metadata.configuration == configuration &&
            claimed.count(proposal.metadata.key()) == 0)
            return &proposal;
    return nullptr;
}

bool FutureProposalBuffer::erase(const ProposalKey &key)
{
    if (keys_.erase(key) == 0)
        return false;

    for (auto proposal = proposals_.begin();
         proposal != proposals_.end();
         ++proposal)
    {
        if (proposal->metadata.key() != key)
            continue;
        proposals_.erase(proposal);
        return true;
    }
    return false;
}

std::size_t FutureProposalBuffer::size() const noexcept
{
    return proposals_.size();
}

std::vector<BufferedProposal> FutureProposalBuffer::drain(
    const ConfigurationId &configuration)
{
    std::vector<BufferedProposal> selected;
    std::vector<BufferedProposal> retained;
    selected.reserve(proposals_.size());
    retained.reserve(proposals_.size());

    for (auto &proposal : proposals_)
    {
        if (proposal.metadata.configuration == configuration)
        {
            keys_.erase(proposal.metadata.key());
            selected.push_back(std::move(proposal));
        }
        else
        {
            retained.push_back(std::move(proposal));
        }
    }
    proposals_ = std::move(retained);
    return selected;
}

std::size_t FutureProposalBuffer::purge(
    const ConfigurationId &configuration)
{
    return drain(configuration).size();
}

std::size_t FutureProposalBuffer::purge_before_epoch(
    std::uint32_t first_live_epoch)
{
    std::vector<BufferedProposal> retained;
    retained.reserve(proposals_.size());
    std::size_t purged = 0;
    for (auto &proposal : proposals_)
    {
        if (proposal.metadata.configuration.epoch_number <
            first_live_epoch)
        {
            keys_.erase(proposal.metadata.key());
            ++purged;
            continue;
        }
        retained.push_back(std::move(proposal));
    }
    proposals_ = std::move(retained);
    return purged;
}

} // namespace hotstuff
