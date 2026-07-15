/**
 * Exact-key storage for proposals received before their configuration is
 * active.
 */

#ifndef HOTSTUFF_FUTURE_PROPOSAL_BUFFER_H_INCLUDED
#define HOTSTUFF_FUTURE_PROPOSAL_BUFFER_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <set>
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
};

class FutureProposalBuffer final
{
public:
    bool insert(BufferedProposal proposal);
    bool contains(const ProposalKey &key) const;
    const BufferedProposal *first_unclaimed(
        const ConfigurationId &configuration,
        const std::set<ProposalKey> &claimed) const noexcept;
    bool erase(const ProposalKey &key);
    std::size_t size() const noexcept;

    std::vector<BufferedProposal> drain(
        const ConfigurationId &configuration);
    std::size_t purge(const ConfigurationId &configuration);
    std::size_t purge_before_epoch(
        std::uint32_t first_live_epoch);

private:
    std::vector<BufferedProposal> proposals_;
    std::set<ProposalKey> keys_;
};

} // namespace hotstuff

#endif
