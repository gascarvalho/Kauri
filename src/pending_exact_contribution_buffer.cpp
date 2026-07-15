#include "hotstuff/pending_exact_contribution_buffer.h"

#include <algorithm>
#include <utility>

namespace hotstuff
{
namespace
{

template<typename Predicate>
std::size_t purge_matching(
    std::map<ProposalKey, std::vector<PendingExactContribution>> &entries,
    std::size_t &retained,
    Predicate matches)
{
    std::size_t purged = 0;
    for (auto item = entries.begin(); item != entries.end();)
    {
        if (!matches(item->first))
        {
            ++item;
            continue;
        }

        purged += item->second.size();
        item = entries.erase(item);
    }
    retained -= purged;
    return purged;
}

} // namespace

PendingExactContributionBuffer::PendingExactContributionBuffer(
    PendingExactContributionBufferLimits limits)
    : limits_(limits)
{}

PendingExactContributionInsertResult
PendingExactContributionBuffer::insert(
    PendingExactContribution contribution)
{
    const auto key = contribution.envelope.message_key;
    std::lock_guard<std::mutex> lock(mutex_);
    auto &bucket = entries_[key];

    const auto duplicate = std::find_if(
        bucket.begin(), bucket.end(),
        [&contribution](const PendingExactContribution &existing) {
            return existing.fingerprint == contribution.fingerprint;
        });
    if (duplicate != bucket.end())
        return PendingExactContributionInsertResult::duplicate;
    if (bucket.size() >= limits_.per_key_capacity)
    {
        if (bucket.empty())
            entries_.erase(key);
        return PendingExactContributionInsertResult::per_key_full;
    }
    if (size_ >= limits_.global_capacity)
    {
        if (bucket.empty())
            entries_.erase(key);
        return PendingExactContributionInsertResult::global_full;
    }

    bucket.push_back(std::move(contribution));
    ++size_;
    return PendingExactContributionInsertResult::inserted;
}

std::size_t PendingExactContributionBuffer::size() const noexcept
{
    std::lock_guard<std::mutex> lock(mutex_);
    return size_;
}

std::size_t PendingExactContributionBuffer::size(
    const ProposalKey &key) const noexcept
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(key);
    return found == entries_.end() ? 0 : found->second.size();
}

std::vector<PendingExactContribution>
PendingExactContributionBuffer::drain(const ProposalKey &key)
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(key);
    if (found == entries_.end())
        return {};

    auto drained = std::move(found->second);
    size_ -= drained.size();
    entries_.erase(found);
    return drained;
}

std::size_t PendingExactContributionBuffer::purge(
    const ProposalKey &key)
{
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = entries_.find(key);
    if (found == entries_.end())
        return 0;

    const auto purged = found->second.size();
    size_ -= purged;
    entries_.erase(found);
    return purged;
}

std::size_t PendingExactContributionBuffer::purge_block(
    const uint256_t &block_hash)
{
    std::lock_guard<std::mutex> lock(mutex_);
    return purge_matching(
        entries_, size_, [&block_hash](const ProposalKey &key) {
            return key.block_hash == block_hash;
        });
}

std::size_t PendingExactContributionBuffer::purge_configuration(
    const ConfigurationId &configuration)
{
    std::lock_guard<std::mutex> lock(mutex_);
    return purge_matching(
        entries_, size_, [&configuration](const ProposalKey &key) {
            return key.configuration == configuration;
        });
}

std::size_t PendingExactContributionBuffer::purge_epoch(
    std::uint32_t epoch_number)
{
    std::lock_guard<std::mutex> lock(mutex_);
    return purge_matching(
        entries_, size_, [epoch_number](const ProposalKey &key) {
            return key.configuration.epoch_number == epoch_number;
        });
}

std::size_t PendingExactContributionBuffer::purge_before_epoch(
    std::uint32_t first_live_epoch)
{
    std::lock_guard<std::mutex> lock(mutex_);
    return purge_matching(
        entries_, size_, [first_live_epoch](const ProposalKey &key) {
            return key.configuration.epoch_number < first_live_epoch;
        });
}

void PendingExactContributionBuffer::clear() noexcept
{
    std::lock_guard<std::mutex> lock(mutex_);
    entries_.clear();
    size_ = 0;
}

} // namespace hotstuff
