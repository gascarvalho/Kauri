#ifndef HOTSTUFF_ADAPTIVE_V3_MANAGER_SESSION_H_INCLUDED
#define HOTSTUFF_ADAPTIVE_V3_MANAGER_SESSION_H_INCLUDED

#include <memory>
#include <optional>
#include <vector>

#include "hotstuff/adaptive_v2_manager_controller.h"
#include "hotstuff/adaptive_v2_manager_session.h"
#include "hotstuff/adaptive_v3_manager_activation.h"
#include "hotstuff/adaptive_v3_manager_readiness.h"
#include "hotstuff/adaptive_v3_reporting_outbox.h"

namespace hotstuff {

struct AdaptiveV3ManagerSessionConfig {
    std::uint32_t active_tree_id{0};
    std::uint64_t activation_generation{0};
    AdaptiveV2ManagerIngressLimits ingress_limits;
    AdaptiveV2ManagerControllerConfig controller;
    std::vector<std::pair<ReplicaID, PubKeyBLS>> readiness_membership;
    std::size_t required_release_count{0};
    std::uint32_t maximum_delivery_attempts{0};
    std::uint64_t retry_interval_ticks{0};
    std::uint64_t pre_certificate_window_ticks{0};
    std::uint64_t delivery_window_ticks{0};
    std::uint64_t residency_ticks{65000};
    // Defaults retain the millisecond unit used by session unit tests.  Live
    // callers may select a finer monotonic unit, but must supply every
    // protocol duration in that same unit.
    std::uint64_t common_commit_window_ticks{5000};
    std::uint64_t common_commit_stabilization_ticks{60000};
    std::uint64_t e2_reserve_ticks{90000};
    std::uint64_t expected_cycle_count{2};
    ActivationReadinessWireLimits wire_limits;
};

enum class AdaptiveV3ManagerSessionStatus : std::uint8_t {
    idle = 1, selecting, successor_available, collecting, distributing, residency, terminal,
};
enum class AdaptiveV3ManagerSessionTerminalReason : std::uint8_t {
    acknowledgements_complete = 1, pre_certificate_deadline,
    delivery_deadline, delivery_retry_exhausted, collector_conflict,
    invalid_rotation, common_commit_missing, hard_deadline_unarmed,
    hard_deadline_exhausted,
};
struct AdaptiveV3ManagerSessionTerminalRecord {
    std::uint64_t cycle_ordinal{0};
    AdaptiveV3ManagerSessionTerminalReason reason{
        AdaptiveV3ManagerSessionTerminalReason::invalid_rotation};
    uint256_t bundle_digest;
    std::optional<ActivationReadyIdentityV1> identity;
    std::vector<ReplicaID> q_seed_sources;
    std::vector<ReplicaID> r_audit_sources;
};

/** Immutable, allocation-safe copy of the E1 facts which authorize E2. */
struct AdaptiveV3E2EligibilityAuditSnapshot {
    std::uint64_t cycle_ordinal{0};
    std::optional<ActivationReadyIdentityV1> e1_identity;
    uint256_t e1_bundle_digest;
    std::uint64_t final_ack_tick{0};
    ProposalKey common_commit;
    std::vector<ReplicaID> common_commit_sources;
    std::uint64_t common_commit_tick{0};
    std::uint64_t earliest_e2_tick{0};
    std::uint64_t actual_e2_begin_tick{0};
    std::uint64_t hard_deadline_tick{0};
    std::uint64_t reserve_ticks{0};
};

/** Transport-independent recurring v3 manager. It owns no voting state and
 * derives a readiness identity only from an immutable emitted v3 bundle plus
 * authenticated signed observations. */
class AdaptiveV3ManagerSession final {
public:
    AdaptiveV3ManagerSession(std::vector<ReplicaID>, EpochDefinitionInput,
                             AdaptiveV3ManagerSessionConfig);
    ~AdaptiveV3ManagerSession();
    AdaptiveV3ManagerSession(const AdaptiveV3ManagerSession &) = delete;
    AdaptiveV3ManagerSession &operator=(const AdaptiveV3ManagerSession &) = delete;

    const AdaptiveV2ManagerIngress &ingress() const noexcept;
    AdaptiveV2ManagerReadinessResult ingest_readiness(
        const AuthenticatedReporter &, const MsgAdaptiveV2ReadinessNotice &) noexcept;
    AdaptiveV2ManagerReadinessResult ingest_readiness(
        const AuthenticatedReporter &, const bytearray_t &) noexcept;
    AdaptiveV2ManagerLifecycleResult ingest_lifecycle(
        const AuthenticatedReporter &, const MsgProposalLifecycleNotice &) noexcept;
    AdaptiveV2ManagerLifecycleResult ingest_lifecycle(
        const AuthenticatedReporter &, const bytearray_t &) noexcept;
    /** Admit a lifecycle fact and, after E1's final R ACK, retain only a
     * bounded all-R identical committed ProposalKey for the pre-E2 gate. */
    AdaptiveV2ManagerLifecycleResult ingest_timed_lifecycle(
        const AuthenticatedReporter &, const MsgProposalLifecycleNotice &,
        std::uint64_t manager_tick) noexcept;
    AdaptiveV2ManagerEvidenceResult ingest_evidence(
        const AuthenticatedReporter &, const MsgEvidenceReport &) noexcept;
    AdaptiveV2ManagerEvidenceResult ingest_evidence(
        const AuthenticatedReporter &, const bytearray_t &) noexcept;
    bool begin_cycle(const AdaptiveV2TransitionPolicy &) noexcept;
    bool arm_fault_window(AdaptiveV2FaultWindowArm) noexcept;
    bool arm_hard_deadline(std::uint64_t hard_deadline_tick) noexcept;
    AdaptiveV2ManagerControllerStatus evaluate() noexcept;
    std::optional<AdaptiveV2ManagerControllerAuditSnapshot>
    controller_audit() const noexcept;
    const AdaptiveV2ManagerControllerFailureDetail *controller_failure_detail() const noexcept;
    const AdaptiveV3EpochChangeBundle *successor_bundle() const noexcept;
    bool begin_readiness(std::uint64_t logical_tick) noexcept;
    AdaptiveV3ManagerObservationResult observe_readiness(
        ReplicaID tls_peer, std::uint64_t logical_tick, const bytearray_t &) noexcept;
    std::optional<AdaptiveV3CertificateDelivery> begin_delivery(
        ReplicaID, std::uint64_t) noexcept;
    AdaptiveV3CertificateDeliveryDisposition record_delivery_result(
        ReplicaID, std::uint32_t, bool, std::uint64_t) noexcept;
    AdaptiveV3CertificateDeliveryDisposition acknowledge(
        ReplicaID tls_peer, std::uint64_t logical_tick, const bytearray_t &) noexcept;
    bool e2_eligible(std::uint64_t manager_tick) const noexcept;
    std::optional<AdaptiveV3E2EligibilityAuditSnapshot>
    e2_eligibility_audit(std::uint64_t manager_tick) const noexcept;
    std::optional<AdaptiveV3E2EligibilityAuditSnapshot>
    begin_e2_at(std::uint64_t manager_tick,
                const AdaptiveV2TransitionPolicy &) noexcept;
    void advance(std::uint64_t logical_tick) noexcept;
    AdaptiveV3ManagerSessionStatus status() const noexcept;
    const AdaptiveV3ManagerSessionTerminalRecord *terminal_audit() const noexcept;
    const std::vector<AdaptiveV3ManagerSessionTerminalRecord> &terminal_records() const noexcept;
    const ActivationReadinessCertificateV1 *certificate() const noexcept;
private: struct State; std::unique_ptr<State> state_;
};
} // namespace hotstuff
#endif
