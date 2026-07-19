/**
 * Bounded manager-side proposal lifecycle evidence coordination.
 */

#ifndef HOTSTUFF_EVIDENCE_LIFECYCLE_H_INCLUDED
#define HOTSTUFF_EVIDENCE_LIFECYCLE_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <map>
#include <memory>
#include <variant>
#include <vector>

#include "hotstuff/evidence.h"
#include "hotstuff/proposal_evidence_index.h"

namespace hotstuff
{

constexpr std::uint32_t kProposalLifecycleNoticeSchemaVersion = 2;

struct NormalProposalRuntimeInitialized
{
    ProposalKey proposal;
};

struct ProposalRuntimeAborted
{
    ProposalKey proposal;
};

struct ProposalCommitted
{
    ProposalKey proposal;
    std::uint64_t evidence_sequence_fence{0};
};

struct ProposalConfigurationRetired
{
    ConfigurationId configuration;
};

struct ProposalRetirementFloorAdvanced
{
    std::uint32_t first_live_epoch{0};
};

using ProposalLifecycleFact = std::variant<
    NormalProposalRuntimeInitialized,
    ProposalRuntimeAborted,
    ProposalCommitted,
    ProposalConfigurationRetired,
    ProposalRetirementFloorAdvanced>;

struct ProposalLifecycleNotice
{
    std::uint32_t schema_version{
        kProposalLifecycleNoticeSchemaVersion};
    ReplicaID source_replica_id{0};
    std::uint64_t source_sequence{0};
    ProposalLifecycleFact fact;
};

struct EvidenceLifecycleLimits
{
    std::size_t maximum_quarantined_records{4096};
    std::size_t maximum_quarantined_bytes{4 * 1024 * 1024};
    std::size_t maximum_reporter_queues{1024};
    std::size_t maximum_signer_entries{65536};
    std::size_t maximum_deduplication_entries{4096};
    std::size_t maximum_lifecycle_sources{1024};
    std::size_t maximum_quarantined_records_per_reporter{128};
};

struct EvidenceLifecycleAccountingLimits
{
    std::size_t maximum_records{4096};
    std::size_t maximum_canonical_bytes{4 * 1024 * 1024};
    std::size_t maximum_signer_entries{65536};
};

struct EvidenceRetentionCost
{
    std::size_t canonical_bytes{0};
    std::size_t signer_entries{0};
};

struct EvidenceLifecycleAccountingStats
{
    std::size_t retained_records{0};
    std::size_t retained_canonical_bytes{0};
    std::size_t retained_signer_entries{0};
};

/**
 * Checked accounting for evidence retained by one coordinator.
 *
 * Retain and release are all-or-nothing. Invalid limits, overflow,
 * underflow, and mismatched releases leave every counter unchanged.
 */
class EvidenceLifecycleAccounting final
{
public:
    explicit EvidenceLifecycleAccounting(
        EvidenceLifecycleAccountingLimits limits = {}) noexcept;

    bool try_retain(EvidenceRetentionCost cost) noexcept;
    bool release(EvidenceRetentionCost cost) noexcept;
    EvidenceLifecycleAccountingLimits limits() const noexcept;
    EvidenceLifecycleAccountingStats stats() const noexcept;

private:
    struct RetentionCostLess
    {
        bool operator()(const EvidenceRetentionCost &left,
                        const EvidenceRetentionCost &right) const noexcept
        {
            if (left.canonical_bytes != right.canonical_bytes)
                return left.canonical_bytes < right.canonical_bytes;
            return left.signer_entries < right.signer_entries;
        }
    };

    EvidenceLifecycleAccountingLimits limits_;
    EvidenceLifecycleAccountingStats stats_;
    std::map<EvidenceRetentionCost, std::size_t, RetentionCostLess>
        retained_costs_;
};

enum class ProposalLifecycleApplyStatus : std::uint8_t
{
    applied = 1,
    rejected_schema,
    rejected_authentication,
    rejected_sequence,
    evidence_unhealthy,
    stopped,
};

enum class EvidenceObservationDisposition : std::uint8_t
{
    ingested = 1,
    quarantined_unknown,
    quarantined_behind_unknown,
    duplicate_quarantined,
    rejected_quarantine_capacity,
    evidence_unhealthy,
    stopped,
};

struct ProposalLifecycleApplyResult
{
    ProposalLifecycleApplyStatus status{
        ProposalLifecycleApplyStatus::applied};
    bool index_changed{false};
    std::size_t retried_observations{0};
    std::size_t accepted_observations{0};
    std::size_t rejected_observations{0};
    std::size_t remaining_quarantined{0};
};

struct EvidenceObservationResult
{
    EvidenceObservationDisposition disposition{
        EvidenceObservationDisposition::ingested};
    std::size_t accepted_observations{0};
    std::size_t rejected_observations{0};
    std::size_t quarantined_observations{0};
};

struct EvidenceLifecycleStats
{
    std::size_t quarantined_records{0};
    std::size_t quarantined_bytes{0};
    std::size_t reporter_queues{0};
    std::size_t signer_entries{0};
    std::size_t deduplication_entries{0};
    std::size_t lifecycle_sources{0};
    std::uint64_t duplicate_observations{0};
    std::uint64_t applied_lifecycle_notices{0};
    std::uint64_t quarantine_quota_rejections{0};
    std::uint64_t capacity_failures{0};
    bool healthy{true};
    bool stopped{false};
};

/**
 * Deterministic diagnostic view ordered by reporter id and reporter FIFO.
 */
struct QuarantinedEvidenceObservation
{
    AuthenticatedReporter authenticated_reporter;
    ResponseObservation observation;
    bool reporter_fifo_head{false};
};

/**
 * Externally serialized owner of lifecycle sequence and quarantine state.
 *
 * The proposal index, optional reporter-aware proposal window, and evidence
 * ledger are borrowed and must outlive this coordinator. This class does not
 * own transport, authentication, clocks, or epoch activation. Authenticated
 * replica identities are supplied by the caller. Construction transfers an
 * empty accounting owner; retained input throws std::invalid_argument before
 * transfer and leaves the input, index, window, and ledger unchanged.
 */
class ProposalLifecycleEvidenceCoordinator final
{
public:
    ProposalLifecycleEvidenceCoordinator(
        ProposalEvidenceIndex &proposal_index,
        EvidenceLedger &ledger,
        EvidenceLifecycleAccounting &&accounting,
        EvidenceLifecycleLimits limits = {});

    ProposalLifecycleEvidenceCoordinator(
        ProposalEvidenceIndex &proposal_index,
        const ProposalEvidenceWindow &proposal_window,
        EvidenceLedger &ledger,
        EvidenceLifecycleAccounting &&accounting,
        EvidenceLifecycleLimits limits = {});
    ~ProposalLifecycleEvidenceCoordinator();

    ProposalLifecycleEvidenceCoordinator(
        const ProposalLifecycleEvidenceCoordinator &) = delete;
    ProposalLifecycleEvidenceCoordinator &operator=(
        const ProposalLifecycleEvidenceCoordinator &) = delete;
    ProposalLifecycleEvidenceCoordinator(
        ProposalLifecycleEvidenceCoordinator &&) = delete;
    ProposalLifecycleEvidenceCoordinator &operator=(
        ProposalLifecycleEvidenceCoordinator &&) = delete;

    ProposalLifecycleApplyResult apply_notice(
        const AuthenticatedReporter &authenticated_source,
        const ProposalLifecycleNotice &notice) noexcept;

    /**
     * Retry only one authenticated reporter FIFO after an external causal
     * boundary changes its reporter-aware proposal classification.
     */
    ProposalLifecycleApplyResult retry_reporter(
        const AuthenticatedReporter &authenticated_reporter) noexcept;

    EvidenceObservationResult ingest_observation(
        const AuthenticatedReporter &authenticated_reporter,
        const ResponseObservation &observation) noexcept;

    EvidenceLifecycleStats stats() const noexcept;
    const EvidenceLifecycleAccounting &accounting() const noexcept;
    std::vector<QuarantinedEvidenceObservation>
    quarantined_observations() const;
    bool healthy() const noexcept;
    void shutdown() noexcept;

private:
    struct State;
    std::unique_ptr<State> state_;
};

} // namespace hotstuff

#endif
