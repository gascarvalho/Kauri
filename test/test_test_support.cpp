#include <chrono>
#include <string>
#include <vector>

#include "catch.hpp"
#include "support/fake_clock.h"
#include "support/fixtures.h"
#include "support/transport_spy.h"

using namespace std::chrono_literals;

TEST_CASE("fake time executes due callbacks deterministically",
          "[infrastructure][clock]")
{
    hotstuff::test::FakeClock clock;
    hotstuff::test::DeterministicScheduler scheduler(clock);
    std::vector<std::string> order;

    scheduler.schedule("later", clock.now() + 2ms,
                       [&order] { order.push_back("later"); });
    scheduler.schedule("first-at-deadline", clock.now() + 1ms,
                       [&order] { order.push_back("first"); });
    scheduler.schedule("second-at-deadline", clock.now() + 1ms,
                       [&order] { order.push_back("second"); });
    scheduler.schedule("cancelled", clock.now() + 1ms,
                       [&order] { order.push_back("cancelled"); });

    REQUIRE(scheduler.cancel("cancelled"));
    scheduler.advance_by(1ms);
    REQUIRE(order == std::vector<std::string>{"first", "second"});
    REQUIRE(scheduler.pending() == 1);

    scheduler.advance_by(1ms);
    REQUIRE(order == std::vector<std::string>{"first", "second", "later"});
    REQUIRE(scheduler.pending() == 0);
}

TEST_CASE("scheduling the same key replaces the stale callback",
          "[infrastructure][clock]")
{
    hotstuff::test::FakeClock clock;
    hotstuff::test::DeterministicScheduler scheduler(clock);
    std::vector<int> calls;

    scheduler.schedule("view", clock.now() + 1ms,
                       [&calls] { calls.push_back(1); });
    scheduler.schedule("view", clock.now() + 2ms,
                       [&calls] { calls.push_back(2); });

    scheduler.advance_by(2ms);
    REQUIRE(calls == std::vector<int>{2});
}

TEST_CASE("fixtures produce deterministic membership keys tree and block",
          "[infrastructure][fixtures]")
{
    const auto membership = hotstuff::test::make_membership(7);
    const auto first_key = hotstuff::test::make_private_key(3);
    const auto second_key = hotstuff::test::make_private_key(3);
    const auto tree = hotstuff::test::make_tree(7, 9);
    const auto block = hotstuff::test::make_genesis_block();
    const auto config = hotstuff::test::make_replica_config(7);

    REQUIRE(membership.front() == 0);
    REQUIRE(membership.back() == 6);
    REQUIRE(hotstuff::get_hex(first_key) == hotstuff::get_hex(second_key));
    REQUIRE(tree.get_tid() == 9);
    REQUIRE(tree.get_tree_array() ==
            std::vector<std::uint32_t>{0, 1, 2, 3, 4, 5, 6});
    REQUIRE(block->is_delivered());
    REQUIRE(config.nreplicas == 7);
    REQUIRE(config.nmajority == 5);
}

TEST_CASE("transport spy records sends without opening sockets",
          "[infrastructure][transport]")
{
    hotstuff::test::TransportSpy<std::string> transport;

    transport.send(2, "proposal");
    transport.send(3, "vote");
    transport.send(2, "relay");

    REQUIRE(transport.messages().size() == 3);
    REQUIRE(transport.count_for(2) == 2);
    REQUIRE(transport.messages().front().message == "proposal");
    transport.clear();
    REQUIRE(transport.messages().empty());
}
