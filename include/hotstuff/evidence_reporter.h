/**
 * Pure bounded reporting outbox for immutable response-attempt facts.
 */

#ifndef HOTSTUFF_EVIDENCE_REPORTER_H_INCLUDED
#define HOTSTUFF_EVIDENCE_REPORTER_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>

#include "hotstuff/response_attempt.h"

namespace hotstuff
{

constexpr std::uint32_t kEvidenceReportEnvelopeSchemaVersion = 1;

enum class EvidenceTransportResult : std::uint8_t
{
    accepted = 1,
    temporary_failure = 2,
    permanent_failure = 3,
};

struct EvidenceReportEnvelope
{
    std::uint32_t schema_version{
        kEvidenceReportEnvelopeSchemaVersion};
    ResponseObservation observation;
    bytearray_t canonical_payload;
};

struct PendingEvidenceReport
{
    EvidenceReportEnvelope envelope;
    std::uint64_t delivery_attempts{0};
    std::uint64_t temporary_failures{0};
    std::uint64_t callback_exceptions{0};
    bool permanently_failed{false};
};

struct EvidenceReporterLimits
{
    std::size_t maximum_pending_reports{4096};
};

struct EvidenceReporterConfig
{
    ReplicaID trusted_reporter_id{0};
    std::uint64_t initial_reporter_sequence{0};
    std::uint64_t initial_reporter_monotonic_ns{0};
    EvidenceReporterLimits limits;
    EvidenceWireLimits wire_limits;
    bool exact_timeout_attempt_evidence_v3{false};
};

struct EvidenceReporterDiagnostics
{
    std::size_t pending_reports{0};
    std::uint64_t last_reporter_sequence{0};
    std::uint64_t last_reporter_monotonic_ns{0};
    std::uint64_t accepted_reports{0};
    std::uint64_t temporary_failures{0};
    std::uint64_t permanent_failures{0};
    std::uint64_t rejected_facts{0};
    std::uint64_t capacity_failures{0};
    std::uint64_t payload_failures{0};
    std::uint64_t sequence_overflows{0};
    std::uint64_t timestamp_regressions{0};
    std::uint64_t allocation_failures{0};
    std::uint64_t callback_exceptions{0};
    bool healthy{true};
    bool stopped{false};
};

using EvidenceTransportCallback =
    std::function<EvidenceTransportResult(
        const EvidenceReportEnvelope &)>;

/**
 * A fact-only, single-writer outbox with injected trusted identity and bounds.
 * Calls must be externally serialized. The transport callback is non-reentrant
 * with every reporter operation.
 *
 * Enqueue constructs one canonical observation batch without mutating reporter
 * state. Sequence and timestamp advance only after the complete envelope is in
 * the bounded FIFO. A pending envelope remains byte-for-byte stable until an
 * accepted transport result removes it.
 *
 * Pointers returned by front() remain valid only until the next mutation or
 * reporter destruction.
 */
class EvidenceReporter final
{
public:
    explicit EvidenceReporter(EvidenceReporterConfig config);
    ~EvidenceReporter();

    EvidenceReporter(const EvidenceReporter &) = delete;
    EvidenceReporter &operator=(const EvidenceReporter &) = delete;
    EvidenceReporter(EvidenceReporter &&) = delete;
    EvidenceReporter &operator=(EvidenceReporter &&) = delete;

    bool enqueue(const ResponseAttemptFact &fact);
    bool enable_exact_timeout_attempt_evidence_v3() noexcept;

    std::optional<EvidenceTransportResult> dispatch_one(
        const EvidenceTransportCallback &transport);

    const PendingEvidenceReport *front() const noexcept;
    std::size_t pending_size() const noexcept;
    EvidenceReporterDiagnostics diagnostics() const noexcept;
    bool healthy() const noexcept;
    void shutdown() noexcept;

private:
    struct State;
    std::unique_ptr<State> state_;
};

} // namespace hotstuff

#endif
