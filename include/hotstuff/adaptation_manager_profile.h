/**
 * Runtime-derived bounds for the adaptive-v2 manager executable.
 */

#ifndef HOTSTUFF_ADAPTATION_MANAGER_PROFILE_H_INCLUDED
#define HOTSTUFF_ADAPTATION_MANAGER_PROFILE_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <optional>
#include <vector>

#include "hotstuff/adaptive_v2_manager_ingress.h"
#include "hotstuff/epoch_change_bundle.h"
#include "hotstuff/tree_policy.h"

namespace hotstuff
{

/**
 * One immutable manager shape derived from canonical configured membership.
 *
 * This value is descriptive configuration only. It cannot override the
 * Byzantine quorum derived from N=3f+1 and grants no consensus authority.
 */
struct AdaptiveV2ManagerRuntimeShape
{
    ByzantineQuorum quorum;
    std::uint32_t required_nonresponsive{0};
    std::uint32_t minimum_score_drop{0};
    std::size_t maximum_post_baseline_timeout_attempts{0};
    TreeShape tree_shape;
    AdaptiveV2ManagerIngressLimits ingress_limits;
    EpochChangeBundleLimits bundle_limits;
};

/**
 * Derive all membership- and topology-dependent manager bounds.
 *
 * Membership must be the exact contiguous sequence [0, N), N must satisfy
 * N=3f+1 with f>0, and the requested topology must fit the existing bounded
 * adaptive-v2 policy and ingress APIs. No caller-supplied f or Q is accepted.
 */
std::optional<AdaptiveV2ManagerRuntimeShape>
derive_adaptive_v2_manager_runtime_shape(
    const std::vector<ReplicaID> &membership,
    std::uint32_t tree_fanout,
    std::uint32_t pipeline_stretch) noexcept;

/**
 * Derive the canonical adaptive-v2 epoch-zero cyclic tree schedule.
 *
 * The result contains one tree per member. Tree r starts with root r and then
 * lists every remaining member in cyclic contiguous order. Validation is
 * identical to the manager runtime-shape validation, so callers cannot create
 * a digest witness for a shape the manager would reject.
 */
std::optional<EpochDefinitionInput>
derive_adaptive_v2_cyclic_epoch_zero(
    const std::vector<ReplicaID> &membership,
    std::uint32_t tree_fanout,
    std::uint32_t pipeline_stretch);

} // namespace hotstuff

#endif
