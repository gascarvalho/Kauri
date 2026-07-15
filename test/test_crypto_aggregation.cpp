#include <stdexcept>
#include <string>
#include <vector>

#include "catch.hpp"
#include "support/bls_fixtures.h"

using hotstuff::QuorumCertAggBLS;
using hotstuff::VoteRelay;
using hotstuff::quorum_cert_bt;
using hotstuff::test::BlsTestCore;
using hotstuff::test::add_valid_signers;
using hotstuff::test::make_digest;
using hotstuff::test::make_test_proposal_key;
using hotstuff::test::serialized_hex;

TEST_CASE("invalid relays fail admission at root and intermediate levels",
          "[s02][relay]")
{
    BlsTestCore core(7);
    const auto block_hash = make_digest(0x31);
    const auto key = make_test_proposal_key(block_hash);

    // Model a malicious wire certificate whose bitmap claims replica 1 while
    // the aggregate contains replica 2's signature. Public accumulation now
    // rejects this mismatch, so the malformed object must enter through the
    // same deserialization boundary as an untrusted relay.
    salticidae::Bits claimed_signers(core.get_config().nreplicas);
    claimed_signers.clear();
    claimed_signers.set(1);
    auto wrong_signer = core.make_part(2, key);
    const auto &wrong_bls_signature =
        dynamic_cast<const hotstuff::SigSecBLSAgg &>(*wrong_signer);
    hotstuff::DataStream malformed_wire;
    hotstuff::serialize_proposal_key(malformed_wire, key);
    malformed_wire << claimed_signers << true;
    wrong_bls_signature.SigSecBLSAgg::serialize(malformed_wire);

    auto invalid_certificate =
        quorum_cert_bt(new QuorumCertAggBLS(core.get_config(), key));
    invalid_certificate->unserialize(malformed_wire);
    REQUIRE_FALSE(invalid_certificate->verify(core.get_config()));

    VoteRelay invalid_relay(key, std::move(invalid_certificate), &core);

    // The current handler is not socket-free testable. Run the same public
    // certificate/hash admission boundary for every handler role so later
    // handler tests can reuse these vectors when a production seam exists.
    SECTION("root")
    {
        REQUIRE_FALSE(
            hotstuff::verify_relay_certificate(core.get_config(),
                                               invalid_relay));
    }
    SECTION("intermediate")
    {
        REQUIRE_FALSE(
            hotstuff::verify_relay_certificate(core.get_config(),
                                               invalid_relay));
    }
    SECTION("verified merge is atomic")
    {
        QuorumCertAggBLS accumulator(core.get_config(), key);
        add_valid_signers(accumulator, core, {0}, key);
        accumulator.compute();
        const auto before = serialized_hex(accumulator);

        REQUIRE_THROWS_AS(
            accumulator.merge_quorum(core.get_config(),
                                     *invalid_relay.cert),
            std::invalid_argument);
        REQUIRE(accumulator.get_sigs_n() == 1);
        REQUIRE(serialized_hex(accumulator) == before);
        REQUIRE(accumulator.verify(core.get_config()));
    }
}

TEST_CASE("aggregate block hash mismatch is rejected without mutation",
          "[s02][aggregate][hash]")
{
    BlsTestCore core(7);
    const auto expected_hash = make_digest(0x32);
    const auto other_hash = make_digest(0x33);
    const auto expected_key = make_test_proposal_key(expected_hash);
    const auto other_key = make_test_proposal_key(other_hash);
    QuorumCertAggBLS accumulator(core.get_config(), expected_key);
    QuorumCertAggBLS wrong_block(core.get_config(), other_key);

    add_valid_signers(accumulator, core, {0, 1}, expected_key);
    add_valid_signers(wrong_block, core, {2, 3}, other_key);
    accumulator.compute();
    wrong_block.compute();
    const auto before = serialized_hex(accumulator);

    REQUIRE_THROWS_AS(accumulator.merge_quorum(core.get_config(), wrong_block),
                      std::invalid_argument);
    REQUIRE(accumulator.get_sigs_n() == 2);
    REQUIRE(serialized_hex(accumulator) == before);
    REQUIRE(accumulator.verify(core.get_config()));
}

TEST_CASE("overlapping signer sets are rejected immutably",
          "[s02][aggregate][overlap]")
{
    BlsTestCore core(7);
    const auto block_hash = make_digest(0x34);
    const auto key = make_test_proposal_key(block_hash);
    QuorumCertAggBLS accumulator(core.get_config(), key);
    QuorumCertAggBLS overlapping(core.get_config(), key);

    add_valid_signers(accumulator, core, {0, 1}, key);
    add_valid_signers(overlapping, core, {1, 2}, key);
    accumulator.compute();
    overlapping.compute();
    REQUIRE(accumulator.verify(core.get_config()));
    REQUIRE(overlapping.verify(core.get_config()));

    const auto before_count = accumulator.get_sigs_n();
    const auto before_bytes = serialized_hex(accumulator);
    bool rejected = false;
    try
    {
        accumulator.merge_quorum(core.get_config(), overlapping);
    }
    catch (const std::invalid_argument &)
    {
        rejected = true;
    }

    // compute() makes the current buggy post-merge state serializable while it
    // is a no-op for the required immutable rejection state.
    accumulator.compute();
    CHECK(rejected);
    CHECK(accumulator.get_sigs_n() == before_count);
    CHECK(serialized_hex(accumulator) == before_bytes);
    CHECK(accumulator.verify(core.get_config()));
}

TEST_CASE("verified disjoint signer sets merge and verify",
          "[s02][aggregate][disjoint]")
{
    BlsTestCore core(7);
    const auto block_hash = make_digest(0x35);
    const auto key = make_test_proposal_key(block_hash);
    QuorumCertAggBLS accumulator(core.get_config(), key);
    QuorumCertAggBLS disjoint(core.get_config(), key);

    add_valid_signers(accumulator, core, {0, 1}, key);
    add_valid_signers(disjoint, core, {2, 3}, key);
    accumulator.compute();
    disjoint.compute();
    REQUIRE(accumulator.verify(core.get_config()));
    REQUIRE(disjoint.verify(core.get_config()));

    accumulator.merge_quorum(core.get_config(), disjoint);
    accumulator.compute();

    REQUIRE(accumulator.get_sigs_n() == 4);
    REQUIRE(accumulator.verify(core.get_config()));
}
