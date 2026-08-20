/**
 * Pure adaptive-v2 manager baseline, selection, and successor controller.
 */

#ifndef HOTSTUFF_ADAPTIVE_V2_MANAGER_CONTROLLER_H_INCLUDED
#define HOTSTUFF_ADAPTIVE_V2_MANAGER_CONTROLLER_H_INCLUDED

#include <cstdint>
#include <memory>
#include <optional>
#include <vector>

#include "hotstuff/adaptive_v2_epoch_factory.h"
#include "hotstuff/adaptive_v2_manager_ingress.h"
#include "hotstuff/adaptive_v2_shape_selection.h"

namespace hotstuff
{

struct AdaptiveV2ManagerControllerConfig
{
    AdaptiveV2SelectionConfig selection;
    EvidenceReputationLimits reputation_limits;
    TreePlacementInput placement;
    std::uint64_t activation_delay_blocks{0};
    EpochChangeIssuerId issuer_id{0};
    PrivKeySecp256k1 issuer_private_key;
    EpochChangeBundleLimits bundle_limits;
    // Keep new defaulted fields after the original positional aggregate shape.
    AdaptiveV2TransitionPolicy transition_policy;
    ShapeV1Config shape_selection;
    bool shape_adaptation_enabled{false};
    EpochProtocolMode successor_protocol_mode{EpochProtocolMode::adaptive_v2};
};

enum class AdaptiveV2ManagerControllerStatus : std::uint8_t
{
    awaiting_readiness = 1,
    awaiting_responsive_baseline,
    baseline_frozen,
    awaiting_guarded_selection,
    successor_ready,
    already_ready,
    unhealthy,
};

enum class AdaptiveV2ManagerControllerFailureStage : std::uint8_t
{
    operational_precondition = 1,
    baseline_selection,
    guarded_selection,
    successor_factory,
};

struct AdaptiveV2ManagerControllerFailureDetail
{
    AdaptiveV2ManagerControllerFailureStage stage{
        AdaptiveV2ManagerControllerFailureStage::operational_precondition};
    std::optional<AdaptiveV2SelectionStatus> selection_status;
    std::optional<AdaptiveV2EpochFactoryStatus> epoch_factory_status;
};

/**
 * Single-writer policy controller over one borrowed manager ingress.
 *
 * The ingress must outlive this object and must not be mutated concurrently
 * with evaluate() or any returned borrowed view. The controller owns its
 * selection state and at most one immutable signed successor bundle. It has
 * no crash-id input and cannot mutate ingress membership, quorum, readiness,
 * lifecycle, ledger, epoch activation, voting, or certificate state.
 */
class AdaptiveV2ManagerController final
{
public:
    AdaptiveV2ManagerController(
        const AdaptiveV2ManagerIngress &ingress,
        AdaptiveV2ManagerControllerConfig config);
    ~AdaptiveV2ManagerController();

    AdaptiveV2ManagerController(
        const AdaptiveV2ManagerController &) = delete;
    AdaptiveV2ManagerController &operator=(
        const AdaptiveV2ManagerController &) = delete;
    AdaptiveV2ManagerController(
        AdaptiveV2ManagerController &&) = delete;
    AdaptiveV2ManagerController &operator=(
        AdaptiveV2ManagerController &&) = delete;

    AdaptiveV2ManagerControllerStatus evaluate() noexcept;
    bool arm_fault_window(AdaptiveV2FaultWindowArm arm) noexcept;

    const AdaptationSnapshot *baseline_audit_snapshot() const noexcept;
    const AdaptiveV2SelectionResult *selection_audit() const noexcept;
    const std::vector<EvidenceReputationAuditUpdate> &
    score_trajectory() const noexcept;
    const AdaptiveV2EpochChangeBundle *successor_bundle() const noexcept;
    const AdaptiveV3EpochChangeBundle *successor_bundle_v3() const noexcept;
    const ShapeDecisionRecord *shape_decision() const noexcept;
    const AdaptiveV2ManagerControllerFailureDetail *
    failure_detail() const noexcept;

    std::uint64_t baseline_cutoff() const noexcept;
    std::uint64_t current_cutoff() const noexcept;
    bool baseline_frozen() const noexcept;
    bool healthy() const noexcept;

private:
    struct State;
    std::unique_ptr<State> state_;
};

} // namespace hotstuff

#endif
