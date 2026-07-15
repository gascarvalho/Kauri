#include <map>
#include <set>

#include "catch.hpp"
#include "support/commit_rule_fixture.h"

using hotstuff::block_t;
using hotstuff::test::CommitRuleCore;
using hotstuff::test::add_direct_chain;

TEST_CASE("a commit candidate must descend from the current execution head",
          "[s03][consensus-conflict][execution-head][safety]")
{
    CommitRuleCore core;
    const block_t genesis = core.get_genesis();
    const auto committed_branch = add_direct_chain(core, genesis, 4);
    REQUIRE_NOTHROW(core.apply_update(committed_branch[3]));
    REQUIRE(core.committed().size() == 1);

    const auto conflicting_branch = add_direct_chain(core, genesis, 4);
    REQUIRE(conflicting_branch[0]->get_height() ==
            committed_branch[0]->get_height());
    REQUIRE(conflicting_branch[0]->get_hash() !=
            committed_branch[0]->get_hash());
    const block_t hqc_before = core.get_hqc();

    REQUIRE_NOTHROW(core.apply_update(conflicting_branch[3]));

    CHECK(core.committed().size() == 1);
    CHECK(core.get_hqc() == hqc_before);
    CHECK(conflicting_branch[0]->get_decision() == 0);
}

TEST_CASE("one committed height maps to exactly one block hash",
          "[s03][consensus-conflict][unique-height][safety]")
{
    CommitRuleCore core;
    const block_t genesis = core.get_genesis();
    const auto branch_a = add_direct_chain(core, genesis, 4);
    const auto branch_b = add_direct_chain(core, genesis, 4);

    REQUIRE_NOTHROW(core.apply_update(branch_a[3]));
    REQUIRE_NOTHROW(core.apply_update(branch_b[3]));

    std::map<std::uint32_t, std::set<hotstuff::uint256_t>> hashes_by_height;
    for (const auto &commit : core.committed())
        hashes_by_height[commit.height].insert(commit.hash);

    for (const auto &entry : hashes_by_height)
    {
        INFO("committed height " << entry.first);
        CHECK(entry.second.size() == 1);
    }
    CHECK_FALSE(core.committed_hash(branch_b[0]->get_hash()));
    CHECK(branch_b[0]->get_decision() == 0);
}
