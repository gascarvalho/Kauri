/**
 * Long-lived ownership for recurring adaptive-v2 manager cycles.
 */

#ifndef HOTSTUFF_ADAPTIVE_V2_MANAGER_SESSION_H_INCLUDED
#define HOTSTUFF_ADAPTIVE_V2_MANAGER_SESSION_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <vector>

#include "hotstuff/adaptive_v2_manager_controller.h"
#include "hotstuff/adaptive_v2_manager_convergence.h"

namespace hotstuff
{

struct AdaptiveV2ManagerSessionConfig
{
    std::uint32_t active_tree_id{0};
    std::uint64_t activation_generation{0};
    AdaptiveV2ManagerIngressLimits ingress_limits;
    AdaptiveV2ManagerControllerConfig controller;
    std::uint64_t retry_interval_ticks{0};
    std::uint32_t maximum_attempts_per_recipient{0};
    std::uint64_t convergence_window_ticks{0};
};

enum class AdaptiveV2ManagerCycleOutcome : std::uint8_t
{
    advanced = 1,
    no_op,
    failed,
};

enum class AdaptiveV2ManagerCycleTerminalReason : std::uint8_t
{
    successor_converged = 1,
    explicit_no_op,
    controller_unhealthy,
    convergence_start_failed,
    convergence_retry_exhausted,
    convergence_conflicting_observation,
    invalid_terminal_identity,
    successor_rotation_failed,
    evidence_window_reset_failed,
    caller_failed,
};

/**
 * Immutable audit result for one converged exact-successor cycle.
 *
 * The record is appended before the session rotates its ingress window. It
 * describes manager observation only; it is not a vote, certificate, or
 * source of epoch activation authority.
 */
struct AdaptiveV2ManagerSessionTerminalRecord
{
    std::uint64_t cycle_ordinal{0};
    TreePolicyKind policy_intent{
        TreePolicyKind::performance_optimization};
    AdaptiveV2ManagerCycleOutcome outcome{
        AdaptiveV2ManagerCycleOutcome::failed};
    AdaptiveV2ManagerCycleTerminalReason reason{
        AdaptiveV2ManagerCycleTerminalReason::caller_failed};
    std::uint32_t predecessor_epoch_number{0};
    uint256_t predecessor_epoch_digest;
    std::optional<std::uint32_t> successor_epoch_number;
    std::optional<uint256_t> successor_epoch_digest;
    std::optional<uint256_t> command_payload_digest;
    std::optional<AdaptiveV2EpochChangeIdentity> winning_activation;
    std::optional<AdaptiveV2ManagerControllerFailureDetail>
        controller_failure;
    std::size_t accepted_commit_count{0};
    std::size_t accepted_activation_count{0};
};

struct AdaptiveV2ManagerControllerAuditSnapshot
{
    std::uint64_t baseline_cutoff{0};
    std::uint64_t current_cutoff{0};
    bool baseline_frozen{false};
    std::vector<EvidenceReputationAuditUpdate> score_trajectory;
    std::optional<ShapeDecisionRecord> shape_decision;
    std::optional<AdaptiveV2ManagerControllerFailureDetail>
        controller_failure;
};

struct AdaptiveV2ManagerConvergenceAuditSnapshot
{
    AdaptiveV2ManagerConvergenceStatus status{
        AdaptiveV2ManagerConvergenceStatus::awaiting_activations};
    std::size_t accepted_commit_count{0};
    std::size_t accepted_activation_count{0};
    std::size_t winning_activation_count{0};
    std::optional<AdaptiveV2EpochChangeIdentity> winning_identity;
    std::vector<ReplicaID> winning_activation_sources;
};

/**
 * Single-writer owner for a sequence of exact adaptive-v2 transitions.
 *
 * The ingress, its epoch store, and authenticated-source replay fences live
 * for the whole session. A controller and convergence object are deliberately
 * one-shot and are replaced only after the current successor has converged
 * and its terminal record has been appended.
 */
class AdaptiveV2ManagerSession final
{
public:
    AdaptiveV2ManagerSession(
        std::vector<ReplicaID> membership,
        EpochDefinitionInput initial_epoch,
        AdaptiveV2ManagerSessionConfig config);
    ~AdaptiveV2ManagerSession();

    AdaptiveV2ManagerSession(
        const AdaptiveV2ManagerSession &) = delete;
    AdaptiveV2ManagerSession &operator=(
        const AdaptiveV2ManagerSession &) = delete;
    AdaptiveV2ManagerSession(
        AdaptiveV2ManagerSession &&) = delete;
    AdaptiveV2ManagerSession &operator=(
        AdaptiveV2ManagerSession &&) = delete;

    const AdaptiveV2ManagerIngress &ingress() const noexcept;

    AdaptiveV2ManagerReadinessResult ingest_readiness(
        const AuthenticatedReporter &authenticated_source,
        const MsgAdaptiveV2ReadinessNotice &message) noexcept;
    AdaptiveV2ManagerReadinessResult ingest_readiness(
        const AuthenticatedReporter &authenticated_source,
        const bytearray_t &canonical_payload) noexcept;
    AdaptiveV2ManagerLifecycleResult ingest_lifecycle(
        const AuthenticatedReporter &authenticated_source,
        const MsgProposalLifecycleNotice &message) noexcept;
    AdaptiveV2ManagerLifecycleResult ingest_lifecycle(
        const AuthenticatedReporter &authenticated_source,
        const bytearray_t &canonical_payload) noexcept;
    AdaptiveV2ManagerEvidenceResult ingest_evidence(
        const AuthenticatedReporter &authenticated_reporter,
        const MsgEvidenceReport &message) noexcept;
    AdaptiveV2ManagerEvidenceResult ingest_evidence(
        const AuthenticatedReporter &authenticated_reporter,
        const bytearray_t &canonical_payload) noexcept;

    bool begin_cycle(
        const AdaptiveV2TransitionPolicy &policy) noexcept;
    AdaptiveV2ManagerControllerStatus evaluate() noexcept;
    const AdaptiveV2EpochChangeBundle *successor_bundle() const noexcept;

    bool start_convergence(std::uint64_t logical_start_tick) noexcept;
    std::vector<AdaptiveV2ManagerDeliveryRequest> due_deliveries(
        std::uint64_t logical_tick) noexcept;
    AdaptiveV2ManagerConvergenceDisposition record_enqueue_result(
        ReplicaID recipient,
        std::uint32_t attempt,
        bool enqueued) noexcept;
    AdaptiveV2ManagerConvergenceDisposition observe_commit(
        ReplicaID authenticated_replica,
        const AdaptiveV2EpochChangeCommittedObservation &observation)
        noexcept;
    AdaptiveV2ManagerConvergenceDisposition observe_activation(
        ReplicaID authenticated_replica,
        const AdaptiveV2EpochActivatedObservation &observation) noexcept;
    std::optional<AdaptiveV2ManagerConvergenceStatus>
    convergence_status() const noexcept;
    std::optional<AdaptiveV2ManagerControllerAuditSnapshot>
    controller_audit() const noexcept;
    std::optional<AdaptiveV2ManagerConvergenceAuditSnapshot>
    convergence_audit() const noexcept;
    bool consume_ready_and_rotate() noexcept;

    bool finalize_noop_cycle(
        AdaptiveV2ManagerCycleTerminalReason reason) noexcept;
    bool finalize_failed_cycle(
        AdaptiveV2ManagerCycleTerminalReason reason) noexcept;

    const std::vector<AdaptiveV2ManagerSessionTerminalRecord> &
    terminal_records() const noexcept;
    void shutdown() noexcept;

private:
    bool finalize_convergence_failure_if_needed() noexcept;
    AdaptiveV2ManagerConvergenceDisposition
    classify_terminal_commit(
        ReplicaID authenticated_replica,
        const AdaptiveV2EpochChangeCommittedObservation &observation,
        bool &matched) const noexcept;
    AdaptiveV2ManagerConvergenceDisposition
    classify_terminal_activation(
        ReplicaID authenticated_replica,
        const AdaptiveV2EpochActivatedObservation &observation,
        bool &matched) const noexcept;

    struct State;
    std::unique_ptr<State> state_;
};

} // namespace hotstuff

#endif
