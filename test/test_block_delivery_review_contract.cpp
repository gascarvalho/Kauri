#include <algorithm>
#include <cstddef>
#include <exception>
#include <functional>
#include <limits>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "catch.hpp"

/*
 * REM-A06-01 review-remediation production seam
 * ------------------------------------------------
 * include/hotstuff/block_delivery_completion.h must expose an orchestration
 * seam used by HotStuffBase for every delivery terminal outcome:
 *
 *   struct BlockDeliveryTimingHooks {
 *       std::function<double()> elapsed_seconds;
 *       std::function<void(double)> record_success;
 *   };
 *
 *   class BlockDeliveryFinalizer {
 *   public:
 *       explicit BlockDeliveryFinalizer(BlockDeliveryLifecycle &);
 *       bool external_result(const uint256_t &, const block_t &, bool valid,
 *                            std::exception_ptr failure) noexcept;
 *       void async_success(BlockDeliveryTerminal, const block_t &) noexcept;
 *       void async_failure(BlockDeliveryTerminal,
 *                          std::exception_ptr) noexcept;
 *   };
 *
 * BlockDeliveryLifecycle::request additionally accepts optional
 * BlockDeliveryTimingHooks. The first successful settlement records exactly
 * one elapsed sample before notifying any observer. Duplicate callers share
 * the original timing hooks. external_result(false) rejects and erases the
 * current hash generation immediately. Async methods remain generation-bound.
 */
#if __has_include("hotstuff/block_delivery_completion.h")
#define KAURI_HAS_BLOCK_DELIVERY_COMPLETION 1
#include "hotstuff/block_delivery_completion.h"
#include "hotstuff/hotstuff.h"
#else
#define KAURI_HAS_BLOCK_DELIVERY_COMPLETION 0
#endif

#if !KAURI_HAS_BLOCK_DELIVERY_COMPLETION

TEST_CASE("block delivery production completion seam is available",
          "[rem-a06-01][delivery-completion][contract][red]")
{
    INFO("Missing include/hotstuff/block_delivery_completion.h. "
         "HotStuffBase needs one production finalizer for external failure, "
         "external success, async success, and timing-before-notification.");
    REQUIRE(KAURI_HAS_BLOCK_DELIVERY_COMPLETION == 1);
}

#else

namespace
{

using hotstuff::BlockDeliveryFinalizer;
using hotstuff::BlockDeliveryLifecycle;
using hotstuff::BlockDeliveryTerminal;
using hotstuff::BlockDeliveryTimingHooks;
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

struct AttemptCapture
{
    std::size_t starts{0};
    std::optional<BlockDeliveryTerminal> terminal;

    StartBlockDeliveryAttempt start()
    {
        return [this](BlockDeliveryTerminal value)
        {
            ++starts;
            terminal = std::move(value);
        };
    }
};

struct Observation
{
    std::size_t fulfilled{0};
    std::size_t rejected{0};
    block_t block;
    std::vector<promise_t> branches;

    void watch(const promise_t &delivery,
               std::function<void()> before_fulfilled = {})
    {
        branches.push_back(delivery.then(
            [this, before_fulfilled](const block_t &value)
            {
                if (before_fulfilled)
                    before_fulfilled();
                ++fulfilled;
                block = value;
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
        return BlockDeliveryTimingHooks{
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

void require_single_sample(const TimingProbe &timing, double expected)
{
    REQUIRE(timing.samples.size() == 1);
    CHECK(timing.samples.front() == Approx(expected));
    CHECK(timing.total == Approx(expected));
    CHECK(timing.minimum == Approx(expected));
    CHECK(timing.maximum == Approx(expected));
}

} // namespace

TEST_CASE("external false delivery terminates shared production orchestration",
          "[rem-a06-01][delivery-completion][external][failure]")
{
    BlockDeliveryLifecycle lifecycle;
    BlockDeliveryFinalizer finalizer(lifecycle);
    const auto block_hash = digest("external-false");
    TimingProbe timing;
    AttemptCapture original_attempt;

    auto first = lifecycle.request(
        block_hash, original_attempt.start(), timing.hooks(0.25));
    std::size_t duplicate_starts = 0;
    auto second = lifecycle.request(
        block_hash,
        [&duplicate_starts](BlockDeliveryTerminal)
        {
            ++duplicate_starts;
        });
    REQUIRE(original_attempt.starts == 1);
    REQUIRE(duplicate_starts == 0);
    REQUIRE(original_attempt.terminal.has_value());
    const auto stale_terminal = *original_attempt.terminal;

    Observation first_observation;
    Observation second_observation;
    first_observation.watch(first);
    second_observation.watch(second);

    AttemptCapture retry_attempt;
    std::optional<promise_t> retry;
    Observation retry_observation;
    auto retry_branch = first.fail(
        [&]()
        {
            retry = lifecycle.request(
                block_hash, retry_attempt.start(), timing.hooks(0.50));
            retry_observation.watch(*retry);
        });

    REQUIRE(finalizer.external_result(
        block_hash,
        delivered_block(),
        false,
        std::make_exception_ptr(
            std::runtime_error("external delivery rejected"))));

    CHECK(first_observation.fulfilled == 0);
    CHECK(first_observation.rejected == 1);
    CHECK(second_observation.fulfilled == 0);
    CHECK(second_observation.rejected == 1);
    CHECK(timing.samples.empty());
    REQUIRE(retry.has_value());
    REQUIRE(retry_attempt.starts == 1);
    REQUIRE(retry_attempt.terminal.has_value());
    CHECK(lifecycle.size() == 1);

    // The original asynchronous prerequisites are deliberately never
    // completed. A late signal from that generation cannot affect the retry.
    finalizer.async_success(stale_terminal, delivered_block());
    finalizer.async_failure(
        stale_terminal,
        std::make_exception_ptr(std::runtime_error("late prerequisite")));
    CHECK(retry_observation.fulfilled == 0);
    CHECK(retry_observation.rejected == 0);
    CHECK(lifecycle.size() == 1);

    finalizer.async_success(*retry_attempt.terminal, delivered_block());
    CHECK(retry_observation.fulfilled == 1);
    CHECK(retry_observation.rejected == 0);
    CHECK(lifecycle.size() == 0);
    require_single_sample(timing, 0.50);
    (void)retry_branch;
}

TEST_CASE("production success records timing before observer notification",
          "[rem-a06-01][delivery-completion][metrics][ordering]")
{
    const auto block_hash = digest("success-timing");

    SECTION("external success")
    {
        BlockDeliveryLifecycle lifecycle;
        BlockDeliveryFinalizer finalizer(lifecycle);
        TimingProbe timing;
        AttemptCapture attempt;
        auto first = lifecycle.request(
            block_hash, attempt.start(), timing.hooks(0.125));
        auto second = lifecycle.request(
            block_hash,
            [](BlockDeliveryTerminal)
            {
                FAIL("duplicate request started another attempt");
            });

        Observation first_observation;
        Observation second_observation;
        first_observation.watch(first, [&timing]()
        {
            require_single_sample(timing, 0.125);
        });
        second_observation.watch(second, [&timing]()
        {
            require_single_sample(timing, 0.125);
        });

        const auto block = delivered_block();
        REQUIRE(finalizer.external_result(block_hash, block, true, nullptr));
        CHECK(first_observation.fulfilled == 1);
        CHECK(second_observation.fulfilled == 1);
        CHECK(first_observation.block == block);
        CHECK(second_observation.block == block);
        require_single_sample(timing, 0.125);
        CHECK(lifecycle.size() == 0);
    }

    SECTION("normal asynchronous success")
    {
        BlockDeliveryLifecycle lifecycle;
        BlockDeliveryFinalizer finalizer(lifecycle);
        TimingProbe timing;
        AttemptCapture attempt;
        auto first = lifecycle.request(
            block_hash, attempt.start(), timing.hooks(0.375));
        auto second = lifecycle.request(
            block_hash,
            [](BlockDeliveryTerminal)
            {
                FAIL("duplicate request started another attempt");
            });
        REQUIRE(attempt.terminal.has_value());

        Observation first_observation;
        Observation second_observation;
        first_observation.watch(first, [&timing]()
        {
            require_single_sample(timing, 0.375);
        });
        second_observation.watch(second, [&timing]()
        {
            require_single_sample(timing, 0.375);
        });

        const auto block = delivered_block();
        finalizer.async_success(*attempt.terminal, block);
        CHECK(first_observation.fulfilled == 1);
        CHECK(second_observation.fulfilled == 1);
        CHECK(first_observation.block == block);
        CHECK(second_observation.block == block);
        require_single_sample(timing, 0.375);
        CHECK(lifecycle.size() == 0);

        finalizer.async_success(*attempt.terminal, delivered_block());
        require_single_sample(timing, 0.375);
    }
}

#endif
