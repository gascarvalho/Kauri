/**
 * Canonical bounded wire envelope for proposal-lifecycle facts.
 */

#ifndef HOTSTUFF_EVIDENCE_LIFECYCLE_WIRE_H_INCLUDED
#define HOTSTUFF_EVIDENCE_LIFECYCLE_WIRE_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>

#include "hotstuff/epoch_change_bundle.h"
#include "hotstuff/evidence_ingress.h"
#include "hotstuff/evidence_lifecycle.h"

namespace hotstuff
{

struct ProposalLifecycleWireLimits
{
    std::size_t maximum_payload_bytes{512};
};

enum class ProposalLifecycleFactTag : std::uint8_t
{
    normal_runtime_initialized = 1,
    runtime_aborted = 2,
    committed = 3,
    configuration_retired = 4,
    retirement_floor_advanced = 5,
};

enum class ProposalLifecycleWireError : std::uint8_t
{
    none = 0,
    invalid_limits,
    payload_too_large,
    truncated,
    trailing_bytes,
    invalid_domain,
    unsupported_schema,
    invalid_fact_tag,
    zero_sequence,
    invalid_configuration_identity,
    invalid_proposal_identity,
    invalid_retirement_floor,
    noncanonical_encoding,
    integer_overflow,
    allocation_failure,
    internal_failure,
};

struct ProposalLifecycleDecodeResult
{
    ProposalLifecycleWireError error{ProposalLifecycleWireError::none};
    std::optional<ProposalLifecycleNotice> notice;

    explicit operator bool() const noexcept
    {
        return error == ProposalLifecycleWireError::none &&
               notice.has_value();
    }
};

const std::string &proposal_lifecycle_notice_domain() noexcept;

/**
 * Encode one lifecycle claim in canonical big-endian form.
 *
 * source_replica_id remains an unauthenticated claim in these bytes. The
 * transport owner must compare it with the configured mutual-TLS identity
 * before applying the decoded notice.
 */
bytearray_t encode_proposal_lifecycle_notice(
    const ProposalLifecycleNotice &notice,
    const ProposalLifecycleWireLimits &limits);

/**
 * Decode structural wire data only. Success grants no proposal, evidence,
 * quorum, topology, or authentication authority.
 */
ProposalLifecycleDecodeResult decode_proposal_lifecycle_notice(
    const bytearray_t &payload,
    const ProposalLifecycleWireLimits &limits) noexcept;

struct MsgProposalLifecycleNotice
{
    static const opcode_t opcode = 0x1A;
    DataStream serialized;

    MsgProposalLifecycleNotice(
        const ProposalLifecycleNotice &notice,
        const ProposalLifecycleWireLimits &limits);
    explicit MsgProposalLifecycleNotice(DataStream &&serialized_payload);
};

static_assert(
    MsgProposalLifecycleNotice::opcode != MsgEvidenceReport::opcode &&
        MsgProposalLifecycleNotice::opcode !=
            MsgStageEpochDefinition::opcode &&
        MsgProposalLifecycleNotice::opcode != MsgStageAck::opcode &&
        MsgProposalLifecycleNotice::opcode != MsgArmActivation::opcode &&
        MsgProposalLifecycleNotice::opcode != MsgActivationStatus::opcode &&
        MsgProposalLifecycleNotice::opcode !=
            MsgEpochDefinitionRequest::opcode &&
        MsgProposalLifecycleNotice::opcode !=
            MsgEpochDefinitionReply::opcode &&
        MsgProposalLifecycleNotice::opcode !=
            MsgAdaptiveV2EpochChangeBundle::opcode,
    "adaptive evidence and epoch message opcodes must remain distinct");

} // namespace hotstuff

#endif
