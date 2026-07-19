/**
 * Bounded, accepted-evidence-only projection into prototype reputation.
 */

#ifndef HOTSTUFF_EVIDENCE_REPUTATION_H_INCLUDED
#define HOTSTUFF_EVIDENCE_REPUTATION_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <memory>
#include <vector>

#include "hotstuff/evidence.h"
#include "hotstuff/simple_reputation.h"

namespace hotstuff
{

struct EvidenceReputationLimits
{
    std::size_t maximum_audit_updates{4096};
};

enum class EvidenceReputationApplyStatus : std::uint8_t
{
    applied = 1,
    no_updates,
    projection_unhealthy,
    ledger_unhealthy,
    invalid_cutoff,
    accepted_order_invalid,
    audit_capacity_exceeded,
    reputation_rejected,
    internal_failure,
};

struct EvidenceReputationAuditUpdate
{
    std::uint64_t ingestion_sequence{0};
    uint256_t observation_id;
    ReplicaID reporter_id{0};
    ReplicaID target_id{0};
    ResponseOutcome evidence_outcome{ResponseOutcome::on_time};
    SimpleReputationOutcome reputation_outcome{
        SimpleReputationOutcome::response};
    int delta{0};
    int score{0};
};

struct EvidenceReputationApplyResult
{
    EvidenceReputationApplyStatus status{
        EvidenceReputationApplyStatus::no_updates};
    std::size_t applied_updates{0};
    std::uint64_t requested_cutoff{0};
    std::uint64_t last_applied_ingestion_sequence{0};
    SimpleReputationDisposition reputation_disposition{
        SimpleReputationDisposition::applied};
};

/**
 * Append-only projection of an EvidenceLedger accepted prefix.
 *
 * Calls are single-writer, externally serialized, and non-reentrant with the
 * borrowed ledger and reputation table. Both borrowed objects must outlive the
 * projection, and the borrowed reputation table must have no other mutation
 * owner during that lifetime. A cutoff is a ledger ingestion high watermark
 * and may only stay equal or increase. Rejected-record gaps are valid;
 * accepted records are applied once in their strict ledger order.
 *
 * Only ledger.accepted() is consumed. On-time evidence maps to +1, timeout to
 * -1, and the ledger's legal timeout-to-late transition maps late to a
 * compensating +1. The returned audit view is append-only and immutable
 * through this API.
 *
 * This component has no consensus, quorum, topology, epoch, wait-exemption,
 * or fault-diagnosis authority. Authentication proves report origin, not
 * truth, so a scalar score cannot independently designate a faulty replica.
 */
class EvidenceReputationProjection final
{
public:
    EvidenceReputationProjection(
        const EvidenceLedger &ledger,
        SimpleReputation &reputation,
        EvidenceReputationLimits limits = {});
    ~EvidenceReputationProjection();

    EvidenceReputationProjection(
        const EvidenceReputationProjection &) = delete;
    EvidenceReputationProjection &operator=(
        const EvidenceReputationProjection &) = delete;
    EvidenceReputationProjection(
        EvidenceReputationProjection &&) = delete;
    EvidenceReputationProjection &operator=(
        EvidenceReputationProjection &&) = delete;

    EvidenceReputationApplyResult apply_through(
        std::uint64_t evidence_cutoff) noexcept;

    const std::vector<EvidenceReputationAuditUpdate> &
    audit_updates() const noexcept;

    std::uint64_t last_cutoff() const noexcept;
    std::uint64_t last_applied_ingestion_sequence() const noexcept;
    bool healthy() const noexcept;

private:
    struct State;
    std::unique_ptr<State> state_;
};

} // namespace hotstuff

#endif
