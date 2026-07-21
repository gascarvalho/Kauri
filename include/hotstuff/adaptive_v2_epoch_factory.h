/**
 * Pure construction of signed adaptive-v2 successor epoch bundles.
 */

#ifndef HOTSTUFF_ADAPTIVE_V2_EPOCH_FACTORY_H_INCLUDED
#define HOTSTUFF_ADAPTIVE_V2_EPOCH_FACTORY_H_INCLUDED

#include <cstdint>
#include <memory>
#include <vector>

#include "hotstuff/adaptive_v2_selection.h"
#include "hotstuff/epoch_change_bundle.h"
#include "hotstuff/tree_policy.h"

namespace hotstuff
{

/**
 * Explicit policy input for one adaptive-v2 successor transition.
 *
 * Performance optimization is the compatibility default because the original
 * factory exposed only that behavior. Fault containment additionally requires
 * one baseline root for every requested tree.
 */
struct AdaptiveV2TransitionPolicy
{
    TreePolicyKind intent{TreePolicyKind::performance_optimization};
    std::vector<BaselineRoot> containment_baseline_roots;
};

enum class AdaptiveV2EpochFactoryStatus : std::uint8_t
{
    success = 1,
    invalid_current_epoch,
    epoch_number_exhausted,
    invalid_selection,
    epoch_mismatch,
    membership_mismatch,
    root_mismatch,
    tree_count_mismatch,
    insufficient_leaf_capacity,
    invalid_activation_delay,
    capacity_exceeded,
    placement_failed,
    authorization_failed,
    bundle_failed,
    internal_failure,
};

struct AdaptiveV2EpochFactoryResult
{
    AdaptiveV2EpochFactoryStatus status{
        AdaptiveV2EpochFactoryStatus::internal_failure};
    std::unique_ptr<const AdaptiveV2EpochChangeBundle> bundle;

    explicit operator bool() const noexcept
    {
        return status == AdaptiveV2EpochFactoryStatus::success &&
               bundle != nullptr;
    }
};

/**
 * Validate one successful Byzantine-guarded selection and turn it into a
 * signed, immutable adaptive-v2 successor bundle.
 *
 * The crash/containment set is accepted only through `selection`; there is no
 * independent fault-list input. The fixed membership and consensus quorum are
 * never changed by this operation.
 */
AdaptiveV2EpochFactoryResult build_adaptive_v2_successor_bundle(
    const EpochDefinition &current_epoch,
    const AdaptiveV2SelectionResult &selection,
    const AdaptiveV2TransitionPolicy &transition_policy,
    const TreePlacementInput &placement_input,
    std::uint64_t activation_delay_blocks,
    EpochChangeIssuerId issuer_id,
    const PrivKeySecp256k1 &issuer_private_key,
    const EpochChangeBundleLimits &bundle_limits) noexcept;

/**
 * Compatibility overload preserving the original optimization-only API.
 */
AdaptiveV2EpochFactoryResult build_adaptive_v2_successor_bundle(
    const EpochDefinition &current_epoch,
    const AdaptiveV2SelectionResult &selection,
    const TreePlacementInput &placement_input,
    std::uint64_t activation_delay_blocks,
    EpochChangeIssuerId issuer_id,
    const PrivKeySecp256k1 &issuer_private_key,
    const EpochChangeBundleLimits &bundle_limits) noexcept;

} // namespace hotstuff

#endif
