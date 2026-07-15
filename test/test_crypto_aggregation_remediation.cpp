#include <memory>
#include <stdexcept>
#include <type_traits>

#include "catch.hpp"
#include "support/bls_fixtures.h"

using hotstuff::QuorumCert;
using hotstuff::QuorumCertAggBLS;
using hotstuff::quorum_cert_bt;
using hotstuff::test::BlsTestCore;
using hotstuff::test::add_valid_signers;
using hotstuff::test::make_digest;
using hotstuff::test::make_test_proposal_key;
using hotstuff::test::serialized_hex;

namespace
{

template<typename Certificate, typename = void>
struct has_public_unchecked_merge : std::false_type
{};

template<typename Certificate>
struct has_public_unchecked_merge<
    Certificate,
    std::void_t<decltype(std::declval<Certificate &>().merge_quorum(
        std::declval<const QuorumCert &>()))>> : std::true_type
{};

struct MergeAttempt
{
    bool public_entry_point;
    bool rejected;
};

template<typename Certificate>
MergeAttempt try_public_unchecked_merge(Certificate &accumulator,
                                        const QuorumCert &incoming)
{
    if constexpr (has_public_unchecked_merge<Certificate>::value)
    {
        try
        {
            accumulator.merge_quorum(incoming);
            return {true, false};
        }
        catch (const std::invalid_argument &)
        {
            return {true, true};
        }
    }
    else
    {
        // Removing or hiding the unchecked mutation entry point is a valid
        // remediation. Keeping it public is valid only when it rejects the
        // malformed certificate atomically.
        return {false, true};
    }
}

quorum_cert_bt make_well_shaped_invalid_certificate(
    BlsTestCore &core,
    const hotstuff::ProposalKey &key,
    hotstuff::ReplicaID claimed_signer,
    hotstuff::ReplicaID cryptographic_signer)
{
    salticidae::Bits claimed_signers(core.get_config().nreplicas);
    claimed_signers.clear();
    claimed_signers.set(claimed_signer);

    auto wrong_part = core.make_part(cryptographic_signer, key);
    const auto &wrong_signature =
        dynamic_cast<const hotstuff::SigSecBLSAgg &>(*wrong_part);

    hotstuff::DataStream wire;
    hotstuff::serialize_proposal_key(wire, key);
    wire << claimed_signers << true;
    wrong_signature.SigSecBLSAgg::serialize(wire);

    quorum_cert_bt malformed(
        new QuorumCertAggBLS(core.get_config(), key));
    malformed->unserialize(wire);
    return malformed;
}

struct VerificationCounts
{
    std::size_t synchronous = 0;
    std::size_t worker = 0;
};

class CountingQuorumCert final : public QuorumCertAggBLS
{
public:
    CountingQuorumCert(const hotstuff::ReplicaConfig &config,
                       const hotstuff::ProposalKey &key,
                       std::shared_ptr<VerificationCounts> counts)
        : QuorumCertAggBLS(config, key), counts_(std::move(counts))
    {}

    CountingQuorumCert(const CountingQuorumCert &) = default;

    bool verify(const hotstuff::ReplicaConfig &config) override
    {
        ++counts_->synchronous;
        return QuorumCertAggBLS::verify(config);
    }

    hotstuff::promise_t verify(const hotstuff::ReplicaConfig &config,
                               hotstuff::VeriPool &) override
    {
        ++counts_->worker;
        const bool valid = QuorumCertAggBLS::verify(config);
        return hotstuff::promise_t(
            [valid](hotstuff::promise_t &promise) { promise.resolve(valid); });
    }

    CountingQuorumCert *clone() override
    {
        return new CountingQuorumCert(*this);
    }

private:
    std::shared_ptr<VerificationCounts> counts_;
};

class VerifiedAccumulator final : public QuorumCertAggBLS
{
public:
    using QuorumCertAggBLS::QuorumCertAggBLS;

    void merge_after_worker_verification(const QuorumCert &incoming)
    {
        merge_verified_quorum(incoming);
    }
};

} // namespace

TEST_CASE("unchecked public merge rejects a cryptographically invalid aggregate atomically",
          "[rem-s02-01][aggregate][public-api]")
{
    BlsTestCore core(7);
    const auto block_hash = make_digest(0x61);
    const auto key = make_test_proposal_key(block_hash);
    QuorumCertAggBLS accumulator(core.get_config(), key);
    add_valid_signers(accumulator, core, {0}, key);
    accumulator.compute();
    REQUIRE(accumulator.verify(core.get_config()));

    auto malformed = make_well_shaped_invalid_certificate(
        core, key, 2, 3);
    REQUIRE_FALSE(malformed->verify(core.get_config()));

    const auto before_count = accumulator.get_sigs_n();
    const auto before_signers = accumulator.get_signers();
    const auto before_bytes = serialized_hex(accumulator);
    const auto before_verification = accumulator.verify(core.get_config());

    const auto attempt = try_public_unchecked_merge(
        accumulator, static_cast<const QuorumCert &>(*malformed));

    INFO("The unchecked one-argument merge must be non-public or reject before mutation");
    CHECK(attempt.rejected);

    // A buggy merge leaves pending aggregate parts. Computing them makes the
    // resulting mutation observable through the same wire representation used
    // by relay messages.
    accumulator.compute();
    CHECK(accumulator.get_sigs_n() == before_count);
    CHECK(accumulator.get_signers() == before_signers);
    CHECK(serialized_hex(accumulator) == before_bytes);
    CHECK(accumulator.verify(core.get_config()) == before_verification);
}

TEST_CASE("checked public merge rejects the same malformed wire certificate",
          "[rem-s02-01][aggregate][checked-control]")
{
    BlsTestCore core(7);
    const auto block_hash = make_digest(0x62);
    const auto key = make_test_proposal_key(block_hash);
    QuorumCertAggBLS accumulator(core.get_config(), key);
    add_valid_signers(accumulator, core, {0}, key);
    accumulator.compute();

    auto malformed = make_well_shaped_invalid_certificate(
        core, key, 2, 3);
    const auto before = serialized_hex(accumulator);

    REQUIRE_THROWS_AS(
        accumulator.merge_quorum(core.get_config(), *malformed),
        std::invalid_argument);
    REQUIRE(accumulator.get_sigs_n() == 1);
    REQUIRE(serialized_hex(accumulator) == before);
    REQUIRE(accumulator.verify(core.get_config()));
}

TEST_CASE("accepted relay contribution is cryptographically verified once",
          "[rem-s02-01][relay][verification-count]")
{
    BlsTestCore core(7);
    const auto block_hash = make_digest(0x63);
    const auto key = make_test_proposal_key(block_hash);
    auto counts = std::make_shared<VerificationCounts>();

    CountingQuorumCert incoming(core.get_config(), key, counts);
    add_valid_signers(incoming, core, {1, 3, 4}, key);
    incoming.compute();
    REQUIRE(incoming.QuorumCertAggBLS::verify(core.get_config()));

    hotstuff::VoteRelay relay(key, incoming.clone(), &core);
    auto *counting =
        dynamic_cast<CountingQuorumCert *>(relay.cert.get());
    REQUIRE(counting != nullptr);

    VerifiedAccumulator accumulator(core.get_config(), key);
    add_valid_signers(accumulator, core, {0}, key);
    accumulator.compute();

    counts->synchronous = 0;
    counts->worker = 0;

    // The handler performs cheap envelope admission, one worker verification,
    // and event-loop mutation through the non-public verified merge seam.
    REQUIRE(hotstuff::validate_relay_envelope(core.get_config(), relay));

    hotstuff::EventContext event_context;
    hotstuff::VeriPool verification_pool(event_context, 0);
    counting->verify(core.get_config(), verification_pool);
    accumulator.merge_after_worker_verification(*counting);

    CHECK(counts->synchronous + counts->worker == 1);
    CHECK(counts->synchronous == 0);
    CHECK(counts->worker == 1);

    accumulator.compute();
    REQUIRE(accumulator.get_sigs_n() == 4);
    REQUIRE(accumulator.verify(core.get_config()));
}
