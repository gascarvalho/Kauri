/**
 * Transport-independent adaptive-v2 replica reporting outbox.
 */

#ifndef HOTSTUFF_ADAPTIVE_V2_REPORTING_OUTBOX_H_INCLUDED
#define HOTSTUFF_ADAPTIVE_V2_REPORTING_OUTBOX_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>

#include "hotstuff/adaptive_v2_convergence_ack_wire.h"
#include "hotstuff/adaptive_v2_readiness_wire.h"
#include "hotstuff/evidence_ingress.h"
#include "hotstuff/evidence_lifecycle_wire.h"

namespace hotstuff
{

enum class AdaptiveV2ReportingStream : std::uint8_t
{
    readiness = 1,
    lifecycle = 2,
    evidence = 3,
    convergence = 4,
};

enum class AdaptiveV2ReportingDeliveryState : std::uint8_t
{
    queued = 1,
    in_flight,
    retry_wait,
    awaiting_ack,
    delivered,
    failed,
};

enum class AdaptiveV2ReportingFailureReason : std::uint8_t
{
    none = 0,
    permanent_failure,
    retry_exhausted,
    backoff_overflow,
    invalid_delivery_result,
};

enum class AdaptiveV2ReportingEnqueueStatus : std::uint8_t
{
    queued = 1,
    stopped,
    unhealthy,
    invalid_configuration,
    capacity_exceeded,
    sequence_exhausted,
    sequence_regression,
    invalid_payload,
    allocation_failure,
    internal_failure,
};

enum class AdaptiveV2ReportingAttemptStatus : std::uint8_t
{
    started = 1,
    empty,
    retry_not_due,
    already_in_flight,
    terminal,
    stopped,
    unhealthy,
};

enum class AdaptiveV2ReportingDeliveryResult : std::uint8_t
{
    delivered = 1,
    temporary_failure,
    permanent_failure,
};

enum class AdaptiveV2ReportingTransitionStatus : std::uint8_t
{
    delivered = 1,
    retry_scheduled,
    failed,
    duplicate,
    stale_report,
    stale_attempt,
    invalid_transition,
    stopped,
    unhealthy,
};

enum class AdaptiveV2ReportingReleaseStatus : std::uint8_t
{
    released = 1,
    empty,
    stale_report,
    not_terminal,
    unhealthy,
};

struct AdaptiveV2ReportingOutboxLimits
{
    std::size_t maximum_pending_reports{4096};
    std::size_t maximum_pending_payload_bytes{4 * 1024 * 1024};
    std::uint32_t maximum_delivery_attempts{5};
    std::uint64_t initial_retry_backoff_ns{1'000'000};
    std::uint64_t maximum_retry_backoff_ns{1'000'000'000};
    AdaptiveV2ReadinessWireLimits readiness_wire;
    ProposalLifecycleWireLimits lifecycle_wire;
    EvidenceWireLimits evidence_wire;
    AdaptiveV2ConvergenceWireLimits convergence_wire;
};

struct AdaptiveV2ReportingOutboxConfig
{
    ReplicaID source_replica_id{0};
    std::uint64_t initial_readiness_sequence{0};
    std::uint64_t initial_lifecycle_sequence{0};
    std::uint64_t initial_evidence_sequence{0};
    std::uint64_t initial_report_id{0};
    AdaptiveV2ReportingOutboxLimits limits;
};

struct AdaptiveV2ReportingDeliveryToken
{
    std::uint64_t report_id{0};
    std::uint32_t attempt_number{0};

    bool operator==(
        const AdaptiveV2ReportingDeliveryToken &other) const noexcept
    {
        return report_id == other.report_id &&
               attempt_number == other.attempt_number;
    }
};

struct AdaptiveV2PendingReport
{
    std::uint64_t report_id{0};
    AdaptiveV2ReportingStream stream{
        AdaptiveV2ReportingStream::readiness};
    opcode_t opcode{0};
    std::uint64_t first_stream_sequence{0};
    std::uint64_t last_stream_sequence{0};
    bytearray_t canonical_payload;
    AdaptiveV2ReportingDeliveryState delivery_state{
        AdaptiveV2ReportingDeliveryState::queued};
    AdaptiveV2ReportingFailureReason failure_reason{
        AdaptiveV2ReportingFailureReason::none};
    std::uint32_t delivery_attempts{0};
    std::uint32_t temporary_failures{0};
    std::uint64_t next_attempt_monotonic_ns{0};
    std::optional<AdaptiveV2ConvergenceObservationKind>
        convergence_observation_kind;
    std::optional<AdaptiveV2EpochChangeIdentity>
        convergence_identity;
    uint256_t convergence_observation_digest;
};

struct AdaptiveV2ReportingAttemptResult
{
    AdaptiveV2ReportingAttemptStatus status{
        AdaptiveV2ReportingAttemptStatus::empty};
    std::optional<AdaptiveV2ReportingDeliveryToken> token;
    const AdaptiveV2PendingReport *report{nullptr};
};

struct AdaptiveV2ReportingDiagnostics
{
    std::size_t pending_reports{0};
    std::size_t pending_payload_bytes{0};
    std::uint64_t last_readiness_sequence{0};
    std::uint64_t last_lifecycle_sequence{0};
    std::uint64_t last_evidence_sequence{0};
    std::uint64_t last_report_id{0};
    std::uint64_t enqueued_reports{0};
    std::uint64_t delivery_attempts{0};
    std::uint64_t temporary_failures{0};
    std::uint64_t delivered_reports{0};
    std::uint64_t failed_reports{0};
    std::uint64_t retry_exhaustions{0};
    std::uint64_t duplicate_acknowledgements{0};
    std::uint64_t capacity_failures{0};
    std::uint64_t sequence_failures{0};
    std::uint64_t payload_failures{0};
    std::uint64_t allocation_failures{0};
    std::uint64_t internal_failures{0};
    std::uint64_t invalid_transitions{0};
    bool healthy{true};
    bool stopped{false};
};

/**
 * Single-writer FIFO for one replica and one externally configured manager.
 *
 * The transport owner chooses the manager and sends `opcode` plus the exact
 * canonical bytes returned by front()/begin_delivery(). This class owns no
 * sockets, TLS or peer authentication, timers, quorum state, scoring,
 * selection, topology, or activation authority. It never creates response
 * evidence: enqueue_evidence accepts and preserves an existing canonical
 * evidence batch byte-for-byte.
 *
 * Calls must be externally serialized. Returned pointers remain valid only
 * until the next outbox mutation or destruction.
 */
class AdaptiveV2ReportingOutbox final
{
public:
    explicit AdaptiveV2ReportingOutbox(
        AdaptiveV2ReportingOutboxConfig config = {});
    ~AdaptiveV2ReportingOutbox();

    AdaptiveV2ReportingOutbox(
        const AdaptiveV2ReportingOutbox &) = delete;
    AdaptiveV2ReportingOutbox &operator=(
        const AdaptiveV2ReportingOutbox &) = delete;
    AdaptiveV2ReportingOutbox(
        AdaptiveV2ReportingOutbox &&) = delete;
    AdaptiveV2ReportingOutbox &operator=(
        AdaptiveV2ReportingOutbox &&) = delete;

    AdaptiveV2ReportingEnqueueStatus enqueue_readiness(
        const ConfigurationId &active_configuration,
        std::uint64_t activation_generation,
        std::uint64_t committed_height) noexcept;

    AdaptiveV2ReportingEnqueueStatus enqueue_lifecycle(
        const ProposalLifecycleFact &fact) noexcept;

    AdaptiveV2ReportingEnqueueStatus enqueue_evidence(
        const bytearray_t &canonical_payload) noexcept;

    AdaptiveV2ReportingEnqueueStatus enqueue_epoch_change_committed(
        const AdaptiveV2EpochChangeIdentity &identity) noexcept;

    AdaptiveV2ReportingEnqueueStatus enqueue_epoch_activated(
        const AdaptiveV2EpochChangeIdentity &identity) noexcept;

    AdaptiveV2ReportingEnqueueStatus enqueue_convergence_observation(
        AdaptiveV2ConvergenceObservationKind kind,
        const AdaptiveV2EpochChangeIdentity &identity) noexcept;

    const AdaptiveV2PendingReport *front() const noexcept;

    AdaptiveV2ReportingAttemptResult begin_delivery(
        std::uint64_t current_monotonic_ns) noexcept;

    AdaptiveV2ReportingTransitionStatus acknowledge_delivery(
        const AdaptiveV2ReportingDeliveryToken &token,
        AdaptiveV2ReportingDeliveryResult result,
        std::uint64_t current_monotonic_ns) noexcept;

    AdaptiveV2ReportingTransitionStatus
    acknowledge_convergence_observation(
        const AdaptiveV2ConvergenceObservationAck &acknowledgement)
        noexcept;

    AdaptiveV2ReportingReleaseStatus release_terminal(
        std::uint64_t report_id) noexcept;

    AdaptiveV2ReportingDiagnostics diagnostics() const noexcept;
    bool healthy() const noexcept;
    void shutdown() noexcept;

private:
    struct State;
    std::unique_ptr<State> state_;
};

} // namespace hotstuff

#endif
