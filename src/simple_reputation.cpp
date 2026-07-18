#include "hotstuff/simple_reputation.h"

#include <limits>
#include <stdexcept>

namespace hotstuff
{

SimpleReputation::SimpleReputation(std::vector<ReplicaID> membership)
{
    if (membership.empty())
        throw std::invalid_argument("reputation membership is empty");
    for (const auto replica : membership)
    {
        if (!scores_.emplace(replica, 0).second)
            throw std::invalid_argument(
                "reputation membership contains a duplicate");
    }
}

SimpleReputationUpdate SimpleReputation::observe_response(
    ReplicaID reporter_id,
    ReplicaID target_id)
{
    return observe(
        reporter_id,
        target_id,
        SimpleReputationOutcome::response,
        1);
}

SimpleReputationUpdate SimpleReputation::observe_timeout(
    ReplicaID reporter_id,
    ReplicaID target_id)
{
    return observe(
        reporter_id,
        target_id,
        SimpleReputationOutcome::timeout,
        -1);
}

int SimpleReputation::score(ReplicaID target_id) const
{
    const auto found = scores_.find(target_id);
    return found == scores_.end() ? 0 : found->second;
}

SimpleReputationUpdate SimpleReputation::observe(
    ReplicaID reporter_id,
    ReplicaID target_id,
    SimpleReputationOutcome outcome,
    int delta)
{
    SimpleReputationUpdate update{
        reporter_id,
        target_id,
        outcome,
        0,
        score(target_id),
        SimpleReputationDisposition::applied};

    if (scores_.count(reporter_id) == 0)
    {
        update.disposition =
            SimpleReputationDisposition::unknown_reporter;
        return update;
    }

    const auto target = scores_.find(target_id);
    if (target == scores_.end())
    {
        update.disposition =
            SimpleReputationDisposition::unknown_target;
        return update;
    }

    if (reporter_id == target_id)
    {
        update.disposition =
            SimpleReputationDisposition::self_observation;
        return update;
    }

    if ((delta > 0 &&
         target->second == std::numeric_limits<int>::max()) ||
        (delta < 0 &&
         target->second == std::numeric_limits<int>::min()))
    {
        update.disposition =
            SimpleReputationDisposition::score_overflow;
        return update;
    }

    target->second += delta;
    update.delta = delta;
    update.score = target->second;
    return update;
}

} // namespace hotstuff
