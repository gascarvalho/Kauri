#include "catch.hpp"
#include "support/commit_rule_fixture.h"

using hotstuff::block_t;
using hotstuff::test::CommitRuleCore;
using hotstuff::test::add_direct_chain;

TEST_CASE("a one-chain prefix advances HQC without committing",
          "[rem-s03-01][commit-progression][hqc]")
{
    CommitRuleCore core;
    const block_t genesis = core.get_genesis();
    const block_t block1 = core.add_block(genesis, genesis);
    const block_t block2 = core.add_block(block1, block1);

    REQUIRE(core.get_hqc() == genesis);
    REQUIRE(core.lock_is(genesis));
    REQUIRE_NOTHROW(core.apply_update(block2));

    CHECK(core.get_hqc() == block1);
    CHECK(core.lock_is(genesis));
    CHECK(core.committed().empty());
    CHECK(block1->get_decision() == 0);
}

TEST_CASE("a two-chain prefix advances HQC and lock without committing",
          "[rem-s03-01][commit-progression][lock][control]")
{
    CommitRuleCore core;
    const auto chain = add_direct_chain(core, core.get_genesis(), 3);

    REQUIRE_NOTHROW(core.apply_update(chain[2]));

    CHECK(core.get_hqc() == chain[1]);
    CHECK(core.lock_is(chain[0]));
    CHECK(core.committed().empty());
    CHECK(chain[0]->get_decision() == 0);
}

TEST_CASE("an invalid outer QC link advances neither HQC nor lock",
          "[rem-s03-01][commit-progression][outer-link][control]")
{
    SECTION("an undelivered proposal is inert")
    {
        CommitRuleCore core;
        const block_t genesis = core.get_genesis();
        const block_t block1 = core.add_block(genesis, genesis);
        const block_t undelivered =
            core.make_undelivered_block(block1, block1);

        REQUIRE_NOTHROW(core.apply_update(undelivered));

        CHECK(core.get_hqc() == genesis);
        CHECK(core.lock_is(genesis));
        CHECK(core.committed().empty());
    }

    SECTION("an undelivered certified block is inert")
    {
        CommitRuleCore core;
        const block_t genesis = core.get_genesis();
        const block_t undelivered =
            core.make_undelivered_block(genesis, genesis);
        core.storage->add_blk(undelivered);
        const block_t new_block = core.add_block(genesis, undelivered);

        REQUIRE(new_block->is_delivered());
        REQUIRE_FALSE(undelivered->is_delivered());
        REQUIRE_NOTHROW(core.apply_update(new_block));

        CHECK(core.get_hqc() == genesis);
        CHECK(core.lock_is(genesis));
        CHECK(core.committed().empty());
    }

    SECTION("a mismatched QC object hash is inert")
    {
        CommitRuleCore core;
        const block_t genesis = core.get_genesis();
        const block_t block1 = core.add_block(genesis, genesis);
        const block_t block2 = core.add_block(block1, block1);
        core.corrupt_qc_object_hash(block2, genesis->get_hash());

        REQUIRE(block2->get_qc()->get_obj_hash() !=
                block2->get_qc_ref()->get_hash());
        REQUIRE_NOTHROW(core.apply_update(block2));

        CHECK(core.get_hqc() == genesis);
        CHECK(core.lock_is(genesis));
        CHECK(core.committed().empty());
    }

    SECTION("an off-lineage QC reference is inert")
    {
        CommitRuleCore core;
        const block_t genesis = core.get_genesis();
        const block_t branch_a = core.add_block(genesis, genesis);
        const block_t branch_b = core.add_block(genesis, genesis);
        const block_t new_block = core.add_block(branch_a, branch_b);

        REQUIRE(branch_a->get_height() == branch_b->get_height());
        REQUIRE_NOTHROW(core.apply_update(new_block));

        CHECK(core.get_hqc() == genesis);
        CHECK(core.lock_is(genesis));
        CHECK(core.committed().empty());
    }
}

TEST_CASE("a valid prefix preserves progression when commit proof is invalid",
          "[rem-s03-01][commit-progression][staged][safety]")
{
    CommitRuleCore core;
    const block_t genesis = core.get_genesis();
    const block_t wrong_candidate = core.add_block(genesis, genesis);
    const block_t parent = core.add_block(genesis, genesis);
    const block_t lock_candidate =
        core.add_block(parent, wrong_candidate);
    const block_t hqc_candidate =
        core.add_block(lock_candidate, lock_candidate);
    const block_t new_block =
        core.add_block(hqc_candidate, hqc_candidate);

    REQUIRE_NOTHROW(core.apply_update(new_block));

    CHECK(core.get_hqc() == hqc_candidate);
    CHECK(core.lock_is(lock_candidate));
    CHECK(core.committed().empty());
    CHECK(wrong_candidate->get_decision() == 0);
    CHECK(parent->get_decision() == 0);
}

TEST_CASE("an invalid second link advances HQC but not lock or commit",
          "[rem-s03-01][commit-progression][staged][safety]")
{
    CommitRuleCore core;
    const block_t genesis = core.get_genesis();
    const block_t branch_a = core.add_block(genesis, genesis);
    const block_t branch_b = core.add_block(genesis, genesis);
    const block_t hqc_candidate = core.add_block(branch_a, branch_b);
    const block_t new_block =
        core.add_block(hqc_candidate, hqc_candidate);

    REQUIRE_NOTHROW(core.apply_update(new_block));

    CHECK(core.get_hqc() == hqc_candidate);
    CHECK(core.lock_is(genesis));
    CHECK(core.committed().empty());
    CHECK(branch_a->get_decision() == 0);
    CHECK(branch_b->get_decision() == 0);
}

TEST_CASE("lower competing prefixes cannot roll back HQC or lock",
          "[rem-s03-01][commit-progression][monotonic][control]")
{
    CommitRuleCore core;
    const block_t genesis = core.get_genesis();
    const auto higher = add_direct_chain(core, genesis, 3);
    REQUIRE_NOTHROW(core.apply_update(higher[2]));
    REQUIRE(core.get_hqc() == higher[1]);
    REQUIRE(core.lock_is(higher[0]));

    const block_t competing1 = core.add_block(genesis, genesis);
    const block_t competing2 =
        core.add_block(competing1, competing1);
    REQUIRE_NOTHROW(core.apply_update(competing2));

    CHECK(core.get_hqc() == higher[1]);
    CHECK(core.lock_is(higher[0]));
    CHECK(core.committed().empty());
}
