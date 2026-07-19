/**
 * Pure assessment of one-shot adaptive-v2 bundle delivery attempts.
 */

#ifndef HOTSTUFF_ADAPTIVE_V2_MANAGER_DELIVERY_H_INCLUDED
#define HOTSTUFF_ADAPTIVE_V2_MANAGER_DELIVERY_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <vector>

#include "hotstuff/type.h"

namespace hotstuff
{

struct AdaptiveV2BundleDeliveryAttempt
{
    ReplicaID recipient{0};
    bool enqueued{false};
};

enum class AdaptiveV2BundleDeliveryStatus : std::uint8_t
{
    assessed = 1,
    invalid_membership,
    invalid_required_recipients,
    invalid_attempt_set,
};

struct AdaptiveV2BundleDeliveryResult
{
    AdaptiveV2BundleDeliveryStatus status{
        AdaptiveV2BundleDeliveryStatus::invalid_attempt_set};
    std::size_t configured_recipients{0};
    std::size_t attempted_recipients{0};
    std::size_t successful_recipients{0};
    std::size_t required_recipients{0};
    bool delivery_requirement_satisfied{false};
};

/**
 * Assess an already completed one-shot delivery pass.
 *
 * Every configured recipient must appear exactly once, whether or not its
 * transport enqueue succeeded. The result is observational delivery state;
 * it has no evidence, ranking, membership, voting, or quorum authority.
 */
AdaptiveV2BundleDeliveryResult assess_adaptive_v2_bundle_delivery(
    const std::vector<ReplicaID> &configured_recipients,
    std::size_t required_recipients,
    const std::vector<AdaptiveV2BundleDeliveryAttempt> &attempts);

} // namespace hotstuff

#endif
