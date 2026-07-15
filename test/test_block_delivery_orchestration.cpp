#include <algorithm>
#include <csignal>
#include <cstddef>
#include <exception>
#include <functional>
#include <limits>
#include <memory>
#include <optional>
#include <stdexcept>
#include <sys/wait.h>
#include <unistd.h>
#include <utility>
#include <vector>

#include "catch.hpp"

/*
 * REM-A06-01 final production-orchestration contract
 * --------------------------------------------------
 * include/hotstuff/block_delivery_orchestration.h must expose the extracted
 * production seam used by HotStuffBase:
 *
 *   struct BlockDeliveryAsyncPlan {
 *       std::function<promise_t()> fetch_block;
 *       std::function<promise_t(const block_t &)> verify_block;
 *       std::function<promise_t(const block_t &)> fetch_qc_reference;
 *       std::function<std::vector<promise_t>(const block_t &)>
 *           deliver_parents;
 *       std::function<bool(const block_t &)> deliver_core;
 *   };
 *
 *   using ExternalBlockDelivery =
 *       std::function<bool(const block_t &)>;
 *
 *   class BlockDeliveryOrchestrator {
 *   public:
 *       BlockDeliveryOrchestrator();
 *       ~BlockDeliveryOrchestrator();
 *       promise_t async_delivery(const uint256_t &,
 *                                BlockDeliveryTimingHooks,
 *                                BlockDeliveryAsyncPlan);
 *       bool external_delivery(const uint256_t &, const block_t &,
 *                              ExternalBlockDelivery);
 *       void cancel(std::exception_ptr) noexcept;
 *       bool contains(const uint256_t &) const noexcept;
 *       std::size_t size() const noexcept;
 *       bool is_cancelled() const noexcept;
 *   };
 *
 * The orchestrator owns the lifecycle/finalizer. external_delivery catches a
 * thrown core-delivery exception, fails and erases the matching generation,
 * notifies all waiters, and only then rethrows. async_delivery starts one
 * shared attempt per hash, verifies true, fetches the QC reference, delivers
 * every parent, and invokes deliver_core only after all prerequisites pass.
 * Its callbacks hold the attempt weakly so an external winner, cancel, or
 * stale generation cannot later deliver or settle again. cancel marks the
 * owner closed before rejecting all generations, and destruction performs the
 * same deterministic cancellation.
 */
#if __has_include("hotstuff/block_delivery_orchestration.h")
#define KAURI_HAS_BLOCK_DELIVERY_ORCHESTRATION 1
#include "hotstuff/block_delivery_orchestration.h"
#else
#define KAURI_HAS_BLOCK_DELIVERY_ORCHESTRATION 0
#endif

#if !KAURI_HAS_BLOCK_DELIVERY_ORCHESTRATION

TEST_CASE("block delivery production orchestrator is available",
          "[rem-a06-01][orchestration][contract][red]")
{
    INFO("Missing include/hotstuff/block_delivery_orchestration.h. "
         "HotStuffBase still lacks the tests-first external exception, "
         "async prerequisite, competition, and shutdown seam.");
    REQUIRE(KAURI_HAS_BLOCK_DELIVERY_ORCHESTRATION == 1);
}

#else

namespace
{

using hotstuff::Block;
using hotstuff::BlockDeliveryAsyncPlan;
using hotstuff::BlockDeliveryOrchestrator;
using hotstuff::BlockDeliveryTimingHooks;
using hotstuff::DataStream;
using hotstuff::ExternalBlockDelivery;
using hotstuff::HotStuffCore;
using hotstuff::VeriPool;
using hotstuff::block_t;
using hotstuff::promise_t;
using hotstuff::uint256_t;

uint256_t digest(const char *label)
{
    return DataStream(label).get_hash();
}

block_t block(std::size_t height = 1)
{
    return new Block(true, height);
}

struct Observation
{
    std::size_t fulfilled{0};
    std::size_t rejected{0};
    block_t value;
    std::vector<promise_t> branches;

    void watch(const promise_t &delivery,
               std::function<void()> before_success = {})
    {
        branches.push_back(delivery.then(
            [this, before_success](const block_t &delivered)
            {
                if (before_success)
                    before_success();
                ++fulfilled;
                value = delivered;
            },
            [this]()
            {
                ++rejected;
            }));
    }
};

struct TimingProbe
{
    std::vector<double> samples;
    double total{0};
    double minimum{std::numeric_limits<double>::infinity()};
    double maximum{0};

    BlockDeliveryTimingHooks hooks(double elapsed)
    {
        return {
            [elapsed]()
            {
                return elapsed;
            },
            [this](double sample)
            {
                samples.push_back(sample);
                total += sample;
                minimum = std::min(minimum, sample);
                maximum = std::max(maximum, sample);
            }};
    }
};

void require_one_sample(const TimingProbe &timing, double expected)
{
    REQUIRE(timing.samples.size() == 1);
    CHECK(timing.samples.front() == Approx(expected));
    CHECK(timing.total == Approx(expected));
    CHECK(timing.minimum == Approx(expected));
    CHECK(timing.maximum == Approx(expected));
}

struct DeferredPlan
{
    promise_t fetched;
    promise_t verified;
    promise_t qc_fetched;
    promise_t parent_a;
    promise_t parent_b;
    std::size_t fetch_starts{0};
    std::size_t verify_starts{0};
    std::size_t qc_starts{0};
    std::size_t parent_starts{0};
    std::size_t core_deliveries{0};
    bool core_result{true};
    bool throw_from_core{false};
    std::function<promise_t(const block_t &)> verify_delegate;
    std::function<promise_t(const block_t &)> qc_delegate;
    std::shared_ptr<int> owner{std::make_shared<int>(7)};

    BlockDeliveryAsyncPlan make()
    {
        return {
            [this, keep_alive = owner]()
            {
                (void)keep_alive;
                ++fetch_starts;
                return fetched;
            },
            [this, keep_alive = owner](const block_t &block)
            {
                (void)keep_alive;
                ++verify_starts;
                if (verify_delegate)
                    return verify_delegate(block);
                return verified;
            },
            [this, keep_alive = owner](const block_t &block)
            {
                (void)keep_alive;
                ++qc_starts;
                if (qc_delegate)
                    return qc_delegate(block);
                return qc_fetched;
            },
            [this, keep_alive = owner](const block_t &)
            {
                (void)keep_alive;
                ++parent_starts;
                return std::vector<promise_t>{parent_a, parent_b};
            },
            [this, keep_alive = owner](const block_t &)
            {
                (void)keep_alive;
                ++core_deliveries;
                if (throw_from_core)
                    throw std::runtime_error("core delivery threw");
                return core_result;
            }};
    }
};

void start_prerequisites(DeferredPlan &plan, const block_t &value)
{
    plan.fetched.resolve(value);
    REQUIRE(plan.fetch_starts == 1);
    REQUIRE(plan.verify_starts == 1);
    REQUIRE(plan.qc_starts == 1);
    REQUIRE(plan.parent_starts == 1);
}

void complete_success(DeferredPlan &plan, const block_t &value)
{
    plan.verified.resolve(true);
    plan.qc_fetched.resolve(value);
    plan.parent_a.resolve(value);
    plan.parent_b.resolve(value);
}

int exercise_null_qc_contract()
{
    salticidae::EventContext event_context;
    VeriPool verification_pool(event_context, 0);
    BlockDeliveryOrchestrator orchestrator;
    const block_t malformed = new Block();
    const auto genesis = block();
    const auto hash = malformed->get_hash();
    if (malformed->get_qc() != nullptr || hash == genesis->get_hash())
        return 1;

    DeferredPlan malformed_plan;
    malformed_plan.verify_delegate =
        [&verification_pool](const block_t &candidate)
        {
            return candidate->verify(
                static_cast<const HotStuffCore *>(nullptr),
                verification_pool);
        };
    malformed_plan.qc_delegate = [](const block_t &candidate) -> promise_t
    {
        const auto &qc = candidate->get_qc();
        if (qc == nullptr)
            throw std::runtime_error(
                "fetched block has no quorum certificate");
        return promise_t();
    };

    std::weak_ptr<int> malformed_owner = malformed_plan.owner;
    auto delivery = orchestrator.async_delivery(
        hash, {}, malformed_plan.make());
    malformed_plan.owner.reset();

    Observation observation;
    observation.watch(delivery);
    std::size_t protocol_continuations = 0;
    auto forbidden_progress = delivery.then(
        [&protocol_continuations](const block_t &)
        {
            ++protocol_continuations;
        });

    // This enters the exact asynchronous Block::verify overload used by
    // HotStuffBase. The child process isolates the current null dereference.
    malformed_plan.fetched.resolve(malformed);

    if (malformed_plan.fetch_starts != 1 ||
        malformed_plan.verify_starts != 1 ||
        malformed_plan.qc_starts != 1)
        return 2;
    if (malformed_plan.parent_starts != 0 ||
        malformed_plan.core_deliveries != 0 ||
        protocol_continuations != 0)
        return 3;
    if (observation.fulfilled != 0 || observation.rejected != 1)
        return 4;
    if (orchestrator.contains(hash) || orchestrator.size() != 0)
        return 5;
    if (!malformed_owner.expired())
        return 6;

    DeferredPlan retry_plan;
    auto retry = orchestrator.async_delivery(
        hash, {}, retry_plan.make());
    Observation retry_observation;
    retry_observation.watch(retry);
    if (retry_plan.fetch_starts != 1 ||
        !orchestrator.contains(hash) || orchestrator.size() != 1)
        return 7;

    orchestrator.cancel(
        std::make_exception_ptr(std::runtime_error("test cleanup")));
    if (retry_observation.fulfilled != 0 ||
        retry_observation.rejected != 1 || orchestrator.size() != 0)
        return 8;
    (void)forbidden_progress;
    return 0;
}

} // namespace

TEST_CASE("external delivery exceptions erase before rethrow and isolate retry",
          "[rem-a06-01][orchestration][external][exception][retry]")
{
    BlockDeliveryOrchestrator orchestrator;
    const auto value = block(10);
    const auto hash = value->get_hash();
    TimingProbe timing;
    DeferredPlan failed_plan;
    auto failed = orchestrator.async_delivery(
        hash, timing.hooks(0.25), failed_plan.make());

    Observation failed_observation;
    failed_observation.watch(failed);
    std::size_t protocol_continuations = 0;
    auto forbidden_progress = failed.then(
        [&protocol_continuations](const block_t &)
        {
            ++protocol_continuations;
        });

    DeferredPlan retry_plan;
    std::optional<promise_t> retry;
    Observation retry_observation;
    bool erased_before_reentrant_retry = false;
    auto retry_branch = failed.fail(
        [&]()
        {
            erased_before_reentrant_retry = !orchestrator.contains(hash);
            retry = orchestrator.async_delivery(
                hash, timing.hooks(0.50), retry_plan.make());
            retry_observation.watch(*retry, [&timing]()
            {
                require_one_sample(timing, 0.50);
            });
        });

    ExternalBlockDelivery throwing_core = [](const block_t &) -> bool
    {
        throw std::runtime_error("missing parent or QC reference");
    };
    REQUIRE_THROWS_AS(
        orchestrator.external_delivery(hash, value, throwing_core),
        std::runtime_error);

    CHECK(erased_before_reentrant_retry);
    CHECK(failed_observation.fulfilled == 0);
    CHECK(failed_observation.rejected == 1);
    CHECK(protocol_continuations == 0);
    CHECK(timing.samples.empty());
    REQUIRE(retry.has_value());
    REQUIRE(orchestrator.size() == 1);

    // Completion from the erased generation is stale even though its fetch
    // source remains alive.
    failed_plan.fetched.resolve(value);
    CHECK(failed_plan.verify_starts == 0);
    CHECK(failed_plan.core_deliveries == 0);
    CHECK(retry_observation.fulfilled == 0);

    start_prerequisites(retry_plan, value);
    complete_success(retry_plan, value);
    CHECK(retry_observation.fulfilled == 1);
    CHECK(retry_observation.rejected == 0);
    CHECK(retry_plan.core_deliveries == 1);
    CHECK(orchestrator.size() == 0);
    require_one_sample(timing, 0.50);
    (void)forbidden_progress;
    (void)retry_branch;
}

TEST_CASE("async production prerequisites gate delivery in either order",
          "[rem-a06-01][orchestration][async][parents][qc][ordering]")
{
    bool verification_first = false;
    SECTION("verification completes first")
    {
        verification_first = true;
    }
    SECTION("verification completes last")
    {
        verification_first = false;
    }
    CAPTURE(verification_first);

    BlockDeliveryOrchestrator orchestrator;
    const auto value = block(11);
    const auto hash = value->get_hash();
    TimingProbe timing;
    DeferredPlan plan;
    auto delivery = orchestrator.async_delivery(
        hash, timing.hooks(0.375), plan.make());
    Observation observation;
    observation.watch(delivery, [&timing]()
    {
        require_one_sample(timing, 0.375);
    });
    std::size_t protocol_continuations = 0;
    auto continuation = delivery.then(
        [&protocol_continuations](const block_t &)
        {
            ++protocol_continuations;
        });

    start_prerequisites(plan, value);
    CHECK(observation.fulfilled == 0);
    CHECK(plan.core_deliveries == 0);

    if (verification_first)
    {
        plan.verified.resolve(true);
        plan.qc_fetched.resolve(value);
        plan.parent_b.resolve(value);
        CHECK(observation.fulfilled == 0);
        CHECK(plan.core_deliveries == 0);
        plan.parent_a.resolve(value);
    }
    else
    {
        plan.parent_a.resolve(value);
        plan.parent_b.resolve(value);
        plan.qc_fetched.resolve(value);
        CHECK(observation.fulfilled == 0);
        CHECK(plan.core_deliveries == 0);
        plan.verified.resolve(true);
    }

    CHECK(plan.core_deliveries == 1);
    CHECK(observation.fulfilled == 1);
    CHECK(observation.rejected == 0);
    CHECK(observation.value == value);
    CHECK(protocol_continuations == 1);
    CHECK(orchestrator.size() == 0);
    require_one_sample(timing, 0.375);
    (void)continuation;
}

TEST_CASE("async false rejection and throw fail without timing or progress",
          "[rem-a06-01][orchestration][async][failure-matrix]")
{
    const auto value = block(12);
    const auto hash = value->get_hash();
    BlockDeliveryOrchestrator orchestrator;
    TimingProbe timing;
    DeferredPlan plan;
    auto delivery = orchestrator.async_delivery(
        hash, timing.hooks(0.625), plan.make());
    Observation observation;
    observation.watch(delivery);
    std::size_t protocol_continuations = 0;
    auto continuation = delivery.then(
        [&protocol_continuations](const block_t &)
        {
            ++protocol_continuations;
        });

    start_prerequisites(plan, value);

    SECTION("verification false")
    {
        plan.verified.resolve(false);
    }
    SECTION("QC fetch rejected")
    {
        plan.qc_fetched.reject(
            std::make_exception_ptr(std::runtime_error("QC fetch rejected")));
    }
    SECTION("core delivery threw after every prerequisite")
    {
        plan.throw_from_core = true;
        complete_success(plan, value);
    }

    CHECK(observation.fulfilled == 0);
    CHECK(observation.rejected == 1);
    CHECK(protocol_continuations == 0);
    CHECK(timing.samples.empty());
    CHECK(orchestrator.size() == 0);
    (void)continuation;
}

TEST_CASE("null QC is rejected by production async verification and permits retry",
          "[rem-a06-01][orchestration][async][null-qc][retry]")
{
    const auto child = fork();
    REQUIRE(child >= 0);
    if (child == 0)
    {
        std::signal(SIGSEGV, SIG_DFL);
        std::signal(SIGBUS, SIG_DFL);
        _exit(exercise_null_qc_contract());
    }

    int child_status = 0;
    REQUIRE(waitpid(child, &child_status, 0) == child);
    const int terminating_signal = WIFSIGNALED(child_status)
        ? WTERMSIG(child_status)
        : 0;
    CAPTURE(child_status);
    CAPTURE(terminating_signal);
    INFO("The child must return from the real asynchronous Block::verify "
         "delegate instead of dereferencing a null QC");
    REQUIRE(WIFEXITED(child_status));

    const int contract_failure = WEXITSTATUS(child_status);
    CAPTURE(contract_failure);
    CHECK(contract_failure == 0);
}

TEST_CASE("external winner makes every asynchronous signal stale",
          "[rem-a06-01][orchestration][competition][generation]")
{
    BlockDeliveryOrchestrator orchestrator;
    const auto value = block(13);
    const auto hash = value->get_hash();
    TimingProbe timing;
    DeferredPlan plan;
    auto delivery = orchestrator.async_delivery(
        hash, timing.hooks(0.75), plan.make());
    Observation observation;
    observation.watch(delivery, [&timing]()
    {
        require_one_sample(timing, 0.75);
    });

    start_prerequisites(plan, value);
    std::size_t external_core_deliveries = 0;
    REQUIRE(orchestrator.external_delivery(
        hash,
        value,
        [&external_core_deliveries](const block_t &)
        {
            ++external_core_deliveries;
            return true;
        }));
    CHECK(external_core_deliveries == 1);
    CHECK(observation.fulfilled == 1);
    CHECK(orchestrator.size() == 0);
    require_one_sample(timing, 0.75);

    complete_success(plan, value);
    CHECK(plan.core_deliveries == 0);
    CHECK(observation.fulfilled == 1);
    CHECK(observation.rejected == 0);
    require_one_sample(timing, 0.75);
}

TEST_CASE("cancel and destruction reject and release outstanding attempts",
          "[rem-a06-01][orchestration][shutdown][lifetime]")
{
    const auto first_block = block(20);
    const auto second_block = block(21);
    const auto shutdown_error =
        std::make_exception_ptr(std::runtime_error("orchestrator shutdown"));

    SECTION("explicit cancel closes before notifying waiters")
    {
        BlockDeliveryOrchestrator orchestrator;
        DeferredPlan first_plan;
        DeferredPlan second_plan;
        std::weak_ptr<int> first_owner = first_plan.owner;
        std::weak_ptr<int> second_owner = second_plan.owner;
        auto first = orchestrator.async_delivery(
            first_block->get_hash(), {}, first_plan.make());
        auto second = orchestrator.async_delivery(
            second_block->get_hash(), {}, second_plan.make());
        first_plan.owner.reset();
        second_plan.owner.reset();

        Observation first_observation;
        Observation second_observation;
        first_observation.watch(first);
        second_observation.watch(second);
        std::size_t protocol_continuations = 0;
        auto first_progress = first.then(
            [&protocol_continuations](const block_t &)
            {
                ++protocol_continuations;
            });
        auto second_progress = second.then(
            [&protocol_continuations](const block_t &)
            {
                ++protocol_continuations;
            });

        orchestrator.cancel(shutdown_error);
        CHECK(orchestrator.is_cancelled());
        CHECK(orchestrator.size() == 0);
        CHECK(first_observation.rejected == 1);
        CHECK(second_observation.rejected == 1);
        CHECK(protocol_continuations == 0);
        CHECK(first_owner.expired());
        CHECK(second_owner.expired());

        DeferredPlan after_cancel;
        auto rejected = orchestrator.async_delivery(
            digest("after-cancel"), {}, after_cancel.make());
        Observation rejected_observation;
        rejected_observation.watch(rejected);
        CHECK(rejected_observation.rejected == 1);
        CHECK(after_cancel.fetch_starts == 0);

        first_plan.fetched.resolve(first_block);
        second_plan.fetched.resolve(second_block);
        CHECK(first_plan.verify_starts == 0);
        CHECK(second_plan.verify_starts == 0);
        CHECK(protocol_continuations == 0);
        (void)first_progress;
        (void)second_progress;
    }

    SECTION("destructor performs the same cancellation")
    {
        DeferredPlan plan;
        std::weak_ptr<int> owner = plan.owner;
        Observation observation;
        std::size_t protocol_continuations = 0;
        promise_t delivery;
        promise_t continuation;
        {
            auto orchestrator =
                std::make_unique<BlockDeliveryOrchestrator>();
            delivery = orchestrator->async_delivery(
                first_block->get_hash(), {}, plan.make());
            plan.owner.reset();
            observation.watch(delivery);
            continuation = delivery.then(
                [&protocol_continuations](const block_t &)
                {
                    ++protocol_continuations;
                });
            REQUIRE(orchestrator->size() == 1);
        }

        CHECK(observation.fulfilled == 0);
        CHECK(observation.rejected == 1);
        CHECK(protocol_continuations == 0);
        CHECK(owner.expired());
        plan.fetched.resolve(first_block);
        CHECK(plan.verify_starts == 0);
        CHECK(protocol_continuations == 0);
        (void)continuation;
    }
}

#endif
