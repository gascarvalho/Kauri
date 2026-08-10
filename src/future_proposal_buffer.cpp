#include "hotstuff/future_proposal_buffer.h"

#include <algorithm>
#include <type_traits>
#include <utility>

namespace hotstuff
{
namespace
{

static_assert(
    std::is_nothrow_move_assignable<BufferedProposal>::value,
    "future proposal erase requires non-throwing in-place compaction");

bool would_exceed(std::size_t current,
                  std::size_t addition,
                  std::size_t limit) noexcept
{
    return current > limit || addition > limit - current;
}

} // namespace

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

FutureProposalBuffer::FutureProposalBuffer(
    FutureProposalBufferLimits limits)
    : limits_(limits)
{
}

FutureProposalInsertResult FutureProposalBuffer::insert(
    BufferedProposal proposal)
{
    const auto key = proposal.metadata.key();
    if (keys_.find(key) != keys_.end())
    {
        return {FutureProposalInsertDisposition::duplicate};
    }

    const ConfigurationGeneration bucket_key{
        proposal.metadata.configuration, proposal.view_generation};
    const auto bucket = bucket_usage_.find(bucket_key);
    const BucketUsage empty_usage;
    const auto &usage =
        bucket == bucket_usage_.end() ? empty_usage : bucket->second;
    const auto wire_bytes = proposal.wire_payload.size();
    if (would_exceed(proposals_.size(), 1, limits_.max_entries) ||
        would_exceed(
            retained_wire_bytes_, wire_bytes, limits_.max_wire_bytes) ||
        would_exceed(
            usage.entries,
            1,
            limits_.max_entries_per_configuration_generation) ||
        would_exceed(
            usage.wire_bytes,
            wire_bytes,
            limits_.max_wire_bytes_per_configuration_generation))
    {
        return {FutureProposalInsertDisposition::rejected_capacity};
    }

    proposals_.push_back(std::move(proposal));
    try
    {
        if (!keys_.insert(key).second)
        {
            proposals_.pop_back();
            return {FutureProposalInsertDisposition::duplicate};
        }
        try
        {
            auto inserted_bucket =
                bucket_usage_.try_emplace(bucket_key).first;
            ++inserted_bucket->second.entries;
            inserted_bucket->second.wire_bytes += wire_bytes;
            retained_wire_bytes_ += wire_bytes;
        }
        catch (...)
        {
            keys_.erase(key);
            proposals_.pop_back();
            throw;
        }
    }
    catch (...)
    {
        if (!proposals_.empty() &&
            proposals_.back().metadata.key() == key)
            proposals_.pop_back();
        throw;
    }
    return {FutureProposalInsertDisposition::inserted};
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

bool FutureProposalBuffer::erase(const ProposalKey &key) noexcept
{
    const auto retained_key = keys_.find(key);
    if (retained_key == keys_.end())
        return false;

    const auto removed = std::find_if(
        proposals_.begin(),
        proposals_.end(),
        [&key](const BufferedProposal &proposal) {
            return proposal.metadata.key() == key;
        });
    if (removed == proposals_.end())
        return false;

    release(*removed);
    keys_.erase(retained_key);
    proposals_.erase(removed);
    return true;
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

    for (const auto &proposal : proposals_)
    {
        if (proposal.metadata.configuration == configuration)
            selected.push_back(proposal);
        else
            retained.push_back(proposal);
    }
    for (const auto &proposal : proposals_)
    {
        if (proposal.metadata.configuration != configuration)
            continue;
        release(proposal);
        keys_.erase(proposal.metadata.key());
    }
    proposals_ = std::move(retained);
    return selected;
}

std::size_t FutureProposalBuffer::purge(
    const ConfigurationId &configuration) noexcept
{
    std::size_t retained = 0;
    std::size_t purged = 0;
    for (std::size_t index = 0; index < proposals_.size(); ++index)
    {
        auto &proposal = proposals_[index];
        if (proposal.metadata.configuration == configuration)
        {
            release(proposal);
            keys_.erase(proposal.metadata.key());
            ++purged;
            continue;
        }
        if (retained != index)
            proposals_[retained] = std::move(proposal);
        ++retained;
    }
    proposals_.resize(retained);
    return purged;
}

std::size_t FutureProposalBuffer::purge_before_epoch(
    std::uint32_t first_live_epoch) noexcept
{
    std::size_t retained = 0;
    std::size_t purged = 0;
    for (std::size_t index = 0; index < proposals_.size(); ++index)
    {
        auto &proposal = proposals_[index];
        if (proposal.metadata.configuration.epoch_number <
            first_live_epoch)
        {
            release(proposal);
            keys_.erase(proposal.metadata.key());
            ++purged;
            continue;
        }
        if (retained != index)
            proposals_[retained] = std::move(proposal);
        ++retained;
    }
    proposals_.resize(retained);
    return purged;
}

void FutureProposalBuffer::release(
    const BufferedProposal &proposal) noexcept
{
    const ConfigurationGeneration bucket_key{
        proposal.metadata.configuration, proposal.view_generation};
    const auto bucket = bucket_usage_.find(bucket_key);
    if (bucket != bucket_usage_.end())
    {
        const auto wire_bytes = proposal.wire_payload.size();
        bucket->second.entries -= 1;
        bucket->second.wire_bytes -= wire_bytes;
        retained_wire_bytes_ -= wire_bytes;
        if (bucket->second.entries == 0)
            bucket_usage_.erase(bucket);
    }
}

} // namespace hotstuff
