#include <cstddef>
#include <stdexcept>
#include <string>

#include "catch.hpp"
#include "hotstuff/promise.hpp"

using promise::promise_t;

TEST_CASE("stack-free settlement survives release of the source handle",
          "[rem-a06-01][promise][stack-free][lifetime]")
{
    promise_t source;
    std::size_t first_observer_calls = 0;
    std::size_t later_observer_calls = 0;

    auto first_branch = source.then(
        [&source, &first_observer_calls]()
        {
            ++first_observer_calls;
            source = promise_t{};
        });
    auto later_branch = source.then(
        [&later_observer_calls]()
        {
            ++later_observer_calls;
        });

    source.resolve();

    CHECK(first_observer_calls == 1);
    CHECK(later_observer_calls == 1);
    (void)first_branch;
    (void)later_branch;
}

TEST_CASE("stack-free settled chaining remains a documented divergence",
          "[.][known-divergence][promise][stack-free]")
{
    promise_t settled;
    settled.resolve(7);

    std::size_t first_observer_calls = 0;
    std::size_t downstream_observer_calls = 0;
    auto chained = settled.then(
        [&first_observer_calls](int value)
        {
            CHECK(value == 7);
            ++first_observer_calls;
        });

    bool invalid_state = false;
    try
    {
        auto downstream = chained.then(
            [&downstream_observer_calls]()
            {
                ++downstream_observer_calls;
            });
        (void)downstream;
    }
    catch (const std::runtime_error &error)
    {
        invalid_state = std::string(error.what()) == "invalid promise state";
    }

    CHECK(first_observer_calls == 1);
    CHECK_FALSE(invalid_state);
    CHECK(downstream_observer_calls == 1);
}
