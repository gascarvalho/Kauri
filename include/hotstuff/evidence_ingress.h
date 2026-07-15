/**
 * Pure bounded manager ingress for authenticated response evidence.
 */

#ifndef HOTSTUFF_EVIDENCE_INGRESS_H_INCLUDED
#define HOTSTUFF_EVIDENCE_INGRESS_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>

#include "hotstuff/evidence.h"

namespace hotstuff
{

/**
 * An opaque transport wrapper for one canonical evidence-batch payload.
 * Construction preserves the supplied bytes without decoding them.
 */
struct MsgEvidenceReport
{
    static const opcode_t opcode = 0x12;
    DataStream serialized;

    explicit MsgEvidenceReport(const bytearray_t &canonical_payload);
    explicit MsgEvidenceReport(DataStream &&serialized_payload);
};

struct EvidenceIngressResult
{
    const std::size_t decoded_observations{0};
    const std::size_t accepted_observations{0};
    const std::size_t rejected_observations{0};
    const std::size_t wire_rejections{0};
    const std::uint64_t ledger_high_watermark{0};
    const std::optional<EvidenceWireError> wire_error;
};

/**
 * Pure single-writer boundary around an externally owned EvidenceLedger.
 *
 * The caller derives AuthenticatedReporter from the
 * configured mutual-TLS certificate identity, never payload or source address.
 * Calls are externally serialized and non-reentrant with every EvidenceLedger
 * operation. The ledger and its dependencies must outlive this ingress.
 *
 * Valid batches are delivered deterministically in wire order. Delivery is
 * not batch-transactional: a completed ledger prefix remains visible if a later
 * observation throws. This class owns no authentication transport or protocol
 * side effects.
 */
class EvidenceIngress final
{
public:
    EvidenceIngress(
        EvidenceLedger &ledger,
        EvidenceWireLimits limits);
    ~EvidenceIngress();

    EvidenceIngress(const EvidenceIngress &) = delete;
    EvidenceIngress &operator=(const EvidenceIngress &) = delete;
    EvidenceIngress(EvidenceIngress &&) = delete;
    EvidenceIngress &operator=(EvidenceIngress &&) = delete;

    EvidenceIngressResult ingest(
        const AuthenticatedReporter &authenticated_reporter,
        const MsgEvidenceReport &message);

    EvidenceIngressResult ingest(
        const AuthenticatedReporter &authenticated_reporter,
        const bytearray_t &canonical_payload);

    bool healthy() const noexcept;

private:
    struct State;
    std::unique_ptr<State> state_;
};

} // namespace hotstuff

#endif
