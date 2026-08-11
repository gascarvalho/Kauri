/**
 * Exact-key storage for proposals received before their configuration is
 * active.
 */

#ifndef HOTSTUFF_FUTURE_PROPOSAL_BUFFER_H_INCLUDED
#define HOTSTUFF_FUTURE_PROPOSAL_BUFFER_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <map>
#include <optional>
#include <set>
#include <utility>
#include <vector>

#include "hotstuff/configuration.h"

namespace hotstuff
{

struct ProposalMetadata
{
    ConfigurationId configuration;
    uint256_t block_hash;
    ReplicaID proposer{0};

    ProposalKey key() const;
    void serialize(DataStream &stream) const;
    void unserialize(DataStream &stream);
};

struct BufferedProposal
{
    ProposalMetadata metadata;
    bytearray_t wire_payload;

    // The authenticated immediate sender is transport-local state. It is not
    // part of the proposal identity or serialized proposal metadata.
    PeerId source_peer;

    // Adaptive ingress binds buffered bytes to one exact view generation.
    // Zero means the proposal used the legacy/static representation.
    std::uint64_t view_generation{0};
    uint256_t wire_digest;

    // The adaptive envelope is relayed byte-for-byte through wire_payload,
    // while normal HotStuff processing consumes only this decoded body.
    bytearray_t processing_payload;

    // Authenticated transport provenance for the immediate proposal sender.
    // This is local evidence metadata only: it is neither serialized nor part
    // of ProposalKey. Adaptive-v2 uses it to distinguish the ordinary
    // physical-parent hop from an authenticated root repair delivery.
    std::optional<ReplicaID> authenticated_proposal_source_replica;
};

struct FutureProposalBufferLimits
{
    std::size_t max_entries{4096};
    std::size_t max_wire_bytes{64U * 1024U * 1024U};
    std::size_t max_entries_per_configuration_generation{64};
    std::size_t max_wire_bytes_per_configuration_generation{
        16U * 1024U * 1024U};
};

enum class FutureProposalInsertDisposition
{
    inserted,
    duplicate,
    rejected_capacity
};

struct FutureProposalInsertResult
{
    FutureProposalInsertDisposition disposition;

    // Preserve source compatibility for existing insertion-only adapters.
    operator bool() const noexcept
    {
        return disposition == FutureProposalInsertDisposition::inserted;
    }
};

class FutureProposalBuffer final
{
public:
    explicit FutureProposalBuffer(
        FutureProposalBufferLimits limits = {});

    FutureProposalInsertResult insert(BufferedProposal proposal);
    bool contains(const ProposalKey &key) const;
    const BufferedProposal *first_unclaimed(
        const ConfigurationId &configuration,
        const std::set<ProposalKey> &claimed) const noexcept;
    bool erase(const ProposalKey &key) noexcept;
    std::size_t size() const noexcept;

    std::vector<BufferedProposal> drain(
        const ConfigurationId &configuration);
    std::size_t purge(
        const ConfigurationId &configuration) noexcept;
    std::size_t purge_before_epoch(
        std::uint32_t first_live_epoch) noexcept;

private:
    using ConfigurationGeneration =
        std::pair<ConfigurationId, std::uint64_t>;

    struct BucketUsage
    {
        std::size_t entries{0};
        std::size_t wire_bytes{0};
    };

    void release(const BufferedProposal &proposal) noexcept;

    FutureProposalBufferLimits limits_;
    std::vector<BufferedProposal> proposals_;
    std::set<ProposalKey> keys_;
    std::map<ConfigurationGeneration, BucketUsage> bucket_usage_;
    std::size_t retained_wire_bytes_{0};
};

} // namespace hotstuff

#endif
