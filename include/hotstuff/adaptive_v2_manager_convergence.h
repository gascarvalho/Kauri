/**
 * Transport-independent adaptive-v2 delivery and activation convergence.
 */

#ifndef HOTSTUFF_ADAPTIVE_V2_MANAGER_CONVERGENCE_H_INCLUDED
#define HOTSTUFF_ADAPTIVE_V2_MANAGER_CONVERGENCE_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <memory>
#include <vector>

#include "hotstuff/adaptive_v2_convergence_wire.h"
#include "hotstuff/epoch_change_bundle.h"

namespace hotstuff
{

struct AdaptiveV2ManagerConvergenceConfig
{
    std::vector<ReplicaID> membership;
    std::uint64_t retry_interval_ticks{0};
    std::uint32_t maximum_attempts_per_recipient{0};
    std::uint64_t convergence_deadline_tick{0};
};

/**
 * One opaque transport request.
 *
 * canonical_bundle_bytes aliases canonical_bundle_owner. Copies of this
 * request keep the immutable bytes alive independently of the convergence
 * object, and every retry aliases the same bytearray object.
 */
struct AdaptiveV2ManagerDeliveryRequest
{
    ReplicaID recipient{0};
    std::uint32_t attempt{0};
    const bytearray_t *canonical_bundle_bytes{nullptr};
    std::shared_ptr<const bytearray_t> canonical_bundle_owner;
};

enum class AdaptiveV2ManagerConvergenceStatus : std::uint8_t
{
    awaiting_activations = 1,
    ready_for_optimization,
    retry_exhausted,
    conflicting_observation,
};

enum class AdaptiveV2ManagerConvergenceDisposition : std::uint8_t
{
    accepted = 1,
    duplicate,
    advisory_enqueue_recorded,
    conflicting_enqueue_result,
    rejected_nonmember,
    rejected_spoofed_source,
    rejected_stale,
    rejected_wrong_identity,
    conflicting_observation,
    terminal,
};

/**
 * Single-writer convergence state for one immutable successor bundle.
 *
 * Enqueue results and commit observations are advisory. Activation
 * observations are grouped by their complete committed identity, and only a
 * clean group of distinct authenticated sources can reach the Byzantine
 * threshold derived from the bundle's complete fixed membership. A source
 * that equivocates is quarantined and contributes to no candidate group.
 * This object never creates a second successor and has no membership, quorum,
 * voting, or activation authority.
 */
class AdaptiveV2ManagerConvergence final
{
public:
    AdaptiveV2ManagerConvergence(
        const AdaptiveV2EpochChangeBundle &bundle,
        AdaptiveV2ManagerConvergenceConfig config,
        std::uint64_t start_tick);
    ~AdaptiveV2ManagerConvergence();

    AdaptiveV2ManagerConvergence(
        const AdaptiveV2ManagerConvergence &) = delete;
    AdaptiveV2ManagerConvergence &operator=(
        const AdaptiveV2ManagerConvergence &) = delete;
    AdaptiveV2ManagerConvergence(
        AdaptiveV2ManagerConvergence &&) = delete;
    AdaptiveV2ManagerConvergence &operator=(
        AdaptiveV2ManagerConvergence &&) = delete;

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

    AdaptiveV2ManagerConvergenceStatus status() const noexcept;
    std::size_t accepted_commit_count() const noexcept;
    std::size_t accepted_activation_count() const noexcept;
    const AdaptiveV2EpochChangeIdentity *winning_identity() const
        noexcept;
    std::size_t winning_activation_count() const noexcept;
    const std::vector<ReplicaID> &winning_activation_sources() const
        noexcept;
    bool consume_ready_for_optimization() noexcept;

private:
    struct State;
    std::unique_ptr<State> state_;
};

} // namespace hotstuff

#endif
