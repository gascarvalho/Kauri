#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <iterator>
#include <limits>
#include <memory>
#include <new>
#include <optional>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/evidence.h"

namespace response_attempt_allocation_failure
{

constexpr std::size_t disabled = std::numeric_limits<std::size_t>::max();
thread_local std::size_t allocations_before_failure = disabled;

bool consume_failure() noexcept
{
    if (allocations_before_failure == disabled)
        return false;
    if (allocations_before_failure == 0)
    {
        allocations_before_failure = disabled;
        return true;
    }
    --allocations_before_failure;
    return false;
}

void disable() noexcept
{
    allocations_before_failure = disabled;
}

class OneShot final
{
public:
    explicit OneShot(std::size_t successful_allocations) noexcept
    {
        allocations_before_failure = successful_allocations;
    }

    ~OneShot()
    {
        disable();
    }

    OneShot(const OneShot &) = delete;
    OneShot &operator=(const OneShot &) = delete;
};

} // namespace response_attempt_allocation_failure

void *operator new(std::size_t size)
{
    if (response_attempt_allocation_failure::consume_failure())
        throw std::bad_alloc();
    if (size == 0)
        size = 1;
    if (auto *const allocation = std::malloc(size))
        return allocation;
    throw std::bad_alloc();
}

void operator delete(void *allocation) noexcept
{
    std::free(allocation);
}

void operator delete(void *allocation, std::size_t) noexcept
{
    std::free(allocation);
}

/*
 * E08 exact response-attempt contract
 * -----------------------------------
 * The tracker is a pure, bounded state machine for one reporter's direct
 * children. Its identity is exactly proposal + child + expected message type.
 * It owns no timers and performs no consensus effect. Arm returns a unique
 * generation handle; callers use that capability to deliver already ordered
 * monotonic response/timeout callbacks and consume immutable facts.
 *
 * The fallback declares the complete public seam while the production header
 * is absent. Tracker methods remain deliberately undefined so this target
 * object-compiles but fails to link with an exact missing-production signal.
 */
#if __has_include("hotstuff/response_attempt.h")
#include "hotstuff/response_attempt.h"
#define KAURI_HAS_RESPONSE_ATTEMPT_API 1
#else
#define KAURI_HAS_RESPONSE_ATTEMPT_API 0

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

namespace
{

using hotstuff::ConfigurationId;
using hotstuff::ExpectedMessageType;
using hotstuff::ProposalKey;
using hotstuff::ReplicaID;
using hotstuff::ResponseAttemptArm;
using hotstuff::ResponseAttemptFact;
using hotstuff::ResponseAttemptHandle;
using hotstuff::ResponseAttemptKey;
using hotstuff::ResponseAttemptLimits;
using hotstuff::ResponseAttemptTracker;
using hotstuff::ResponseOutcome;
using hotstuff::uint256_t;

constexpr std::uint64_t kNanosecondsPerMicrosecond = 1000;
constexpr std::uint64_t kStartNs = 1'000'000;
constexpr std::uint64_t kDeadlineUs = 100;

uint256_t fixture_digest(const std::string &label)
{
    return hotstuff::DataStream(label).get_hash();
}

ConfigurationId configuration(
    std::uint32_t epoch,
    std::uint32_t tree,
    const std::string &digest_label)
{
    return {epoch, tree, fixture_digest(digest_label)};
}

ProposalKey proposal(
    const ConfigurationId &configuration_id,
    const uint256_t &block_hash)
{
    return {configuration_id, block_hash};
}

ProposalKey proposal(
    std::uint32_t epoch,
    std::uint32_t tree,
    const std::string &label)
{
    return proposal(
        configuration(epoch, tree, label + "-configuration"),
        fixture_digest(label + "-block"));
}

ResponseAttemptKey attempt_key(
    const ProposalKey &proposal_key,
    ReplicaID child,
    ExpectedMessageType type = ExpectedMessageType::direct_vote)
{
    return {proposal_key, child, type};
}

ResponseAttemptArm arm(
    const ResponseAttemptKey &key,
    std::uint64_t start_ns = kStartNs,
    std::uint64_t deadline_us = kDeadlineUs)
{
    return {key, start_ns, deadline_us};
}

ResponseAttemptLimits limits(
    std::size_t maximum_attempts = 16,
    std::size_t maximum_signers = 16,
    std::uint64_t maximum_generation =
        std::numeric_limits<std::uint64_t>::max())
{
    return {maximum_attempts, maximum_signers, maximum_generation};
}

std::uint64_t after_microseconds(
    std::uint64_t start_ns,
    std::uint64_t elapsed_us,
    std::uint64_t remainder_ns = 0)
{
    return start_ns + elapsed_us * kNanosecondsPerMicrosecond +
           remainder_ns;
}

ResponseAttemptHandle require_arm(
    ResponseAttemptTracker &tracker,
    const ResponseAttemptArm &attempt)
{
    const auto handle = tracker.arm(attempt);
    REQUIRE(handle.has_value());
    return *handle;
}

void check_fact(
    const std::optional<ResponseAttemptFact> &fact,
    const ResponseAttemptKey &key,
    ResponseOutcome outcome,
    std::uint64_t duration_us,
    std::uint64_t deadline_us,
    std::uint64_t fact_ns,
    const std::vector<ReplicaID> &signers)
{
    REQUIRE(fact.has_value());
    CHECK(fact->key == key);
    CHECK(fact->outcome == outcome);
    CHECK(fact->response_duration_us == duration_us);
    CHECK(fact->deadline_duration_us == deadline_us);
    CHECK(fact->fact_monotonic_ns == fact_ns);
    CHECK(fact->signer_set == signers);
}

struct InjectedCallResult
{
    bool threw_bad_alloc{false};
    bool returned_value{false};
};

template <typename Callable>
InjectedCallResult inject_allocation_failure(
    std::size_t successful_allocations,
    Callable &&callable)
{
    InjectedCallResult result;
    try
    {
        response_attempt_allocation_failure::OneShot fault(
            successful_allocations);
        result.returned_value = callable();
    }
    catch (const std::bad_alloc &)
    {
        result.threw_bad_alloc = true;
    }
    return result;
}

std::string read_source(const std::string &relative_path)
{
#ifdef KAURI_PROJECT_SOURCE_DIR
    const std::string root = KAURI_PROJECT_SOURCE_DIR;
#else
    const std::string root = ".";
#endif
    std::ifstream input(root + "/" + relative_path);
    REQUIRE(input.good());
    return std::string(
        std::istreambuf_iterator<char>(input),
        std::istreambuf_iterator<char>());
}

} // namespace

TEST_CASE("E08 response-attempt API pins a pure immutable fact seam",
          "[e08][response-attempt][contract][intentional-red]")
{
    CHECK(KAURI_HAS_RESPONSE_ATTEMPT_API == 1);
    CHECK(ResponseAttemptLimits{}.maximum_attempts > 0);
    CHECK(ResponseAttemptLimits{}.maximum_signers_per_response > 0);
    CHECK(ResponseAttemptLimits{}.maximum_generation ==
          std::numeric_limits<std::uint64_t>::max());

    static_assert(
        !std::is_copy_constructible<ResponseAttemptTracker>::value,
        "an attempt tracker must have one state owner");
    static_assert(
        !std::is_move_constructible<ResponseAttemptTracker>::value,
        "callbacks must not outlive a moved tracker owner");

    const auto key = attempt_key(
        proposal(4, 2, "contract"),
        7,
        ExpectedMessageType::aggregate_relay);
    const auto value = arm(key, 9'000, 250);

    static_assert(
        std::is_same<decltype(ResponseAttemptHandle{}.generation),
                     std::uint64_t>::value,
        "attempt generations must use a fixed-width monotonic counter");

    CHECK(value.key.proposal == key.proposal);
    CHECK(value.key.observed_replica_id == 7);
    CHECK(value.key.expected_message_type ==
          ExpectedMessageType::aggregate_relay);
    CHECK(value.start_monotonic_ns == 9'000);
    CHECK(value.deadline_duration_us == 250);
}

TEST_CASE("response callback wins waiting even beyond nominal deadline",
          "[e08][response-attempt][winner][on-time][intentional-red]")
{
    ResponseAttemptTracker tracker(limits());
    const auto key = attempt_key(proposal(5, 1, "slow-response"), 2);
    const auto handle = require_arm(tracker, arm(key));

    const std::vector<ReplicaID> signers{2};
    const auto response_ns = after_microseconds(kStartNs, 250, 999);
    const auto fact = tracker.record_response(
        handle, response_ns, signers);
    check_fact(
        fact,
        key,
        ResponseOutcome::on_time,
        250,
        kDeadlineUs,
        response_ns,
        signers);

    CHECK_FALSE(
        tracker.record_response(
                   handle, response_ns + 1, signers)
            .has_value());
    CHECK_FALSE(
        tracker.record_timeout(handle, response_ns + 1).has_value());
    CHECK(tracker.size() == 1);
    CHECK(tracker.healthy());
}

TEST_CASE("timeout winner emits one timeout and one correlated late fact",
          "[e08][response-attempt][winner][timeout][late]"
          "[intentional-red]")
{
    ResponseAttemptTracker tracker(limits());
    const auto key = attempt_key(
        proposal(6, 3, "timeout-late"),
        3,
        ExpectedMessageType::aggregate_relay);
    const auto handle = require_arm(tracker, arm(key));

    const auto timeout_ns = after_microseconds(kStartNs, kDeadlineUs);
    const auto timeout = tracker.record_timeout(handle, timeout_ns);
    check_fact(
        timeout,
        key,
        ResponseOutcome::timeout,
        0,
        kDeadlineUs,
        timeout_ns,
        {});
    CHECK_FALSE(
        tracker.record_timeout(handle, timeout_ns + 1).has_value());

    const std::vector<ReplicaID> signers{3, 5, 6};
    const auto late_ns = after_microseconds(kStartNs, 175, 999);
    const auto late = tracker.record_response(handle, late_ns, signers);
    check_fact(
        late,
        key,
        ResponseOutcome::late,
        175,
        kDeadlineUs,
        late_ns,
        signers);

    CHECK_FALSE(
        tracker.record_response(handle, late_ns + 1, signers).has_value());
    CHECK_FALSE(
        tracker.record_timeout(handle, late_ns + 1).has_value());
    CHECK(tracker.healthy());
}

TEST_CASE("late fact cannot precede its immutable deadline",
          "[e08][response-attempt][late][deadline][fail-closed]"
          "[intentional-red]")
{
    ResponseAttemptTracker tracker(limits());
    const auto key = attempt_key(proposal(7, 1, "early-late"), 4);
    const auto handle = require_arm(tracker, arm(key));

    const auto timeout_ns = after_microseconds(kStartNs, kDeadlineUs);
    REQUIRE(tracker.record_timeout(handle, timeout_ns).has_value());

    const auto invalid_late_ns =
        after_microseconds(kStartNs, kDeadlineUs) - 1;
    CHECK_FALSE(
        tracker.record_response(handle, invalid_late_ns, {4}).has_value());
    CHECK_FALSE(tracker.healthy());
    CHECK(tracker.size() == 1);

    const auto valid_late_ns =
        after_microseconds(kStartNs, kDeadlineUs, 1);
    const auto late = tracker.record_response(
        handle, valid_late_ns, {4});
    check_fact(
        late,
        key,
        ResponseOutcome::late,
        kDeadlineUs,
        kDeadlineUs,
        valid_late_ns,
        {4});
    CHECK_FALSE(tracker.healthy());
}

TEST_CASE("late fact cannot regress behind its timeout fact timestamp",
          "[e08][response-attempt][late][monotonic][retryable]"
          "[intentional-red]")
{
    ResponseAttemptTracker tracker(limits());
    const auto key = attempt_key(proposal(20, 1, "late-regression"), 4);
    const auto handle = require_arm(tracker, arm(key));

    const auto timeout_ns = after_microseconds(kStartNs, 200);
    REQUIRE(tracker.record_timeout(handle, timeout_ns).has_value());

    const auto regressing_late_ns = timeout_ns - 1;
    CHECK_FALSE(
        tracker.record_response(
                   handle, regressing_late_ns, {4})
            .has_value());
    CHECK_FALSE(tracker.healthy());
    CHECK(tracker.size() == 1);

    check_fact(
        tracker.record_response(handle, timeout_ns, {4}),
        key,
        ResponseOutcome::late,
        200,
        kDeadlineUs,
        timeout_ns,
        {4});
    CHECK_FALSE(tracker.healthy());
}

TEST_CASE("rearm cannot replace immutable timing",
          "[e08][response-attempt][arm][immutable][intentional-red]")
{
    ResponseAttemptTracker tracker(limits());
    const auto key = attempt_key(proposal(8, 2, "immutable-arm"), 5);
    const auto original = arm(key, 20'000, 75);
    const auto handle = require_arm(tracker, original);

    CHECK_FALSE(tracker.arm(original).has_value());
    CHECK(tracker.healthy());
    CHECK(tracker.size() == 1);

    auto changed_start = original;
    ++changed_start.start_monotonic_ns;
    CHECK_FALSE(tracker.arm(changed_start).has_value());
    CHECK_FALSE(tracker.healthy());

    auto changed_deadline = original;
    ++changed_deadline.deadline_duration_us;
    CHECK_FALSE(tracker.arm(changed_deadline).has_value());
    CHECK(tracker.size() == 1);

    const auto response_ns = after_microseconds(
        original.start_monotonic_ns, 50, 999);
    const auto fact = tracker.record_response(handle, response_ns, {5});
    check_fact(
        fact,
        key,
        ResponseOutcome::on_time,
        50,
        original.deadline_duration_us,
        response_ns,
        {5});
}

TEST_CASE("attempt identity isolates configuration child and message type",
          "[e08][response-attempt][identity][exact-key]"
          "[intentional-red]")
{
    ResponseAttemptTracker tracker(limits());
    const auto shared_hash = fixture_digest("shared-block");
    const auto proposal_a = proposal(
        configuration(9, 1, "identity-a"), shared_hash);
    const auto proposal_b = proposal(
        configuration(10, 1, "identity-b"), shared_hash);

    const auto base = attempt_key(proposal_a, 2);
    const auto other_child = attempt_key(proposal_a, 3);
    const auto other_type = attempt_key(
        proposal_a, 2, ExpectedMessageType::aggregate_relay);
    const auto other_configuration = attempt_key(proposal_b, 2);

    const auto base_handle = require_arm(tracker, arm(base));
    const auto child_handle = require_arm(tracker, arm(other_child));
    const auto type_handle = require_arm(tracker, arm(other_type));
    const auto configuration_handle =
        require_arm(tracker, arm(other_configuration));
    CHECK(tracker.size() == 4);

    const auto event_ns = after_microseconds(kStartNs, kDeadlineUs);
    check_fact(
        tracker.record_timeout(base_handle, event_ns),
        base,
        ResponseOutcome::timeout,
        0,
        kDeadlineUs,
        event_ns,
        {});
    check_fact(
        tracker.record_response(child_handle, event_ns, {3}),
        other_child,
        ResponseOutcome::on_time,
        kDeadlineUs,
        kDeadlineUs,
        event_ns,
        {3});
    check_fact(
        tracker.record_response(type_handle, event_ns, {2, 4}),
        other_type,
        ResponseOutcome::on_time,
        kDeadlineUs,
        kDeadlineUs,
        event_ns,
        {2, 4});
    check_fact(
        tracker.record_response(configuration_handle, event_ns, {2}),
        other_configuration,
        ResponseOutcome::on_time,
        kDeadlineUs,
        kDeadlineUs,
        event_ns,
        {2});
    CHECK(tracker.healthy());
}

TEST_CASE("invalid arms fail closed without creating attempts",
          "[e08][response-attempt][validation][arm][intentional-red]")
{
    const auto valid_key = attempt_key(proposal(11, 2, "invalid-arm"), 6);

    SECTION("leader progress is not a direct-child response")
    {
        ResponseAttemptTracker tracker(limits());
        auto invalid = arm(valid_key);
        invalid.key.expected_message_type =
            ExpectedMessageType::leader_progress;
        CHECK_FALSE(tracker.arm(invalid).has_value());
        CHECK_FALSE(tracker.healthy());
        CHECK(tracker.size() == 0);
    }

    SECTION("unknown message type is rejected")
    {
        ResponseAttemptTracker tracker(limits());
        auto invalid = arm(valid_key);
        invalid.key.expected_message_type =
            static_cast<ExpectedMessageType>(255);
        CHECK_FALSE(tracker.arm(invalid).has_value());
        CHECK_FALSE(tracker.healthy());
        CHECK(tracker.size() == 0);
    }

    SECTION("deadline is positive")
    {
        ResponseAttemptTracker tracker(limits());
        CHECK_FALSE(
            tracker.arm(arm(valid_key, kStartNs, 0)).has_value());
        CHECK_FALSE(tracker.healthy());
        CHECK(tracker.size() == 0);
    }

    SECTION("deadline conversion cannot overflow")
    {
        ResponseAttemptTracker tracker(limits());
        const auto overflow_us =
            std::numeric_limits<std::uint64_t>::max() /
                kNanosecondsPerMicrosecond +
            1;
        CHECK_FALSE(
            tracker.arm(arm(valid_key, 0, overflow_us)).has_value());
        CHECK_FALSE(tracker.healthy());
        CHECK(tracker.size() == 0);
    }

    SECTION("absolute deadline cannot overflow")
    {
        ResponseAttemptTracker tracker(limits());
        const auto start =
            std::numeric_limits<std::uint64_t>::max() - 500;
        CHECK_FALSE(
            tracker.arm(arm(valid_key, start, 1)).has_value());
        CHECK_FALSE(tracker.healthy());
        CHECK(tracker.size() == 0);
    }
}

TEST_CASE("clock regression fails closed without consuming waiting state",
          "[e08][response-attempt][clock][fail-closed]"
          "[intentional-red]")
{
    SECTION("response callback")
    {
        ResponseAttemptTracker tracker(limits());
        const auto key = attempt_key(
            proposal(12, 1, "response-clock"), 2);
        const auto handle = require_arm(tracker, arm(key));

        CHECK_FALSE(
            tracker.record_response(
                       handle, kStartNs - 1, {2})
                .has_value());
        CHECK_FALSE(tracker.healthy());
        CHECK(tracker.size() == 1);

        const auto response_ns = after_microseconds(kStartNs, 10);
        check_fact(
            tracker.record_response(handle, response_ns, {2}),
            key,
            ResponseOutcome::on_time,
            10,
            kDeadlineUs,
            response_ns,
            {2});
    }

    SECTION("timeout callback")
    {
        ResponseAttemptTracker tracker(limits());
        const auto key = attempt_key(
            proposal(12, 1, "timeout-clock"), 3);
        const auto handle = require_arm(tracker, arm(key));

        CHECK_FALSE(
            tracker.record_timeout(handle, kStartNs - 1).has_value());
        CHECK_FALSE(tracker.healthy());
        CHECK(tracker.size() == 1);

        const auto timeout_ns = after_microseconds(kStartNs, kDeadlineUs);
        check_fact(
            tracker.record_timeout(handle, timeout_ns),
            key,
            ResponseOutcome::timeout,
            0,
            kDeadlineUs,
            timeout_ns,
            {});
    }
}

TEST_CASE("timeout callback must reach the immutable deadline",
          "[e08][response-attempt][timeout][deadline][fail-closed]"
          "[intentional-red]")
{
    ResponseAttemptTracker tracker(limits());
    const auto key = attempt_key(proposal(12, 2, "early-timeout"), 4);
    const auto handle = require_arm(tracker, arm(key));

    const auto due_ns = after_microseconds(kStartNs, kDeadlineUs);
    CHECK_FALSE(
        tracker.record_timeout(handle, due_ns - 1).has_value());
    CHECK_FALSE(tracker.healthy());
    CHECK(tracker.size() == 1);

    check_fact(
        tracker.record_timeout(handle, due_ns),
        key,
        ResponseOutcome::timeout,
        0,
        kDeadlineUs,
        due_ns,
        {});
    CHECK_FALSE(tracker.healthy());
}

TEST_CASE("responses require bounded canonical verified signers",
          "[e08][response-attempt][signers][canonical][bounded]"
          "[intentional-red]")
{
    const auto run_invalid = [](
        const std::string &label,
        const std::vector<ReplicaID> &invalid_signers,
        ResponseAttemptLimits tracker_limits = limits()) {
        ResponseAttemptTracker tracker(tracker_limits);
        const auto key = attempt_key(
            proposal(13, 2, label),
            4,
            ExpectedMessageType::aggregate_relay);
        const auto handle = require_arm(tracker, arm(key));
        const auto response_ns = after_microseconds(kStartNs, 25);

        CHECK_FALSE(
            tracker.record_response(
                       handle, response_ns, invalid_signers)
                .has_value());
        CHECK_FALSE(tracker.healthy());
        CHECK(tracker.size() == 1);

        check_fact(
            tracker.record_response(handle, response_ns, {4, 5}),
            key,
            ResponseOutcome::on_time,
            25,
            kDeadlineUs,
            response_ns,
            {4, 5});
    };
    const auto run_invalid_direct = [](
        const std::string &label,
        const std::vector<ReplicaID> &invalid_signers) {
        ResponseAttemptTracker tracker(limits());
        const auto key = attempt_key(proposal(13, 3, label), 4);
        const auto handle = require_arm(tracker, arm(key));
        const auto response_ns = after_microseconds(kStartNs, 25);

        CHECK_FALSE(
            tracker.record_response(
                       handle, response_ns, invalid_signers)
                .has_value());
        CHECK_FALSE(tracker.healthy());
        CHECK(tracker.size() == 1);

        check_fact(
            tracker.record_response(handle, response_ns, {4}),
            key,
            ResponseOutcome::on_time,
            25,
            kDeadlineUs,
            response_ns,
            {4});
    };

    SECTION("empty")
    {
        run_invalid("empty-signers", {});
    }
    SECTION("out of order")
    {
        run_invalid("unordered-signers", {5, 4});
    }
    SECTION("duplicate")
    {
        run_invalid("duplicate-signers", {4, 4});
    }
    SECTION("over configured bound")
    {
        run_invalid("many-signers", {3, 4, 5}, limits(4, 2));
    }
    SECTION("direct vote from the wrong signer")
    {
        run_invalid_direct("wrong-direct-signer", {5});
    }
    SECTION("direct vote with extra signers")
    {
        run_invalid_direct("extra-direct-signer", {4, 5});
    }
}

TEST_CASE("attempt capacity is bounded and reclaimed only by retirement",
          "[e08][response-attempt][capacity][retire][intentional-red]")
{
    ResponseAttemptTracker tracker(limits(1, 4));
    const auto proposal_a = proposal(14, 1, "capacity-a");
    const auto proposal_b = proposal(14, 1, "capacity-b");
    const auto key_a = attempt_key(proposal_a, 2);
    const auto key_b = attempt_key(proposal_b, 3);

    const auto handle_a = require_arm(tracker, arm(key_a));
    CHECK(handle_a.key == key_a);
    CHECK_FALSE(tracker.arm(arm(key_b)).has_value());
    CHECK_FALSE(tracker.healthy());
    CHECK(tracker.size() == 1);

    CHECK(tracker.retire(proposal_a) == 1);
    CHECK(tracker.size() == 0);
    const auto handle_b = tracker.arm(arm(key_b));
    REQUIRE(handle_b.has_value());
    CHECK(handle_b->key == key_b);
    CHECK(tracker.size() == 1);
    CHECK_FALSE(tracker.healthy());
}

TEST_CASE("arm allocation failure is fail closed and logically atomic",
          "[e08][response-attempt][allocation][arm][atomic]"
          "[intentional-red]")
{
    constexpr std::size_t allocation_sweep = 16;
    std::size_t injected_failures = 0;
    std::vector<std::size_t> partial_state;
    std::vector<std::size_t> inconsistent_retry;

    for (std::size_t allocation = 0;
         allocation < allocation_sweep;
         ++allocation)
    {
        ResponseAttemptTracker tracker(limits(16, 16, 1));
        const auto key = attempt_key(
            proposal(15, 1, "arm-allocation-" +
                                    std::to_string(allocation)),
            2);
        const auto value = arm(key);

        const auto result = inject_allocation_failure(
            allocation,
            [&]() { return tracker.arm(value).has_value(); });
        const auto failed = result.threw_bad_alloc ||
                            (!result.returned_value &&
                             !tracker.healthy());
        if (!failed)
            continue;
        ++injected_failures;

        if (tracker.healthy() || tracker.size() != 0)
            partial_state.push_back(allocation);

        const auto retry = tracker.arm(value);
        if (!retry.has_value() || tracker.size() != 1 ||
            tracker.healthy() || retry->generation != 1)
        {
            inconsistent_retry.push_back(allocation);
        }
    }

    CAPTURE(injected_failures);
    CAPTURE(partial_state);
    CAPTURE(inconsistent_retry);
    REQUIRE(injected_failures > 0);
    CHECK(partial_state.empty());
    CHECK(inconsistent_retry.empty());
}

TEST_CASE("response allocation failure does not consume the winning callback",
          "[e08][response-attempt][allocation][response][atomic]"
          "[intentional-red]")
{
    constexpr std::size_t allocation_sweep = 16;
    std::size_t injected_failures = 0;
    std::vector<std::size_t> partial_state;
    std::vector<std::size_t> inconsistent_retry;

    for (std::size_t allocation = 0;
         allocation < allocation_sweep;
         ++allocation)
    {
        ResponseAttemptTracker tracker(limits(4, 16));
        const auto key = attempt_key(
            proposal(16, 1, "response-allocation-" +
                                    std::to_string(allocation)),
            2,
            ExpectedMessageType::aggregate_relay);
        const auto handle = require_arm(tracker, arm(key));
        const std::vector<ReplicaID> signers{2, 3, 4, 5, 6, 7};
        const auto response_ns = after_microseconds(kStartNs, 50);

        const auto result = inject_allocation_failure(
            allocation,
            [&]() {
                return tracker.record_response(
                           handle, response_ns, signers)
                    .has_value();
            });
        const auto failed = result.threw_bad_alloc ||
                            (!result.returned_value &&
                             !tracker.healthy());
        if (!failed)
            continue;
        ++injected_failures;

        if (tracker.healthy() || tracker.size() != 1)
            partial_state.push_back(allocation);

        const auto retry =
            tracker.record_response(handle, response_ns, signers);
        if (!retry.has_value() ||
            retry->outcome != ResponseOutcome::on_time ||
            retry->response_duration_us != 50 ||
            retry->signer_set != signers || tracker.healthy())
        {
            inconsistent_retry.push_back(allocation);
        }
    }

    CAPTURE(injected_failures);
    CAPTURE(partial_state);
    CAPTURE(inconsistent_retry);
    REQUIRE(injected_failures > 0);
    CHECK(partial_state.empty());
    CHECK(inconsistent_retry.empty());
}

TEST_CASE("retirement is exact and makes stale callbacks inert",
          "[e08][response-attempt][retire][exact-key][stale-callback]"
          "[intentional-red]")
{
    ResponseAttemptTracker tracker(limits());
    const auto shared_hash = fixture_digest("retirement-shared-block");
    const auto retired_proposal = proposal(
        configuration(17, 1, "retired-configuration"), shared_hash);
    const auto retained_proposal = proposal(
        configuration(18, 1, "retained-configuration"), shared_hash);
    const auto retired_direct = attempt_key(retired_proposal, 2);
    const auto retired_aggregate = attempt_key(
        retired_proposal, 3, ExpectedMessageType::aggregate_relay);
    const auto retained = attempt_key(retained_proposal, 2);

    const auto direct_handle =
        require_arm(tracker, arm(retired_direct));
    const auto aggregate_handle =
        require_arm(tracker, arm(retired_aggregate));
    const auto retained_handle = require_arm(tracker, arm(retained));
    REQUIRE(tracker.size() == 3);

    CHECK(tracker.retire(retired_proposal) == 2);
    CHECK(tracker.size() == 1);
    CHECK(tracker.retire(retired_proposal) == 0);

    const auto event_ns = after_microseconds(kStartNs, kDeadlineUs);
    CHECK_FALSE(
        tracker.record_response(direct_handle, event_ns, {2})
            .has_value());
    CHECK_FALSE(
        tracker.record_timeout(aggregate_handle, event_ns).has_value());
    CHECK(tracker.healthy());

    check_fact(
        tracker.record_response(retained_handle, event_ns, {2}),
        retained,
        ResponseOutcome::on_time,
        kDeadlineUs,
        kDeadlineUs,
        event_ns,
        {2});
}

TEST_CASE("retired callbacks cannot affect a rearmed exact key",
          "[e08][response-attempt][retire][generation][stale-callback]"
          "[intentional-red]")
{
    SECTION("stale timeout")
    {
        ResponseAttemptTracker tracker(limits());
        const auto key = attempt_key(
            proposal(21, 1, "rearm-stale-timeout"), 2);
        const auto retired_handle = require_arm(tracker, arm(key));
        REQUIRE(tracker.retire(key.proposal) == 1);

        const auto new_start = after_microseconds(kStartNs, 500);
        const auto current_handle = require_arm(
            tracker, arm(key, new_start, kDeadlineUs));
        CHECK(current_handle.generation > retired_handle.generation);
        const auto new_due = after_microseconds(new_start, kDeadlineUs);

        CHECK_FALSE(
            tracker.record_timeout(retired_handle, new_due).has_value());
        CHECK(tracker.healthy());
        CHECK(tracker.size() == 1);

        check_fact(
            tracker.record_timeout(current_handle, new_due),
            key,
            ResponseOutcome::timeout,
            0,
            kDeadlineUs,
            new_due,
            {});
    }

    SECTION("stale response")
    {
        ResponseAttemptTracker tracker(limits());
        const auto key = attempt_key(
            proposal(21, 1, "rearm-stale-response"), 3);
        const auto retired_handle = require_arm(tracker, arm(key));
        REQUIRE(tracker.retire(key.proposal) == 1);

        const auto new_start = after_microseconds(kStartNs, 500);
        const auto current_handle = require_arm(
            tracker, arm(key, new_start, kDeadlineUs));
        CHECK(current_handle.generation > retired_handle.generation);
        const auto response_ns = after_microseconds(new_start, 25);

        CHECK_FALSE(
            tracker.record_response(
                       retired_handle, response_ns, {3})
                .has_value());
        CHECK(tracker.healthy());
        CHECK(tracker.size() == 1);

        check_fact(
            tracker.record_response(current_handle, response_ns, {3}),
            key,
            ResponseOutcome::on_time,
            25,
            kDeadlineUs,
            response_ns,
            {3});
    }
}

TEST_CASE("attempt generation overflow fails closed without partial rearm",
          "[e08][response-attempt][generation][overflow][atomic]"
          "[intentional-red]")
{
    ResponseAttemptTracker tracker(limits(4, 4, 1));
    const auto key = attempt_key(
        proposal(22, 1, "generation-overflow"), 2);
    const auto retired_handle = require_arm(tracker, arm(key));
    REQUIRE(retired_handle.generation == 1);
    REQUIRE(tracker.retire(key.proposal) == 1);
    REQUIRE(tracker.size() == 0);

    const auto new_start = after_microseconds(kStartNs, 500);
    CHECK_FALSE(
        tracker.arm(arm(key, new_start, kDeadlineUs)).has_value());
    CHECK_FALSE(tracker.healthy());
    CHECK(tracker.size() == 0);

    const auto event_ns = after_microseconds(new_start, kDeadlineUs);
    CHECK_FALSE(
        tracker.record_timeout(retired_handle, event_ns).has_value());
    CHECK(tracker.size() == 0);
}

TEST_CASE("shutdown clears attempts and permanently blocks new arms",
          "[e08][response-attempt][shutdown][stale-callback]"
          "[intentional-red]")
{
    ResponseAttemptTracker tracker(limits());
    const auto first = attempt_key(proposal(19, 1, "shutdown-a"), 2);
    const auto second = attempt_key(proposal(19, 1, "shutdown-b"), 3);
    const auto first_handle = require_arm(tracker, arm(first));
    const auto second_handle = require_arm(tracker, arm(second));
    REQUIRE(tracker.size() == 2);

    tracker.shutdown();
    CHECK(tracker.size() == 0);
    CHECK(tracker.healthy());

    const auto event_ns = after_microseconds(kStartNs, kDeadlineUs);
    CHECK_FALSE(
        tracker.record_timeout(first_handle, event_ns).has_value());
    CHECK_FALSE(
        tracker.record_response(second_handle, event_ns, {3}).has_value());
    CHECK_FALSE(tracker.arm(arm(first)).has_value());
    CHECK(tracker.size() == 0);

    tracker.shutdown();
    CHECK(tracker.size() == 0);
}

TEST_CASE("response-attempt implementation has no protocol side effects",
          "[e08][response-attempt][source-audit][pure]"
          "[intentional-red]")
{
    const auto header = read_source("include/hotstuff/response_attempt.h");
    const auto source = read_source("src/response_attempt.cpp");
    const auto implementation = header + "\n" + source;

    CHECK(header.find("ResponseAttemptKey") != std::string::npos);
    CHECK(header.find("ResponseAttemptHandle") != std::string::npos);
    CHECK(header.find("ResponseAttemptArm") != std::string::npos);
    CHECK(header.find("ResponseAttemptFact") != std::string::npos);
    CHECK(header.find("ResponseAttemptTracker") != std::string::npos);
    CHECK(header.find("std::function") == std::string::npos);

    INFO("the public API documents its external caller trust boundary");
    for (const auto *required_contract :
         {"caller trust boundary",
          "event-loop confined",
          "externally serialized",
          "ordered monotonic callbacks",
          "topology-verified signer"})
    {
        INFO("Missing API contract phrase: " << required_contract);
        CHECK(header.find(required_contract) != std::string::npos);
    }

    INFO("the pure tracker cannot own network or runtime objects");
    for (const auto *forbidden :
         {"HotStuffBase",
          "HotStuffCore",
          "PeerNetwork",
          "MsgNetwork",
          "EventContext",
          "send_msg("})
    {
        CHECK(implementation.find(forbidden) == std::string::npos);
    }

    INFO("facts cannot mutate quorum, vote, leader, or timer state");
    for (const auto *forbidden :
         {"QuorumCert",
          "quorum_cert",
          "on_receive_vote",
          "add_verified_part",
          "TimerEvent",
          "schedule_after",
          "LeaderProgressMonitor",
          "rotate_active_view",
          "set_proposer",
          "inc_time("})
    {
        CHECK(implementation.find(forbidden) == std::string::npos);
    }

    INFO("attempt tracking cannot own evidence admission or ranking policy");
    for (const auto *forbidden :
         {"EvidenceLedger",
          "AdaptationSnapshot",
          "ReplicaAdaptationResult",
          "build_adaptation_snapshot",
          "AdaptationManager"})
    {
        CHECK(implementation.find(forbidden) == std::string::npos);
    }
}
