#include <string>

#include "catch.hpp"

/*
 * REM-A06-01 block-delivery terminal lifecycle contract
 * -----------------------------------------------------
 * The production header is deliberately optional in this red commit. Its
 * absence keeps the project buildable while making this dedicated target fail
 * one explicit contract assertion. Once the production seam exists, the full
 * state-machine matrix below compiles automatically.
 *
 * include/hotstuff/block_delivery.h must expose:
 *
 *   class BlockDeliveryTerminal {
 *   public:
 *       void succeed(const block_t &) const;
 *       void fail(std::exception_ptr) const;
 *   };
 *
 *   using StartBlockDeliveryAttempt =
 *       std::function<void(BlockDeliveryTerminal)>;
 *
 *   class BlockDeliveryLifecycle {
 *   public:
 *       BlockDeliveryLifecycle();
 *       ~BlockDeliveryLifecycle();
 *       promise_t request(const uint256_t &,
 *                         StartBlockDeliveryAttempt);
 *       bool succeed_pending(const uint256_t &,
 *                            const block_t &) noexcept;
 *       bool contains(const uint256_t &) const noexcept;
 *       std::size_t size() const noexcept;
 *   };
 *
 * request owns one shared promise per block hash. A duplicate request joins
 * that promise without starting another attempt. succeed/fail are idempotent
 * generation-bound terminal handles: the first terminal signal settles every
 * waiter and erases the context, while a late handle from an old attempt can
 * neither settle nor erase a fresh retry for the same hash. request catches a
 * synchronous StartBlockDeliveryAttempt exception and converts it to the same
 * terminal rejection path. The terminal handle must not strongly retain a
 * completed context.
 * A valid delivery completed through another path must settle and erase the
 * current generation immediately; its stale terminal cannot affect a retry.
 *
 * HotStuffBase::async_deliver_blk remains responsible for its existing fetch,
 * QC verification, QC-reference fetch, and parent-delivery ordering. It must
 * call fail for false verification or any rejected/exceptional prerequisite,
 * and succeed only after all prerequisites and on_deliver_blk succeed.
 */
#if defined(KAURI_REM_A06_01_DECLARATION_MOCK)
#define KAURI_HAS_BLOCK_DELIVERY_LIFECYCLE 1

#include <cstddef>
#include <exception>
#include <functional>

#include "hotstuff/hotstuff.h"

namespace hotstuff
{

class BlockDeliveryTerminal
{
public:
    void succeed(const block_t &block) const;
    void fail(std::exception_ptr reason) const;
};

using StartBlockDeliveryAttempt =
    std::function<void(BlockDeliveryTerminal)>;

class BlockDeliveryLifecycle
{
public:
    BlockDeliveryLifecycle();
    ~BlockDeliveryLifecycle();

    promise_t request(const uint256_t &block_hash,
                      StartBlockDeliveryAttempt start_attempt);
    bool succeed_pending(const uint256_t &block_hash,
                         const block_t &block) noexcept;
    bool contains(const uint256_t &block_hash) const noexcept;
    std::size_t size() const noexcept;
};

} // namespace hotstuff

#elif __has_include("hotstuff/block_delivery.h")
#define KAURI_HAS_BLOCK_DELIVERY_LIFECYCLE 1

#include <cstddef>
#include <exception>
#include <functional>
#include <memory>
#include <optional>
#include <stdexcept>
#include <utility>
#include <vector>

#include "hotstuff/block_delivery.h"
#include "hotstuff/hotstuff.h"

#else
#define KAURI_HAS_BLOCK_DELIVERY_LIFECYCLE 0
#endif

#if !KAURI_HAS_BLOCK_DELIVERY_LIFECYCLE

TEST_CASE("block delivery terminal lifecycle is available",
          "[rem-a06-01][block-delivery][contract][red]")
{
    INFO("Missing include/hotstuff/block_delivery.h. REM-A06-01 requires "
         "one generation-bound shared terminal lifecycle used by "
         "HotStuffBase::async_deliver_blk.");
    REQUIRE(KAURI_HAS_BLOCK_DELIVERY_LIFECYCLE == 1);
}

#else

namespace
{

using hotstuff::BlockDeliveryLifecycle;
using hotstuff::BlockDeliveryTerminal;
using hotstuff::DataStream;
using hotstuff::StartBlockDeliveryAttempt;
using hotstuff::block_t;
using hotstuff::promise_t;
using hotstuff::uint256_t;

uint256_t digest(const std::string &label)
{
    return DataStream(label).get_hash();
}

block_t delivered_block()
{
    return new hotstuff::Block(true, 1);
}

struct PromiseObservation
{
    std::size_t fulfilled{0};
    std::size_t rejected{0};
    block_t block;
    std::vector<promise_t> callbacks;

    void watch(const promise_t &promise)
    {
        callbacks.push_back(promise.then(
            [this](const block_t &value)
            {
                ++fulfilled;
                block = value;
            },
            [this]()
            {
                ++rejected;
            }));
    }

    void release()
    {
        callbacks.clear();
    }
};

struct ProtocolMutationProbe
{
    std::size_t continuations{0};
    std::size_t accumulator_mutations{0};
    std::size_t forwarded_votes{0};
    std::size_t forwarded_relays{0};
    std::size_t timer_mutations{0};
    std::size_t progress_observations{0};

    void continue_after_delivery(const block_t &)
    {
        ++continuations;
        ++accumulator_mutations;
        ++forwarded_votes;
        ++forwarded_relays;
        ++timer_mutations;
        ++progress_observations;
    }

    bool unchanged() const
    {
        return continuations == 0 && accumulator_mutations == 0 &&
               forwarded_votes == 0 && forwarded_relays == 0 &&
               timer_mutations == 0 && progress_observations == 0;
    }
};

promise_t attach_protocol_continuation(const promise_t &delivery,
                                       ProtocolMutationProbe &probe)
{
    return delivery.then(
        [&probe](const block_t &block)
        {
            probe.continue_after_delivery(block);
        });
}

struct AttemptCapture
{
    std::size_t starts{0};
    std::optional<BlockDeliveryTerminal> terminal;
    std::shared_ptr<int> owner{std::make_shared<int>(1)};

    StartBlockDeliveryAttempt start()
    {
        return [this, keep_alive = owner](BlockDeliveryTerminal value)
        {
            (void)keep_alive;
            ++starts;
            terminal = std::move(value);
        };
    }

    void release()
    {
        terminal.reset();
        owner.reset();
    }
};

void require_no_delivery_or_protocol_progress(
    const PromiseObservation &observation,
    const ProtocolMutationProbe &protocol)
{
    CHECK(observation.fulfilled == 0);
    CHECK(observation.block == nullptr);
    CHECK(protocol.unchanged());
}

} // namespace

TEST_CASE("false QC verification rejects every shared waiter and cleans state",
          "[rem-a06-01][block-delivery][verification][shared]")
{
    BlockDeliveryLifecycle lifecycle;
    const auto block_hash = digest("false-verification");
    AttemptCapture attempt;
    std::size_t duplicate_starts = 0;

    auto first = lifecycle.request(block_hash, attempt.start());
    auto second = lifecycle.request(
        block_hash,
        [&duplicate_starts](BlockDeliveryTerminal)
        {
            ++duplicate_starts;
        });

    REQUIRE(attempt.starts == 1);
    REQUIRE(duplicate_starts == 0);
    REQUIRE(attempt.terminal.has_value());
    REQUIRE(lifecycle.size() == 1);
    REQUIRE(lifecycle.contains(block_hash));

    PromiseObservation first_observation;
    PromiseObservation second_observation;
    ProtocolMutationProbe protocol;
    first_observation.watch(first);
    second_observation.watch(second);
    auto protocol_continuation =
        attach_protocol_continuation(first, protocol);

    const bool qc_verified = false;
    REQUIRE_FALSE(qc_verified);
    REQUIRE_NOTHROW(attempt.terminal->fail(std::make_exception_ptr(
        std::runtime_error("block QC verification returned false"))));

    CHECK(first_observation.rejected == 1);
    CHECK(second_observation.rejected == 1);
    require_no_delivery_or_protocol_progress(first_observation, protocol);
    require_no_delivery_or_protocol_progress(second_observation, protocol);
    CHECK(lifecycle.size() == 0);
    CHECK_FALSE(lifecycle.contains(block_hash));

    // Terminal signals are idempotent. Neither a duplicate failure nor a late
    // success may escape or resurrect protocol progress.
    REQUIRE_NOTHROW(attempt.terminal->fail(std::make_exception_ptr(
        std::runtime_error("duplicate terminal failure"))));
    REQUIRE_NOTHROW(attempt.terminal->succeed(delivered_block()));
    CHECK(first_observation.rejected == 1);
    CHECK(second_observation.rejected == 1);
    CHECK(protocol.unchanged());
    (void)protocol_continuation;
}

TEST_CASE("rejected and exceptional prerequisites share terminal cleanup",
          "[rem-a06-01][block-delivery][failure-matrix]")
{
    const auto block_hash = digest("failure-matrix");

    SECTION("a synchronous attempt exception is converted to rejection")
    {
        BlockDeliveryLifecycle lifecycle;
        PromiseObservation observation;
        ProtocolMutationProbe protocol;
        promise_t delivery;

        REQUIRE_NOTHROW(delivery = lifecycle.request(
            block_hash,
            [](BlockDeliveryTerminal)
            {
                throw std::runtime_error("synchronous prerequisite start");
            }));
        observation.watch(delivery);
        auto continuation = attach_protocol_continuation(delivery, protocol);

        CHECK(observation.rejected == 1);
        require_no_delivery_or_protocol_progress(observation, protocol);
        CHECK(lifecycle.size() == 0);
        CHECK_FALSE(lifecycle.contains(block_hash));
        (void)continuation;
    }

    SECTION("an asynchronous prerequisite rejection is terminal")
    {
        BlockDeliveryLifecycle lifecycle;
        AttemptCapture attempt;
        auto delivery = lifecycle.request(block_hash, attempt.start());
        PromiseObservation observation;
        ProtocolMutationProbe protocol;
        observation.watch(delivery);
        auto continuation = attach_protocol_continuation(delivery, protocol);

        REQUIRE(attempt.terminal.has_value());
        REQUIRE_NOTHROW(attempt.terminal->fail(std::make_exception_ptr(
            std::runtime_error("parent delivery rejected"))));

        CHECK(observation.rejected == 1);
        require_no_delivery_or_protocol_progress(observation, protocol);
        CHECK(lifecycle.size() == 0);
        CHECK_FALSE(lifecycle.contains(block_hash));
        (void)continuation;
    }

    SECTION("an asynchronous exception uses the same idempotent path")
    {
        BlockDeliveryLifecycle lifecycle;
        AttemptCapture attempt;
        auto delivery = lifecycle.request(block_hash, attempt.start());
        PromiseObservation observation;
        ProtocolMutationProbe protocol;
        observation.watch(delivery);
        auto continuation = attach_protocol_continuation(delivery, protocol);

        REQUIRE(attempt.terminal.has_value());
        REQUIRE_NOTHROW(attempt.terminal->fail(std::make_exception_ptr(
            std::logic_error("verification callback threw"))));
        REQUIRE_NOTHROW(attempt.terminal->fail(std::make_exception_ptr(
            std::logic_error("late callback threw"))));

        CHECK(observation.rejected == 1);
        require_no_delivery_or_protocol_progress(observation, protocol);
        CHECK(lifecycle.size() == 0);
        CHECK_FALSE(lifecycle.contains(block_hash));
        (void)continuation;
    }
}

TEST_CASE("failed contexts release ownership and retry with a new generation",
          "[rem-a06-01][block-delivery][cleanup][retry]")
{
    BlockDeliveryLifecycle lifecycle;
    const auto block_hash = digest("fresh-retry");
    AttemptCapture failed_attempt;
    std::weak_ptr<int> failed_owner = failed_attempt.owner;

    auto failed = lifecycle.request(block_hash, failed_attempt.start());
    PromiseObservation failed_observation;
    failed_observation.watch(failed);
    REQUIRE(failed_attempt.terminal.has_value());
    std::optional<BlockDeliveryTerminal> stale_terminal{
        *failed_attempt.terminal};

    auto callback_owner = std::make_shared<int>(2);
    std::weak_ptr<int> callback_owner_ref = callback_owner;
    auto retained_callback = failed.then(
        [callback_owner](const block_t &)
        {
            (void)callback_owner;
        },
        [callback_owner]()
        {
            (void)callback_owner;
        });

    AttemptCapture retry_attempt;
    std::optional<promise_t> retry;
    PromiseObservation retry_observation;
    auto reentrant_retry = failed.fail(
        [&]()
        {
            retry = lifecycle.request(block_hash, retry_attempt.start());
            retry_observation.watch(*retry);
        });

    failed_attempt.terminal->fail(std::make_exception_ptr(
        std::runtime_error("terminal verification failure")));
    REQUIRE(failed_observation.rejected == 1);
    REQUIRE(retry.has_value());
    REQUIRE(retry_attempt.starts == 1);
    REQUIRE(retry_attempt.terminal.has_value());
    REQUIRE(lifecycle.size() == 1);

    // A terminal handle is bound to one generation, not just the block hash.
    REQUIRE_NOTHROW(stale_terminal->succeed(delivered_block()));
    CHECK(retry_observation.fulfilled == 0);
    CHECK(retry_observation.rejected == 0);
    CHECK(lifecycle.size() == 1);

    const auto retry_block = delivered_block();
    retry_attempt.terminal->succeed(retry_block);
    CHECK(retry_observation.fulfilled == 1);
    CHECK(retry_observation.rejected == 0);
    CHECK(retry_observation.block == retry_block);
    CHECK(lifecycle.size() == 0);

    // Once callers and terminal handles are released, neither the lifecycle
    // nor settled promise callbacks retain attempt-owned state.
    failed_attempt.release();
    failed_observation.release();
    callback_owner.reset();
    failed = promise_t{};
    retained_callback = promise_t{};
    reentrant_retry = promise_t{};
    retry.reset();
    stale_terminal.reset();
    CHECK(failed_owner.expired());
    CHECK(callback_owner_ref.expired());
}

TEST_CASE("external delivery settles waiters immediately and isolates retry",
          "[rem-a06-01][block-delivery][external][retry]")
{
    BlockDeliveryLifecycle lifecycle;
    const auto external_block = delivered_block();
    const auto block_hash = external_block->get_hash();
    AttemptCapture original_attempt;
    std::size_t duplicate_starts = 0;

    auto first = lifecycle.request(block_hash, original_attempt.start());
    auto second = lifecycle.request(
        block_hash,
        [&duplicate_starts](BlockDeliveryTerminal)
        {
            ++duplicate_starts;
        });
    REQUIRE(original_attempt.starts == 1);
    REQUIRE(duplicate_starts == 0);
    REQUIRE(original_attempt.terminal.has_value());
    const BlockDeliveryTerminal stale_terminal = *original_attempt.terminal;

    PromiseObservation first_observation;
    PromiseObservation second_observation;
    first_observation.watch(first);
    second_observation.watch(second);

    AttemptCapture retry_attempt;
    std::optional<promise_t> retry;
    PromiseObservation retry_observation;
    bool erased_before_callback = false;
    auto reentrant_retry = first.then(
        [&](const block_t &)
        {
            erased_before_callback = !lifecycle.contains(block_hash);
            retry = lifecycle.request(block_hash, retry_attempt.start());
            retry_observation.watch(*retry);
        });

    REQUIRE(lifecycle.succeed_pending(block_hash, external_block));
    CHECK(erased_before_callback);
    CHECK(first_observation.fulfilled == 1);
    CHECK(first_observation.rejected == 0);
    CHECK(first_observation.block == external_block);
    CHECK(second_observation.fulfilled == 1);
    CHECK(second_observation.rejected == 0);
    CHECK(second_observation.block == external_block);

    REQUIRE(retry.has_value());
    REQUIRE(retry_attempt.starts == 1);
    REQUIRE(retry_attempt.terminal.has_value());
    REQUIRE(lifecycle.size() == 1);
    REQUIRE(lifecycle.contains(block_hash));

    REQUIRE_NOTHROW(stale_terminal.fail(std::make_exception_ptr(
        std::runtime_error("late prerequisite rejection"))));
    REQUIRE_NOTHROW(stale_terminal.succeed(delivered_block()));
    CHECK(retry_observation.fulfilled == 0);
    CHECK(retry_observation.rejected == 0);
    CHECK(lifecycle.size() == 1);

    const auto retry_block = delivered_block();
    retry_attempt.terminal->succeed(retry_block);
    CHECK(retry_observation.fulfilled == 1);
    CHECK(retry_observation.rejected == 0);
    CHECK(retry_observation.block == retry_block);
    CHECK(lifecycle.size() == 0);
    CHECK_FALSE(lifecycle.contains(block_hash));
    CHECK_FALSE(lifecycle.succeed_pending(block_hash, delivered_block()));
    (void)reentrant_retry;
}

TEST_CASE("throwing observers cannot block shared terminal cleanup",
          "[rem-a06-01][block-delivery][observers][cleanup]")
{
    const auto block_hash = digest("throwing-observers");

    SECTION("fulfilled observer throws before later observer")
    {
        BlockDeliveryLifecycle lifecycle;
        AttemptCapture attempt;
        auto delivery = lifecycle.request(block_hash, attempt.start());
        REQUIRE(attempt.terminal.has_value());

        std::size_t throwing_calls = 0;
        auto callback_owner = std::make_shared<int>(1);
        std::weak_ptr<int> callback_owner_ref = callback_owner;
        auto throwing_observer = delivery.then(
            [callback_owner, &throwing_calls](const block_t &)
            {
                (void)callback_owner;
                ++throwing_calls;
                throw std::runtime_error("fulfilled observer failed");
            });
        PromiseObservation later_observer;
        later_observer.watch(delivery);
        callback_owner.reset();

        const auto block = delivered_block();
        REQUIRE_NOTHROW(attempt.terminal->succeed(block));
        CHECK(throwing_calls == 1);
        CHECK(later_observer.fulfilled == 1);
        CHECK(later_observer.rejected == 0);
        CHECK(later_observer.block == block);
        CHECK(lifecycle.size() == 0);
        CHECK_FALSE(lifecycle.contains(block_hash));
        CHECK(callback_owner_ref.expired());
        (void)throwing_observer;
    }

    SECTION("rejected observer throws before later observer")
    {
        BlockDeliveryLifecycle lifecycle;
        AttemptCapture attempt;
        auto delivery = lifecycle.request(block_hash, attempt.start());
        REQUIRE(attempt.terminal.has_value());

        std::size_t throwing_calls = 0;
        auto callback_owner = std::make_shared<int>(2);
        std::weak_ptr<int> callback_owner_ref = callback_owner;
        auto throwing_observer = delivery.then(
            [](const block_t &)
            {},
            [callback_owner, &throwing_calls]()
            {
                (void)callback_owner;
                ++throwing_calls;
                throw std::runtime_error("rejected observer failed");
            });
        PromiseObservation later_observer;
        later_observer.watch(delivery);
        callback_owner.reset();

        REQUIRE_NOTHROW(attempt.terminal->fail(std::make_exception_ptr(
            std::runtime_error("delivery failed"))));
        CHECK(throwing_calls == 1);
        CHECK(later_observer.fulfilled == 0);
        CHECK(later_observer.rejected == 1);
        CHECK(lifecycle.size() == 0);
        CHECK_FALSE(lifecycle.contains(block_hash));
        CHECK(callback_owner_ref.expired());
        (void)throwing_observer;
    }
}

TEST_CASE("successful delivery remains ordered after every parent prerequisite",
          "[rem-a06-01][block-delivery][success][parents]")
{
    BlockDeliveryLifecycle lifecycle;
    const auto block_hash = digest("successful-parent-order");
    AttemptCapture attempt;
    auto delivery = lifecycle.request(block_hash, attempt.start());
    PromiseObservation observation;
    ProtocolMutationProbe protocol;
    observation.watch(delivery);
    auto continuation = attach_protocol_continuation(delivery, protocol);

    REQUIRE(attempt.terminal.has_value());
    bool verification_complete = false;
    bool qc_reference_fetched = false;
    bool first_parent_delivered = false;
    bool second_parent_delivered = false;

    auto maybe_succeed = [&]()
    {
        if (verification_complete && qc_reference_fetched &&
            first_parent_delivered && second_parent_delivered)
        {
            attempt.terminal->succeed(delivered_block());
        }
    };

    verification_complete = true;
    maybe_succeed();
    CHECK(observation.fulfilled == 0);
    CHECK(protocol.unchanged());

    qc_reference_fetched = true;
    maybe_succeed();
    CHECK(observation.fulfilled == 0);
    CHECK(protocol.unchanged());

    first_parent_delivered = true;
    maybe_succeed();
    CHECK(observation.fulfilled == 0);
    CHECK(protocol.unchanged());

    second_parent_delivered = true;
    maybe_succeed();
    CHECK(observation.fulfilled == 1);
    CHECK(observation.rejected == 0);
    CHECK(observation.block != nullptr);
    CHECK(protocol.continuations == 1);
    CHECK(protocol.accumulator_mutations == 1);
    CHECK(protocol.forwarded_votes == 1);
    CHECK(protocol.forwarded_relays == 1);
    CHECK(protocol.timer_mutations == 1);
    CHECK(protocol.progress_observations == 1);
    CHECK(lifecycle.size() == 0);
    CHECK_FALSE(lifecycle.contains(block_hash));
    (void)continuation;
}

#endif
