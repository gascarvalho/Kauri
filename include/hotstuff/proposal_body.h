/**
 * Detached structural validation for proposal wire payloads.
 */

#ifndef HOTSTUFF_PROPOSAL_BODY_H_INCLUDED
#define HOTSTUFF_PROPOSAL_BODY_H_INCLUDED

#include <optional>

#include "hotstuff/proposal_admission.h"

namespace hotstuff
{

class HotStuffCore;

/** Decode and validate a proposal body without mutating EntityStorage. */
std::optional<ProposalMetadata> decode_detached_proposal_body(
    const bytearray_t &wire_payload,
    HotStuffCore &structural_decoder) noexcept;

/**
 * Validate one complete proposal payload before entering proposal admission.
 *
 * Structural decoding is detached from EntityStorage and consensus effects.
 * The coordinator is called exactly once, and only after the payload has been
 * consumed completely and its decoded block hash matches the wire metadata.
 */
ProposalAdmissionResult admit_proposal_payload(
    const bytearray_t &wire_payload,
    const PeerId &source_peer,
    HotStuffCore &structural_decoder,
    ProposalAdmissionCoordinator &coordinator);

} // namespace hotstuff

#endif
