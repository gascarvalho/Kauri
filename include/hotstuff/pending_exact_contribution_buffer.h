/**
 * Bounded ownership buffer for exact contributions received while an
 * admitted proposal is waiting for its runtime context to open.
 */

#ifndef HOTSTUFF_PENDING_EXACT_CONTRIBUTION_BUFFER_H_INCLUDED
#define HOTSTUFF_PENDING_EXACT_CONTRIBUTION_BUFFER_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <map>
#include <mutex>
#include <vector>

#include "hotstuff/exact_vote_handler.h"

namespace hotstuff
{

struct PendingExactContribution
{
    ExactContributionKind kind;
    ExactContributionEnvelope envelope;
    PeerId authenticated_source;
    uint256_t fingerprint;
    // CLOCK_MONOTONIC_RAW observation captured after authenticated wire
    // decoding and before worker verification. Experiment-only observers may
    // consume it; normal consensus ordering never depends on this field.
    std::uint64_t received_ns{0};
};

struct PendingExactContributionBufferLimits
{
    std::size_t global_capacity;
    std::size_t per_key_capacity;
};

enum class PendingExactContributionInsertResult
{
    inserted,
    duplicate,
    per_key_full,
    global_full
};

class PendingExactContributionBuffer final
{
public:
    explicit PendingExactContributionBuffer(
        PendingExactContributionBufferLimits limits);

    PendingExactContributionInsertResult insert(
        PendingExactContribution contribution);

    std::size_t size() const noexcept;
    std::size_t size(const ProposalKey &key) const noexcept;

    std::vector<PendingExactContribution> drain(const ProposalKey &key);
    std::size_t purge(const ProposalKey &key);
    std::size_t purge_block(const uint256_t &block_hash);
    std::size_t purge_configuration(
        const ConfigurationId &configuration);
    std::size_t purge_epoch(std::uint32_t epoch_number);
    std::size_t purge_before_epoch(std::uint32_t first_live_epoch);
    void clear() noexcept;

private:
    PendingExactContributionBufferLimits limits_;
    mutable std::mutex mutex_;
    std::map<ProposalKey, std::vector<PendingExactContribution>> entries_;
    std::size_t size_{0};
};

} // namespace hotstuff

#endif
