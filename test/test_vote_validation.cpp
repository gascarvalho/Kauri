#include "catch.hpp"
#include "support/bls_fixtures.h"

using hotstuff::QuorumCertAggBLS;
using hotstuff::test::BlsTestCore;
using hotstuff::test::add_part_without_prescribing_rejection_style;
using hotstuff::test::add_vote_without_prescribing_rejection_style;
using hotstuff::test::make_digest;
using hotstuff::test::make_test_proposal_key;

/*
 * HotStuffBase::vote_handler is private and requires an authenticated
 * PeerNetwork::conn_t backed by its dispatcher and event-loop state. The
 * Fabricating a Conn would be undefined behavior, so these tests exercise the
 * socket-free production admission seam followed by the exact
 * QuorumCertAggBLS::add_part accumulator called by both handler paths.
 */

TEST_CASE("direct vote admission binds the signer to the authenticated peer",
          "[s02][vote][identity]")
{
    BlsTestCore core(7);
    const auto block_hash = make_digest(0x20);
    const auto key = make_test_proposal_key(block_hash);
    const auto valid_vote = core.make_vote(1, 1, key);
    const auto forged_vote = core.make_vote(1, 2, key);
    const auto &replica_one_peer = core.get_config().get_peer_id(1);
    const auto &replica_two_peer = core.get_config().get_peer_id(2);

    SECTION("root admission")
    {
        REQUIRE(hotstuff::verify_authenticated_vote(
            core.get_config(), replica_one_peer, valid_vote));
        REQUIRE_FALSE(hotstuff::verify_authenticated_vote(
            core.get_config(), replica_two_peer, valid_vote));
        REQUIRE_FALSE(hotstuff::verify_authenticated_vote(
            core.get_config(), replica_one_peer, forged_vote));
    }

    SECTION("intermediate admission")
    {
        REQUIRE(hotstuff::verify_authenticated_vote(
            core.get_config(), replica_one_peer, valid_vote));
        REQUIRE_FALSE(hotstuff::verify_authenticated_vote(
            core.get_config(), replica_two_peer, valid_vote));
        REQUIRE_FALSE(hotstuff::verify_authenticated_vote(
            core.get_config(), replica_one_peer, forged_vote));
    }
}

TEST_CASE("non-root rejects an invalid direct child vote before mutation",
          "[s02][vote][non-root]")
{
    BlsTestCore core(7);
    const auto block_hash = make_digest(0x21);
    const auto key = make_test_proposal_key(block_hash);
    QuorumCertAggBLS accumulator(core.get_config(), key);

    auto local_part = core.make_part(0, key);
    accumulator.add_part(core.get_config(), 0, *local_part);
    REQUIRE(accumulator.get_sigs_n() == 1);

    auto invalid_child_vote = core.make_vote(1, 2, key);
    REQUIRE_FALSE(invalid_child_vote.verify());

    add_vote_without_prescribing_rejection_style(
        accumulator, core.get_config(), invalid_child_vote);

    REQUIRE(accumulator.get_sigs_n() == 1);
}

TEST_CASE("root rejects an invalid direct vote before mutation",
          "[s02][vote][root]")
{
    BlsTestCore core(7);
    const auto block_hash = make_digest(0x22);
    const auto key = make_test_proposal_key(block_hash);
    QuorumCertAggBLS accumulator(core.get_config(), key);
    auto invalid_vote = core.make_vote(1, 2, key);

    REQUIRE_FALSE(invalid_vote.verify());
    add_vote_without_prescribing_rejection_style(
        accumulator, core.get_config(), invalid_vote);

    REQUIRE(accumulator.get_sigs_n() == 0);
    REQUIRE_FALSE(accumulator.has_n(core.get_config().nmajority));
}

TEST_CASE("a valid direct vote is represented exactly once",
          "[s02][vote][duplicate]")
{
    BlsTestCore core(7);
    const auto block_hash = make_digest(0x23);
    const auto key = make_test_proposal_key(block_hash);
    QuorumCertAggBLS accumulator(core.get_config(), key);
    auto vote = core.make_vote(1, 1, key);

    REQUIRE(vote.verify());
    add_vote_without_prescribing_rejection_style(
        accumulator, core.get_config(), vote);
    add_vote_without_prescribing_rejection_style(
        accumulator, core.get_config(), vote);

    REQUIRE(accumulator.get_sigs_n() == 1);
    accumulator.compute();
    REQUIRE(accumulator.verify(core.get_config()));
}

TEST_CASE("duplicate cryptographic signer identities cannot form 2f plus 1",
          "[s02][vote][quorum]")
{
    BlsTestCore core(7);
    const auto block_hash = make_digest(0x24);
    const auto key = make_test_proposal_key(block_hash);
    QuorumCertAggBLS accumulator(core.get_config(), key);

    // Seven replicas imply f=2 and 2f+1=5. Only replica 0 signs; the same
    // signature identity is presented under five claimed voter identifiers.
    for (hotstuff::ReplicaID claimed_voter = 0;
         claimed_voter < core.get_config().nmajority;
         ++claimed_voter)
    {
        auto duplicate_signer_part = core.make_part(0, key);
        add_part_without_prescribing_rejection_style(
            accumulator,
            core.get_config(),
            claimed_voter,
            *duplicate_signer_part);
    }

    REQUIRE_FALSE(accumulator.has_n(core.get_config().nmajority));
}
