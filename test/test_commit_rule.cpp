#include "catch.hpp"
#include "support/commit_rule_fixture.h"

using hotstuff::block_t;
using hotstuff::test::CommitRuleCore;
using hotstuff::test::add_direct_chain;

TEST_CASE("a direct certified three-chain commits its oldest block",
          "[s03][commit-rule][control]")
{
    CommitRuleCore core;
    const auto chain = add_direct_chain(core, core.get_genesis(), 4);

    REQUIRE_NOTHROW(core.apply_update(chain[3]));

    REQUIRE(core.committed().size() == 1);
    CHECK(core.committed()[0].height == chain[0]->get_height());
    CHECK(core.committed()[0].hash == chain[0]->get_hash());
    CHECK(chain[0]->get_decision() == 1);
}

TEST_CASE("certified height gaps commit on one pipelined ancestry branch",
          "[s03][commit-rule][pipeline][control]")
{
    CommitRuleCore core;
    const block_t genesis = core.get_genesis();

    const block_t block1 = core.add_block(genesis, genesis);
    const block_t block2 = core.add_block(block1, genesis);
    const block_t block3 = core.add_block(block2, block1);
    const block_t block4 = core.add_block(block3, block1);
    const block_t block5 = core.add_block(block4, block3);
    const block_t block6 = core.add_block(block5, block5);

    REQUIRE(block1->get_height() < block3->get_height());
    REQUIRE(block3->get_height() < block5->get_height());
    REQUIRE_NOTHROW(core.apply_update(block6));

    REQUIRE(core.committed().size() == 1);
    CHECK(core.committed()[0].hash == block1->get_hash());
    CHECK(block1->get_decision() == 1);
}

TEST_CASE("sibling certified blocks at one height cannot authorize a commit",
          "[s03][rem-s03-01][commit-rule][siblings][safety]")
{
    CommitRuleCore core;
    const block_t genesis = core.get_genesis();
    const block_t candidate = core.add_block(genesis, genesis);
    const block_t sibling = core.add_block(genesis, candidate);
    const block_t certified2 = core.add_block(candidate, sibling);
    const block_t new_block = core.add_block(certified2, certified2);
    REQUIRE(candidate->get_height() == sibling->get_height());
    REQUIRE(candidate->get_hash() != sibling->get_hash());
    REQUIRE_NOTHROW(core.apply_update(new_block));

    CHECK(core.committed().empty());
    CHECK(core.get_hqc() == certified2);
    CHECK(candidate->get_decision() == 0);
    CHECK(sibling->get_decision() == 0);
}

TEST_CASE("a QC reference off the certified block ancestry fails closed",
          "[s03][rem-s03-01][commit-rule][qc-ancestry][safety]")
{
    CommitRuleCore core;
    const block_t genesis = core.get_genesis();

    const block_t candidate = core.add_block(genesis, genesis);
    const block_t branch_a2 = core.add_block(candidate, candidate);
    const block_t branch_b1 = core.add_block(genesis, genesis);
    const block_t branch_b2 = core.add_block(branch_b1, candidate);
    const block_t certified2 = core.add_block(branch_a2, branch_b2);
    const block_t new_block = core.add_block(certified2, certified2);
    REQUIRE(candidate->get_height() < branch_b2->get_height());
    REQUIRE(branch_b2->get_height() < certified2->get_height());
    REQUIRE_NOTHROW(core.apply_update(new_block));

    CHECK(core.committed().empty());
    CHECK(core.get_hqc() == certified2);
    CHECK(candidate->get_decision() == 0);
}

TEST_CASE("null and nonadvancing certified ancestry fail closed",
          "[s03][commit-rule][malformed][safety]")
{
    SECTION("a null block has no commit side effects")
    {
        CommitRuleCore core;
        const block_t block;

        REQUIRE_NOTHROW(core.apply_update(block));
        CHECK(core.committed().empty());
    }

    SECTION("a missing QC reference has no commit side effects")
    {
        CommitRuleCore core;
        const block_t block = new hotstuff::Block();

        REQUIRE_NOTHROW(core.apply_update(block));
        CHECK(core.committed().empty());
        CHECK(block->get_decision() == 0);
    }

    SECTION("certified heights must advance strictly")
    {
        CommitRuleCore core;
        const block_t genesis = core.get_genesis();
        const block_t high1 = core.add_block(genesis, genesis);
        const block_t high2 = core.add_block(high1, high1);
        const block_t low1 = core.add_block(genesis, high2);
        const block_t low2 = core.add_block(low1, low1);
        const block_t new_block = core.add_block(low2, low2);

        REQUIRE(high2->get_height() >= low1->get_height());
        REQUIRE_NOTHROW(core.apply_update(new_block));

        CHECK(core.committed().empty());
        CHECK(high2->get_decision() == 0);
    }
}
