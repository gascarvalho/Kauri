#include "hotstuff/adaptive_v2_manager_delivery.h"

#include <algorithm>
#include <set>

namespace hotstuff
{

AdaptiveV2BundleDeliveryResult assess_adaptive_v2_bundle_delivery(
    const std::vector<ReplicaID> &configured_recipients,
    std::size_t required_recipients,
    const std::vector<AdaptiveV2BundleDeliveryAttempt> &attempts)
{
    AdaptiveV2BundleDeliveryResult result;
    result.configured_recipients = configured_recipients.size();
    result.attempted_recipients = attempts.size();
    result.required_recipients = required_recipients;

    auto membership = configured_recipients;
    std::sort(membership.begin(), membership.end());
    if (membership.empty() ||
        std::adjacent_find(membership.begin(), membership.end()) !=
            membership.end())
    {
        result.status =
            AdaptiveV2BundleDeliveryStatus::invalid_membership;
        return result;
    }
    if (required_recipients == 0 ||
        required_recipients > membership.size())
    {
        result.status = AdaptiveV2BundleDeliveryStatus::
            invalid_required_recipients;
        return result;
    }
    if (attempts.size() != membership.size())
    {
        result.status =
            AdaptiveV2BundleDeliveryStatus::invalid_attempt_set;
        return result;
    }

    std::set<ReplicaID> attempted;
    for (const auto &attempt : attempts)
    {
        if (!std::binary_search(
                membership.begin(), membership.end(), attempt.recipient) ||
            !attempted.insert(attempt.recipient).second)
        {
            result.status =
                AdaptiveV2BundleDeliveryStatus::invalid_attempt_set;
            result.successful_recipients = 0;
            return result;
        }
        if (attempt.enqueued)
            ++result.successful_recipients;
    }

    result.status = AdaptiveV2BundleDeliveryStatus::assessed;
    result.delivery_requirement_satisfied =
        result.successful_recipients >= required_recipients;
    return result;
}

} // namespace hotstuff
