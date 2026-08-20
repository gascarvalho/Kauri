#ifndef HOTSTUFF_ADAPTIVE_MANAGER_SESSION_FACADE_H_INCLUDED
#define HOTSTUFF_ADAPTIVE_MANAGER_SESSION_FACADE_H_INCLUDED

#include <memory>
#include <optional>

#include "hotstuff/adaptive_v2_manager_session.h"
#include "hotstuff/adaptive_v3_manager_session.h"

namespace hotstuff {
enum class AdaptiveManagerSessionMode : std::uint8_t { adaptive_v2 = 2, adaptive_v3 = 3 };
struct AdaptiveManagerSessionFacadeConfig {
    AdaptiveManagerSessionMode mode{AdaptiveManagerSessionMode::adaptive_v2};
    std::optional<AdaptiveV2ManagerSessionConfig> v2;
    std::optional<AdaptiveV3ManagerSessionConfig> v3;
};
/** Explicit mode partition: no fallback, no mixed authority. */
class AdaptiveManagerSessionFacade final {
public:
    AdaptiveManagerSessionFacade(std::vector<ReplicaID>, EpochDefinitionInput,
                                 AdaptiveManagerSessionFacadeConfig);
    ~AdaptiveManagerSessionFacade();
    AdaptiveManagerSessionFacade(const AdaptiveManagerSessionFacade &) = delete;
    AdaptiveManagerSessionMode mode() const noexcept;
    const AdaptiveV2ManagerIngress &ingress() const noexcept;
    AdaptiveV2ManagerReadinessResult ingest_readiness(const AuthenticatedReporter &, const MsgAdaptiveV2ReadinessNotice &) noexcept;
    AdaptiveV2ManagerLifecycleResult ingest_lifecycle(const AuthenticatedReporter &, const MsgProposalLifecycleNotice &) noexcept;
    AdaptiveV2ManagerEvidenceResult ingest_evidence(const AuthenticatedReporter &, const MsgEvidenceReport &) noexcept;
    bool begin_cycle(const AdaptiveV2TransitionPolicy &) noexcept;
    bool arm_fault_window(AdaptiveV2FaultWindowArm) noexcept;
    AdaptiveV2ManagerControllerStatus evaluate() noexcept;
    std::optional<AdaptiveV2ManagerControllerAuditSnapshot> controller_audit() const noexcept;

    // V2 convergence remains intentionally typed: the v3 certificate path is
    // not a convergence protocol and must never be coerced into one.
    bool v2_start_convergence(std::uint64_t logical_start_tick) noexcept;
    std::vector<AdaptiveV2ManagerDeliveryRequest> v2_due_deliveries(
        std::uint64_t logical_tick) noexcept;
    AdaptiveV2ManagerConvergenceDisposition v2_record_enqueue_result(
        ReplicaID recipient, std::uint32_t attempt, bool enqueued) noexcept;
    AdaptiveV2ManagerConvergenceDisposition v2_observe_commit(
        ReplicaID authenticated_replica,
        const AdaptiveV2EpochChangeCommittedObservation &) noexcept;
    AdaptiveV2ManagerConvergenceDisposition v2_observe_activation(
        ReplicaID authenticated_replica,
        const AdaptiveV2EpochActivatedObservation &) noexcept;
    std::optional<AdaptiveV2ManagerConvergenceStatus>
    v2_convergence_status() const noexcept;
    std::optional<AdaptiveV2ManagerConvergenceAuditSnapshot>
    v2_convergence_audit() const noexcept;
    bool v2_consume_ready_and_rotate() noexcept;
    bool v2_finalize_noop_cycle(AdaptiveV2ManagerCycleTerminalReason) noexcept;
    bool v2_finalize_failed_cycle(AdaptiveV2ManagerCycleTerminalReason) noexcept;
    const std::vector<AdaptiveV2ManagerSessionTerminalRecord> *
    v2_terminal_records() const noexcept;

    // V3 is an authenticated readiness/certificate delivery path.  Its
    // operations stay explicit so callers cannot accidentally treat a v3
    // certificate as v2 convergence authority.
    bool v3_arm_hard_deadline(std::uint64_t hard_deadline_tick) noexcept;
    AdaptiveV2ManagerLifecycleResult v3_ingest_timed_lifecycle(
        const AuthenticatedReporter &, const MsgProposalLifecycleNotice &,
        std::uint64_t manager_tick) noexcept;
    bool v3_begin_readiness(std::uint64_t logical_tick) noexcept;
    AdaptiveV3ManagerObservationResult v3_observe_readiness(
        ReplicaID tls_peer, std::uint64_t logical_tick,
        const bytearray_t &) noexcept;
    std::optional<AdaptiveV3CertificateDelivery> v3_begin_delivery(
        ReplicaID recipient, std::uint64_t logical_tick) noexcept;
    AdaptiveV3CertificateDeliveryDisposition v3_record_delivery_result(
        ReplicaID recipient, std::uint32_t attempt, bool enqueued,
        std::uint64_t logical_tick) noexcept;
    AdaptiveV3CertificateDeliveryDisposition v3_acknowledge(
        ReplicaID tls_peer, std::uint64_t logical_tick,
        const bytearray_t &) noexcept;
    bool v3_e2_eligible(std::uint64_t manager_tick) const noexcept;
    std::optional<AdaptiveV3E2EligibilityAuditSnapshot>
    v3_e2_eligibility_audit(std::uint64_t manager_tick) const noexcept;
    std::optional<AdaptiveV3E2EligibilityAuditSnapshot>
    v3_begin_e2_at(std::uint64_t manager_tick,
                   const AdaptiveV2TransitionPolicy &) noexcept;
    void v3_advance(std::uint64_t logical_tick) noexcept;
    std::optional<AdaptiveV3ManagerSessionStatus> v3_status() const noexcept;
    const AdaptiveV3ManagerSessionTerminalRecord *v3_terminal_audit() const noexcept;
    const std::vector<AdaptiveV3ManagerSessionTerminalRecord> *
    v3_terminal_records() const noexcept;
    const ActivationReadinessCertificateV1 *v3_certificate() const noexcept;
    const AdaptiveV2EpochChangeBundle *v2_successor_bundle() const noexcept;
    const AdaptiveV3EpochChangeBundle *v3_successor_bundle() const noexcept;
    AdaptiveV2ManagerSession *v2() noexcept;
    const AdaptiveV2ManagerSession *v2() const noexcept;
    AdaptiveV3ManagerSession *v3() noexcept;
    const AdaptiveV3ManagerSession *v3() const noexcept;
private: struct State; std::unique_ptr<State> state_;
};
} // namespace hotstuff
#endif
