/**
 * Bounded response-attempt tracking and immutable response facts.
 */

#ifndef HOTSTUFF_RESPONSE_ATTEMPT_H_INCLUDED
#define HOTSTUFF_RESPONSE_ATTEMPT_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <limits>
#include <memory>
#include <optional>
#include <vector>

#include "hotstuff/evidence.h"

namespace hotstuff
{

struct ResponseAttemptKey
{
    ProposalKey proposal;
    ReplicaID observed_replica_id{0};
    ExpectedMessageType expected_message_type{
        ExpectedMessageType::direct_vote};

    bool operator==(const ResponseAttemptKey &other) const noexcept
    {
        return proposal == other.proposal &&
               observed_replica_id == other.observed_replica_id &&
               expected_message_type == other.expected_message_type;
    }

    bool operator!=(const ResponseAttemptKey &other) const noexcept
    {
        return !(*this == other);
    }
};

struct ResponseAttemptHandle
{
    ResponseAttemptKey key;
    std::uint64_t generation{0};

    bool operator==(const ResponseAttemptHandle &other) const noexcept
    {
        return key == other.key && generation == other.generation;
    }

    bool operator!=(const ResponseAttemptHandle &other) const noexcept
    {
        return !(*this == other);
    }
};

struct ResponseAttemptArm
{
    ResponseAttemptKey key;
    std::uint64_t start_monotonic_ns{0};
    std::uint64_t deadline_duration_us{0};
};

struct ResponseAttemptFact
{
    ResponseAttemptKey key;
    ResponseOutcome outcome{ResponseOutcome::on_time};
    std::uint64_t response_duration_us{0};
    std::uint64_t deadline_duration_us{0};
    std::uint64_t fact_monotonic_ns{0};
    std::vector<ReplicaID> signer_set;
};

struct ResponseAttemptLimits
{
    std::size_t maximum_attempts{4096};
    std::size_t maximum_signers_per_response{4096};
    std::uint64_t maximum_generation{
        std::numeric_limits<std::uint64_t>::max()};
};

/**
 * Pure state machine with an explicit caller trust boundary.
 *
 * The tracker is event-loop confined and must be externally serialized.
 * Callers provide ordered monotonic callbacks and a topology-verified signer
 * set. The tracker owns no timers or protocol effects; generation handles
 * only correlate callbacks with an arm.
 */
class ResponseAttemptTracker final
{
public:
    explicit ResponseAttemptTracker(ResponseAttemptLimits limits = {});
    ~ResponseAttemptTracker();

    ResponseAttemptTracker(const ResponseAttemptTracker &) = delete;
    ResponseAttemptTracker &operator=(const ResponseAttemptTracker &) = delete;
    ResponseAttemptTracker(ResponseAttemptTracker &&) = delete;
    ResponseAttemptTracker &operator=(ResponseAttemptTracker &&) = delete;

    std::optional<ResponseAttemptHandle> arm(
        const ResponseAttemptArm &attempt);

    std::optional<ResponseAttemptFact> record_response(
        const ResponseAttemptHandle &handle,
        std::uint64_t response_monotonic_ns,
        const std::vector<ReplicaID> &canonical_verified_signers);

    std::optional<ResponseAttemptFact> record_timeout(
        const ResponseAttemptHandle &handle,
        std::uint64_t timeout_monotonic_ns);

    std::size_t retire(const ProposalKey &proposal) noexcept;
    void shutdown() noexcept;

    std::size_t size() const noexcept;
    bool healthy() const noexcept;

private:
    struct State;
    std::unique_ptr<State> state_;
};

} // namespace hotstuff

#endif
