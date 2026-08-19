/**
 * Canonical response evidence and bounded manager-side validation.
 */

#ifndef HOTSTUFF_EVIDENCE_H_INCLUDED
#define HOTSTUFF_EVIDENCE_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <vector>

#include "hotstuff/configuration.h"

namespace hotstuff
{

constexpr std::uint32_t kResponseObservationSchemaVersionV1 = 1;
constexpr std::uint32_t kResponseObservationSchemaVersionV2 = 2;
// Schema v3 binds every fact to the exact locally armed attempt start.  Its
// wire layout deliberately remains the v2 two-u64 extension.
constexpr std::uint32_t kResponseObservationSchemaVersionV3 = 3;
// The default remains v1. Schema v2 is an explicitly enabled experiment
// extension and must never leak into ordinary reporters.
constexpr std::uint32_t kResponseObservationSchemaVersion =
    kResponseObservationSchemaVersionV1;
constexpr std::uint32_t kEvidenceBatchSchemaVersion = 1;

constexpr bool is_supported_response_observation_schema(
    std::uint32_t schema_version) noexcept
{
    return schema_version == kResponseObservationSchemaVersionV1 ||
           schema_version == kResponseObservationSchemaVersionV2 ||
           schema_version == kResponseObservationSchemaVersionV3;
}

enum class ExpectedMessageType : std::uint8_t
{
    direct_vote = 1,
    aggregate_relay = 2,
    leader_progress = 3,
};

enum class ResponseOutcome : std::uint8_t
{
    on_time = 1,
    timeout = 2,
    late = 3,
};

struct ResponseAttemptIdentity
{
    ReplicaID reporter_id{0};
    ReplicaID observed_replica_id{0};
    ProposalKey proposal;
    ExpectedMessageType expected_message_type{
        ExpectedMessageType::direct_vote};

    bool operator==(const ResponseAttemptIdentity &other) const noexcept
    {
        return reporter_id == other.reporter_id &&
               observed_replica_id == other.observed_replica_id &&
               proposal == other.proposal &&
               expected_message_type == other.expected_message_type;
    }

    bool operator!=(const ResponseAttemptIdentity &other) const noexcept
    {
        return !(*this == other);
    }
};

struct ResponseObservation
{
    std::uint32_t schema_version{kResponseObservationSchemaVersion};
    uint256_t observation_id;
    ReplicaID reporter_id{0};
    ReplicaID observed_replica_id{0};
    ConfigurationId configuration;
    uint256_t block_hash;
    ExpectedMessageType expected_message_type{
        ExpectedMessageType::direct_vote};
    ResponseOutcome outcome{ResponseOutcome::on_time};
    std::uint64_t response_duration_us{0};
    std::uint64_t deadline_duration_us{0};
    std::uint64_t reporter_monotonic_ns{0};
    std::uint64_t reporter_sequence{0};
    std::vector<ReplicaID> signer_set;
    // Schema-v2-only reporter-local retention chronology. Both values are
    // CLOCK_MONOTONIC_RAW nanoseconds; schema v1 requires both to remain zero.
    std::uint64_t attempt_start_monotonic_ns{0};
    std::uint64_t reporter_local_commit_monotonic_ns{0};

    ProposalKey proposal_key() const
    {
        return {configuration, block_hash};
    }

    ResponseAttemptIdentity attempt_identity() const
    {
        return {reporter_id,
                observed_replica_id,
                proposal_key(),
                expected_message_type};
    }
};

bool valid_response_observation_retention_witness(
    const ResponseObservation &observation) noexcept;

uint256_t compute_response_observation_id(
    const ResponseAttemptIdentity &identity);
uint256_t compute_response_observation_id(
    const ResponseObservation &observation);

struct ResponseObservationBatch
{
    std::uint32_t schema_version{kEvidenceBatchSchemaVersion};
    std::vector<ResponseObservation> observations;
};

struct EvidenceWireLimits
{
    std::size_t maximum_payload_bytes{1024 * 1024};
    std::uint32_t maximum_observations{1024};
    std::uint32_t maximum_signers_per_observation{4096};
};

enum class EvidenceWireError : std::uint8_t
{
    none = 0,
    payload_too_large,
    unsupported_batch_schema,
    batch_count_exceeded,
    truncated,
    trailing_bytes,
    unsupported_observation_schema,
    invalid_expected_message_type,
    invalid_outcome,
    signer_count_exceeded,
    noncanonical_signer_set,
    allocation_failure,
    internal_failure,
    invalid_retention_witness,
};

struct EvidenceDecodeResult
{
    EvidenceWireError error{EvidenceWireError::none};
    std::optional<ResponseObservationBatch> batch;

    explicit operator bool() const noexcept
    {
        return error == EvidenceWireError::none && batch.has_value();
    }
};

bytearray_t encode_evidence_batch(
    const ResponseObservationBatch &batch,
    const EvidenceWireLimits &limits);

EvidenceDecodeResult decode_evidence_batch(
    const bytearray_t &payload,
    const EvidenceWireLimits &limits) noexcept;

enum class ProposalEvidenceStatus : std::uint8_t
{
    admissible = 1,
    stale = 2,
    unknown = 3,
};

struct AuthenticatedReporter
{
    ReplicaID replica_id{0};
};

class ProposalEvidenceWindow
{
public:
    virtual ~ProposalEvidenceWindow() = default;

    virtual ProposalEvidenceStatus classify(
        const ProposalKey &proposal) const noexcept = 0;

    /**
     * Reporter-aware admission defaults to the global proposal window.
     *
     * A transport owner with a stronger authenticated FIFO contract may
     * override this view without weakening the global lifecycle state seen by
     * other reporters. Overrides must key reporter state only from the
     * authenticated argument, never from the observation's claimed id. The
     * evidence ledger independently verifies that claim before acceptance.
     */
    virtual ProposalEvidenceStatus classify_for_reporter(
        const AuthenticatedReporter &authenticated_reporter,
        const ResponseObservation &observation) const noexcept
    {
        static_cast<void>(authenticated_reporter);
        return classify(observation.proposal_key());
    }
};

enum class EvidenceRejectionReason : std::uint8_t
{
    unsupported_schema = 1,
    observation_id_mismatch,
    reporter_mismatch,
    unknown_configuration,
    unknown_block,
    stale_block,
    impossible_topology,
    invalid_expected_message_type,
    invalid_outcome,
    invalid_timing,
    invalid_signer_set,
    duplicate_fact,
    invalid_transition,
    reporter_sequence_regression,
    reporter_timestamp_regression,
    accepted_capacity_exceeded,
    wire_error,
};

struct AcceptedEvidenceRecord
{
    std::uint64_t ingestion_sequence{0};
    ResponseObservation observation;
};

struct RejectedEvidenceRecord
{
    std::uint64_t ingestion_sequence{0};
    AuthenticatedReporter authenticated_reporter;
    EvidenceRejectionReason reason{
        EvidenceRejectionReason::unsupported_schema};
    std::optional<ResponseObservation> observation;
    std::optional<EvidenceWireError> wire_error;
};

struct EvidenceStoreLimits
{
    std::size_t maximum_accepted_records{4096};
    std::size_t maximum_rejected_records{4096};
};

/**
 * Validates authenticated observations against immutable epoch topology and
 * an injected proposal window.
 *
 * Concurrency and lifetime contract:
 * - The ledger is single-writer and non-reentrant. Callers must externally
 *   serialize all readers and writers.
 * - References and iterators returned through accepted() and rejected() may
 *   be invalidated by the next mutation or by ledger destruction.
 * - The epoch store and proposal window must outlive the ledger and must not
 *   be mutated concurrently with, or reenter, ledger operations.
 */
class EvidenceLedger
{
public:
    EvidenceLedger(const EpochStore &epochs,
                   const ProposalEvidenceWindow &window,
                   EvidenceStoreLimits limits);
    ~EvidenceLedger();

    EvidenceLedger(const EvidenceLedger &) = delete;
    EvidenceLedger &operator=(const EvidenceLedger &) = delete;
    EvidenceLedger(EvidenceLedger &&) = delete;
    EvidenceLedger &operator=(EvidenceLedger &&) = delete;

    void ingest(const AuthenticatedReporter &authenticated_reporter,
                const ResponseObservation &observation);

    /**
     * Audit an observation only when canonical validation proves that it is
     * invalid independently of proposal admission state.
     *
     * Returns false without mutating the ledger when proposal classification
     * is still required. Returns true after routing a definite rejection
     * through the ledger's own ingestion sequence and rejection storage.
     * The caller cannot supply a rejection reason and this method can never
     * accept evidence or bypass the proposal window for an otherwise valid
     * observation.
     */
    bool ingest_if_proposal_independent_rejected(
        const AuthenticatedReporter &authenticated_reporter,
        const ResponseObservation &observation);

    void reject_wire(const AuthenticatedReporter &authenticated_reporter,
                     EvidenceWireError error);

    std::uint64_t high_watermark() const noexcept;

    const std::vector<AcceptedEvidenceRecord> &accepted() const noexcept;
    const std::vector<RejectedEvidenceRecord> &rejected() const noexcept;

    bool healthy() const noexcept;

private:
    struct State;
    std::unique_ptr<State> state_;
};

} // namespace hotstuff

#endif
