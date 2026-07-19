/**
 * Bounded target-only reputation scoring for the first live prototype.
 */

#ifndef HOTSTUFF_SIMPLE_REPUTATION_H_INCLUDED
#define HOTSTUFF_SIMPLE_REPUTATION_H_INCLUDED

#include <cstdint>
#include <map>
#include <vector>

#include "hotstuff/type.h"

namespace hotstuff
{

enum class SimpleReputationOutcome : std::uint8_t
{
    response = 1,
    timeout = 2,
};

enum class SimpleReputationDisposition : std::uint8_t
{
    applied = 1,
    unknown_reporter,
    unknown_target,
    self_observation,
    score_overflow,
};

struct SimpleReputationUpdate
{
    ReplicaID reporter_id{0};
    ReplicaID target_id{0};
    SimpleReputationOutcome outcome{SimpleReputationOutcome::response};
    int delta{0};
    int score{0};
    SimpleReputationDisposition disposition{
        SimpleReputationDisposition::applied};
};

/**
 * Single-writer prototype score table.
 *
 * A response changes only the observed target by +1 and a timeout changes
 * only the observed target by -1. Reporter identity is retained for audit but
 * is never scored. This class owns no consensus, quorum, topology, transport,
 * authentication, or fault-diagnosis behavior.
 */
class SimpleReputation final
{
public:
    explicit SimpleReputation(std::vector<ReplicaID> membership);

    SimpleReputationUpdate observe_response(
        ReplicaID reporter_id,
        ReplicaID target_id);

    SimpleReputationUpdate observe_timeout(
        ReplicaID reporter_id,
        ReplicaID target_id);

    bool contains(ReplicaID replica_id) const noexcept;
    int score(ReplicaID target_id) const;

private:
    SimpleReputationUpdate observe(
        ReplicaID reporter_id,
        ReplicaID target_id,
        SimpleReputationOutcome outcome,
        int delta);

    std::map<ReplicaID, int> scores_;
};

} // namespace hotstuff

#endif
