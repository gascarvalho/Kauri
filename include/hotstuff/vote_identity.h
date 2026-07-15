/**
 * Exact, domain-separated identity for votes and quorum certificates.
 */

#ifndef HOTSTUFF_VOTE_IDENTITY_H_INCLUDED
#define HOTSTUFF_VOTE_IDENTITY_H_INCLUDED

#include "hotstuff/configuration.h"

namespace hotstuff
{

bytearray_t canonical_serialize_exact_vote(const ProposalKey &key);

uint256_t exact_vote_authentication_digest(const ProposalKey &key);

void serialize_proposal_key(DataStream &stream, const ProposalKey &key);

void unserialize_proposal_key(DataStream &stream, ProposalKey &key);

/**
 * Genesis is not produced by a proposal and therefore has no normal proposal
 * configuration.  This is the sole explicit construction path for its
 * certification identity.
 */
ProposalKey genesis_certification_key(const uint256_t &genesis_block_hash);

} // namespace hotstuff

#endif
