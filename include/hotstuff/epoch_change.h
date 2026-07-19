/**
 * Signed, consensus-ordered adaptive-v2 epoch-change commands.
 */

#ifndef HOTSTUFF_EPOCH_CHANGE_H_INCLUDED
#define HOTSTUFF_EPOCH_CHANGE_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <optional>
#include <vector>

#include "hotstuff/crypto.h"
#include "hotstuff/epoch_store.h"
#include "hotstuff/epoch_wire.h"

namespace hotstuff
{

class Block;

constexpr std::uint32_t kEpochChangeSchemaVersionV1 = 1;
using EpochChangeIssuerId = std::uint32_t;

struct EpochChangePayload
{
    std::uint32_t successor_epoch_number{0};
    uint256_t predecessor_epoch_digest;
    uint256_t successor_epoch_digest;
    std::uint64_t activation_delay_blocks{0};

    bool operator==(const EpochChangePayload &other) const noexcept;
    bool operator!=(const EpochChangePayload &other) const noexcept;
};

struct AuthorizedEpochChange
{
    std::uint32_t schema_version{kEpochChangeSchemaVersionV1};
    EpochProtocolMode protocol_mode{EpochProtocolMode::adaptive_v2};
    EpochChangePayload payload;
    EpochChangeIssuerId issuer_id{0};
    SigSecp256k1 authorization;
};

struct EpochChangeIssuer
{
    EpochChangeIssuerId issuer_id{0};
    PubKeySecp256k1 public_key;
};

bytearray_t canonical_serialize_epoch_change_payload(
    const EpochChangePayload &payload);

uint256_t epoch_change_payload_digest(const EpochChangePayload &payload);

AuthorizedEpochChange authorize_epoch_change(
    const EpochChangePayload &payload,
    EpochChangeIssuerId issuer_id,
    const PrivKeySecp256k1 &private_key);

bool verify_epoch_change_signature(
    const AuthorizedEpochChange &command,
    const EpochChangeIssuer &issuer) noexcept;

bytearray_t canonical_epoch_change_signing_bytes(
    const AuthorizedEpochChange &command);

bytearray_t encode_authorized_epoch_change(
    const AuthorizedEpochChange &command);

uint256_t epoch_change_envelope_digest(
    const AuthorizedEpochChange &command);

enum class EpochChangeWireError : std::uint8_t
{
    none = 0,
    invalid_limit,
    payload_too_large,
    truncated,
    trailing_bytes,
    invalid_domain,
    unsupported_schema,
    unsupported_mode,
    malformed_signature,
    noncanonical_encoding,
    allocation_failure,
    internal_failure,
};

struct EpochChangeDecodeResult
{
    EpochChangeWireError error{EpochChangeWireError::none};
    std::optional<AuthorizedEpochChange> value;

    explicit operator bool() const noexcept
    {
        return error == EpochChangeWireError::none && value.has_value();
    }
};

EpochChangeDecodeResult decode_authorized_epoch_change(
    const bytearray_t &payload,
    std::size_t maximum_payload_bytes) noexcept;

enum class EpochChangeExtraDisposition : std::uint8_t
{
    absent = 0,
    present,
    rejected,
};

struct EpochChangeBlockExtraResult
{
    EpochChangeExtraDisposition disposition{
        EpochChangeExtraDisposition::rejected};
    EpochChangeWireError wire_error{EpochChangeWireError::none};
    std::optional<AuthorizedEpochChange> command;
    std::optional<uint256_t> payload_digest;
    std::optional<uint256_t> envelope_digest;
};

bytearray_t encode_epoch_change_block_extra(
    const AuthorizedEpochChange &command);

EpochChangeBlockExtraResult extract_epoch_change_block_extra(
    const bytearray_t &extra,
    std::size_t maximum_payload_bytes) noexcept;

struct EpochChangeDelayBounds
{
    std::uint64_t minimum_blocks{0};
    std::uint64_t maximum_blocks{0};
};

enum class EpochChangeDisposition : std::uint8_t
{
    accepted = 0,
    duplicate,
    defer_missing_definition,
    unsupported_schema,
    unsupported_mode,
    unauthorized_issuer,
    invalid_signature,
    invalid_delay,
    stale,
    wrong_predecessor,
    invalid_successor,
    conflicting_successor,
    invalid_definition,
};

struct EpochChangeHistoryView
{
    std::optional<uint256_t> ancestry_payload_digest;
    std::optional<uint256_t> committed_payload_digest;
};

struct EpochChangeCommittedHistoryEntry
{
    uint256_t predecessor_epoch_digest;
    uint256_t payload_digest;
};

struct EpochChangeCommittedHistorySnapshot
{
    uint256_t committed_head_hash;
    std::uint32_t committed_head_height{0};
    std::optional<EpochChangeCommittedHistoryEntry> command;
};

enum class EpochChangeProposalHistoryDisposition : std::uint8_t
{
    complete = 0,
    rejected,
};

enum class EpochChangeProposalHistoryError : std::uint8_t
{
    none = 0,
    invalid_limit,
    invalid_committed_boundary,
    incoherent_committed_snapshot,
    missing_parent,
    parent_hash_mismatch,
    cycle,
    nondecreasing_height,
    ancestry_limit_exceeded,
    boundary_not_reached,
    malformed_extra,
    conflicting_history,
    allocation_failure,
    internal_failure,
};

struct EpochChangeProposalHistoryObservation
{
    uint256_t block_hash;
    std::uint32_t block_height{0};
    bool committed_boundary{false};
    AuthorizedEpochChange command;
    uint256_t payload_digest;
    uint256_t envelope_digest;
};

struct EpochChangeProposalHistoryResult
{
    EpochChangeProposalHistoryDisposition disposition{
        EpochChangeProposalHistoryDisposition::rejected};
    EpochChangeProposalHistoryError error{
        EpochChangeProposalHistoryError::internal_failure};
    EpochChangeWireError wire_error{EpochChangeWireError::none};
    EpochChangeHistoryView history;
    std::vector<EpochChangeProposalHistoryObservation> observations;

    explicit operator bool() const noexcept
    {
        return disposition == EpochChangeProposalHistoryDisposition::complete &&
               error == EpochChangeProposalHistoryError::none &&
               wire_error == EpochChangeWireError::none;
    }
};

/**
 * Build the epoch-command history of a proposal's first-parent ancestry.
 *
 * The proposal itself is excluded. The exact committed boundary is parsed as
 * committed history and reconciled with its head-bound snapshot. Any malformed
 * or incomplete ancestry is rejected without exposing partial history or
 * observations. Nonempty blocks are observed in first-parent traversal order,
 * from the proposal's parent through the exact committed boundary.
 *
 * This builder performs bounded structural and canonical extraction only.
 * Before voting, callers must authenticate and semantically validate every
 * traversed nonempty ancestor; block delivery alone does not establish that
 * invariant.
 */
EpochChangeProposalHistoryResult build_epoch_change_proposal_history(
    const Block &proposal,
    const Block &committed_head,
    const uint256_t &candidate_predecessor_digest,
    const EpochChangeCommittedHistorySnapshot &committed_snapshot,
    std::size_t maximum_block_extra_bytes,
    std::size_t maximum_ancestry_blocks) noexcept;

struct EpochChangeValidationResult
{
    EpochChangeDisposition disposition{
        EpochChangeDisposition::invalid_definition};
    uint256_t payload_digest;
    uint256_t envelope_digest;
    const EpochDefinition *successor_definition{nullptr};
    std::optional<EpochDefinitionRequest> recovery_request;
};

class EpochChangeVerifier final
{
public:
    EpochChangeVerifier(
        EpochChangeIssuer issuer,
        EpochChangeDelayBounds delay_bounds);

    EpochChangeValidationResult validate(
        const AuthorizedEpochChange &command,
        const EpochDefinition &active_epoch,
        const EpochStore &store,
        const EpochChangeHistoryView &history) const;

private:
    const EpochChangeIssuer issuer_;
    const EpochChangeDelayBounds delay_bounds_;
};

enum class EpochChangeProposalDisposition : std::uint8_t
{
    accepted = 0,
    duplicate,
    defer,
    rejected,
};

struct EpochChangeProposalControlResult
{
    EpochChangeProposalDisposition disposition{
        EpochChangeProposalDisposition::rejected};
    EpochChangeWireError wire_error{EpochChangeWireError::none};
    std::optional<AuthorizedEpochChange> command;
    std::optional<EpochChangeValidationResult> validation;
};

EpochChangeProposalControlResult evaluate_epoch_change_proposal_control(
    const bytearray_t &extra,
    std::size_t maximum_payload_bytes,
    const EpochChangeVerifier &verifier,
    const EpochDefinition &active_epoch,
    const EpochStore &store,
    const EpochChangeHistoryView &history) noexcept;

enum class EpochChangeProposalChainFailureSource : std::uint8_t
{
    candidate = 0,
    ancestor,
};

struct EpochChangeProposalChainFailure
{
    EpochChangeProposalChainFailureSource source{
        EpochChangeProposalChainFailureSource::candidate};
    uint256_t block_hash;
    std::uint32_t block_height{0};
    std::optional<EpochChangeDisposition> validation_disposition;
};

struct EpochChangeProposalChainResult
{
    EpochChangeProposalDisposition disposition{
        EpochChangeProposalDisposition::rejected};
    EpochChangeProposalHistoryError history_error{
        EpochChangeProposalHistoryError::internal_failure};
    EpochChangeWireError wire_error{EpochChangeWireError::none};
    EpochChangeHistoryView history;
    std::vector<EpochChangeProposalHistoryObservation> observations;
    std::optional<EpochDefinitionRequest> recovery_request;
    std::optional<EpochChangeProposalChainFailure> failure;
};

/**
 * Produce one pure, bounded, fail-closed verdict for a proposal chain.
 *
 * The candidate command and every nonempty uncommitted first-parent ancestor
 * are authenticated and semantically checked against the exact active epoch.
 * The committed boundary remains structural history authority and is not
 * revalidated against the newer active epoch. No store or block state is
 * mutated.
 */
EpochChangeProposalChainResult evaluate_epoch_change_proposal_chain(
    const Block &proposal,
    const Block &committed_head,
    const EpochChangeCommittedHistorySnapshot &committed_snapshot,
    std::size_t maximum_block_extra_bytes,
    std::size_t maximum_ancestry_blocks,
    const EpochChangeVerifier &verifier,
    const EpochDefinition &active_epoch,
    const EpochStore &store) noexcept;

} // namespace hotstuff

#endif
