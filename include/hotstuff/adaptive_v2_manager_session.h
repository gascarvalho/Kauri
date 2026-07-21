/**
 * Long-lived ownership for recurring adaptive-v2 manager cycles.
 */

#ifndef HOTSTUFF_ADAPTIVE_V2_MANAGER_SESSION_H_INCLUDED
#define HOTSTUFF_ADAPTIVE_V2_MANAGER_SESSION_H_INCLUDED

#include <cstdint>
#include <limits>
#include <memory>
#include <vector>

#include "hotstuff/adaptive_v2_manager_controller.h"
#include "hotstuff/adaptive_v2_manager_convergence.h"
#include "hotstuff/epoch_activation.h"

namespace hotstuff
{

struct AdaptiveV2ManagerSessionConfig
{
    std::uint32_t active_tree_id{0};
    std::uint64_t activation_generation{0};
    std::uint32_t maximum_epoch_number{
        std::numeric_limits<std::uint32_t>::max()};
    std::uint64_t maximum_activation_generation{
        std::numeric_limits<std::uint64_t>::max()};
    AdaptiveV2ManagerIngressLimits ingress_limits;
    AdaptiveV2ManagerControllerConfig controller;
    std::uint64_t retry_interval_ticks{0};
    std::uint32_t maximum_attempts_per_recipient{0};
    std::uint64_t convergence_window_ticks{0};
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
    std::uint32_t predecessor_epoch_number{0};
    uint256_t predecessor_epoch_digest;
    std::uint32_t successor_epoch_number{0};
    uint256_t successor_epoch_digest;
    uint256_t command_payload_digest;
    AdaptiveV2EpochChangeIdentity winning_activation;
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

    AdaptiveV2ManagerIngress &ingress() noexcept;
    const AdaptiveV2ManagerIngress &ingress() const noexcept;

    bool begin_cycle(
        const AdaptiveV2TransitionPolicy &policy) noexcept;
    AdaptiveV2ManagerControllerStatus evaluate() noexcept;
    const AdaptiveV2EpochChangeBundle *successor_bundle() const noexcept;

    bool start_convergence(
        std::uint64_t command_block_height) noexcept;
    AdaptiveV2ManagerConvergenceDisposition observe_activation(
        ReplicaID authenticated_replica,
        const AdaptiveV2EpochActivatedObservation &observation) noexcept;
    bool consume_ready_and_rotate() noexcept;

    const std::vector<AdaptiveV2ManagerSessionTerminalRecord> &
    terminal_records() const noexcept;

private:
    struct State;
    std::unique_ptr<State> state_;
};

} // namespace hotstuff

#endif
