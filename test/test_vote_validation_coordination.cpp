#include <cstddef>
#include <functional>
#include <memory>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/hotstuff.h"
#include "support/bls_fixtures.h"

using hotstuff::QuorumCert;
using hotstuff::QuorumCertAggBLS;
using hotstuff::Vote;
using hotstuff::VoteRelay;
using hotstuff::block_t;
using hotstuff::promise_t;
using hotstuff::quorum_cert_bt;
using hotstuff::test::BlsTestCore;
using hotstuff::test::add_valid_signers;
using hotstuff::test::make_digest;
using hotstuff::test::make_test_proposal_key;
using hotstuff::test::serialized_hex;

namespace
{

using VerifiedDeliveryContinuation =
    std::function<void(const block_t &)>;
using AsyncPrerequisiteStart = std::function<promise_t()>;

template<typename Message, typename = void>
struct has_verified_delivery_coordinator : std::false_type
{};

template<typename Message>
struct has_verified_delivery_coordinator<
    Message,
    std::void_t<decltype(coordinate_verified_delivery(
        std::declval<const Message &>(),
        std::declval<AsyncPrerequisiteStart>(),
        std::declval<AsyncPrerequisiteStart>(),
        std::declval<VerifiedDeliveryContinuation>()))>>
    : std::bool_constant<std::is_convertible_v<
          decltype(coordinate_verified_delivery(
              std::declval<const Message &>(),
              std::declval<AsyncPrerequisiteStart>(),
              std::declval<AsyncPrerequisiteStart>(),
              std::declval<VerifiedDeliveryContinuation>())),
          promise_t>>
{};

template<typename Message>
promise_t coordinate(const Message &message,
                     AsyncPrerequisiteStart start_worker_verification,
                     AsyncPrerequisiteStart start_block_delivery,
                     VerifiedDeliveryContinuation continuation)
{
    if constexpr (has_verified_delivery_coordinator<Message>::value)
    {
        return coordinate_verified_delivery(
            message,
            std::move(start_worker_verification),
            std::move(start_block_delivery),
            std::move(continuation));
    }

    return promise_t([](promise_t &) {});
}

class VerifiedAccumulator final : public QuorumCertAggBLS
{
public:
    using QuorumCertAggBLS::QuorumCertAggBLS;

    void add_after_worker_verification(
        const hotstuff::ReplicaConfig &config,
        hotstuff::ReplicaID signer,
        const hotstuff::PartCert &part)
    {
        add_verified_part(config, signer, part);
    }

    void merge_after_worker_verification(const QuorumCert &incoming)
    {
        merge_verified_quorum(incoming);
    }
};

enum class ProgressPath
{
    NormalRelay,
    RelayPassThrough,
    NonRootDirectVote,
    RootDirectVote,
};

struct ProtocolSnapshot
{
    std::size_t signer_count = 0;
    std::string certificate_bytes;
    std::size_t forwarded_messages = 0;
    std::size_t pending_state_removals = 0;
    std::size_t hqc_updates = 0;
    std::size_t qc_finishes = 0;
    std::size_t timer_stops = 0;
    std::size_t progress_observations = 0;
    std::size_t continuations = 0;
};

bool operator==(const ProtocolSnapshot &left,
                const ProtocolSnapshot &right)
{
    return left.signer_count == right.signer_count &&
           left.certificate_bytes == right.certificate_bytes &&
           left.forwarded_messages == right.forwarded_messages &&
           left.pending_state_removals == right.pending_state_removals &&
           left.hqc_updates == right.hqc_updates &&
           left.qc_finishes == right.qc_finishes &&
           left.timer_stops == right.timer_stops &&
           left.progress_observations == right.progress_observations &&
           left.continuations == right.continuations;
}

bool operator!=(const ProtocolSnapshot &left,
                const ProtocolSnapshot &right)
{
    return !(left == right);
}

class ProtocolProgressProbe
{
public:
    ProtocolProgressProbe(BlsTestCore &core,
                          const hotstuff::ProposalKey &key)
        : core_(core), accumulator_(core.get_config(), key)
    {
        add_valid_signers(accumulator_, core_, {0}, key);
        accumulator_.compute();
    }

    ProtocolSnapshot snapshot() const
    {
        auto &accumulator =
            const_cast<VerifiedAccumulator &>(accumulator_);
        return {accumulator.get_sigs_n(),
                serialized_hex(accumulator),
                forwarded_messages_,
                pending_state_removals_,
                hqc_updates_,
                qc_finishes_,
                timer_stops_,
                progress_observations_,
                continuations_};
    }

    void apply(ProgressPath path, const Vote &vote)
    {
        ++continuations_;
        ++pending_state_removals_;
        ++progress_observations_;
        accumulator_.add_after_worker_verification(
            core_.get_config(), vote.voter, *vote.cert);
        accumulator_.compute();

        if (path == ProgressPath::NonRootDirectVote)
        {
            ++forwarded_messages_;
            ++timer_stops_;
        }
        else
        {
            ++hqc_updates_;
            ++qc_finishes_;
        }
    }

    void apply(ProgressPath path, const VoteRelay &relay)
    {
        ++continuations_;
        ++pending_state_removals_;
        ++progress_observations_;

        if (path == ProgressPath::NormalRelay)
        {
            accumulator_.merge_after_worker_verification(*relay.cert);
            accumulator_.compute();
            ++forwarded_messages_;
            ++timer_stops_;
            ++hqc_updates_;
            ++qc_finishes_;
        }
        else
        {
            ++forwarded_messages_;
        }
    }

private:
    BlsTestCore &core_;
    VerifiedAccumulator accumulator_;
    std::size_t forwarded_messages_ = 0;
    std::size_t pending_state_removals_ = 0;
    std::size_t hqc_updates_ = 0;
    std::size_t qc_finishes_ = 0;
    std::size_t timer_stops_ = 0;
    std::size_t progress_observations_ = 0;
    std::size_t continuations_ = 0;
};

struct DeferredPrerequisites
{
    promise_t verification;
    promise_t delivery;
    std::size_t worker_verifications = 0;
    std::size_t synchronous_verifications = 0;
    std::size_t delivery_requests = 0;

    AsyncPrerequisiteStart worker_verification_start()
    {
        return [this]()
        {
            ++worker_verifications;
            return verification;
        };
    }

    AsyncPrerequisiteStart block_delivery_start()
    {
        return [this]()
        {
            ++delivery_requests;
            return delivery;
        };
    }
};

quorum_cert_bt make_relay_certificate(
    BlsTestCore &core,
    const hotstuff::ProposalKey &key)
{
    quorum_cert_bt certificate(
        new QuorumCertAggBLS(core.get_config(), key));
    auto &bls = dynamic_cast<QuorumCertAggBLS &>(*certificate);
    add_valid_signers(bls, core, {1}, key);
    certificate->compute();
    return certificate;
}

VoteRelay make_relay(BlsTestCore &core,
                     const hotstuff::ProposalKey &key)
{
    return VoteRelay(key, make_relay_certificate(core, key), &core);
}

block_t make_delivered_block()
{
    return new hotstuff::Block(true, 1);
}

hotstuff::uint256_t delivered_block_hash()
{
    return make_delivered_block()->get_hash();
}

template<typename Message, typename Apply>
promise_t start_coordination(const Message &message,
                             DeferredPrerequisites &prerequisites,
                             Apply &&apply)
{
    return coordinate(
        message,
        prerequisites.worker_verification_start(),
        prerequisites.block_delivery_start(),
        VerifiedDeliveryContinuation(std::forward<Apply>(apply)));
}

void require_one_async_verification(
    const DeferredPrerequisites &prerequisites)
{
    REQUIRE(prerequisites.worker_verifications == 1);
    REQUIRE(prerequisites.synchronous_verifications == 0);
    REQUIRE(prerequisites.delivery_requests == 1);
}

} // namespace

TEST_CASE("vote and relay handlers expose a shared verified-delivery coordinator",
          "[rem-s02-02][coordination][contract]")
{
    INFO("Expected overload: coordinate_verified_delivery(const Vote&, start_worker_verification, start_block_delivery, continuation)");
    CHECK(has_verified_delivery_coordinator<Vote>::value);

    INFO("Expected overload: coordinate_verified_delivery(const VoteRelay&, start_worker_verification, start_block_delivery, continuation)");
    CHECK(has_verified_delivery_coordinator<VoteRelay>::value);
}

TEST_CASE("normal relay progress waits for both prerequisites in either order",
          "[rem-s02-02][coordination][relay]")
{
    REQUIRE(has_verified_delivery_coordinator<VoteRelay>::value);

    BlsTestCore core(7);
    const auto block_hash = delivered_block_hash();
    const auto key = make_test_proposal_key(block_hash);
    const auto relay = make_relay(core, key);
    DeferredPrerequisites prerequisites;
    ProtocolProgressProbe progress(core, key);
    const auto before = progress.snapshot();

    auto completion = start_coordination(
        relay,
        prerequisites,
        [&progress, &relay](const block_t &)
        {
            progress.apply(ProgressPath::NormalRelay, relay);
        });
    (void)completion;
    require_one_async_verification(prerequisites);
    REQUIRE(progress.snapshot() == before);

    SECTION("verification completes first")
    {
        prerequisites.verification.resolve(true);
        REQUIRE(progress.snapshot() == before);
        prerequisites.delivery.resolve(make_delivered_block());
    }

    SECTION("delivery completes first")
    {
        prerequisites.delivery.resolve(make_delivered_block());
        REQUIRE(progress.snapshot() == before);
        prerequisites.verification.resolve(true);
    }

    const auto after = progress.snapshot();
    REQUIRE(after != before);
    REQUIRE(after.continuations == 1);
    REQUIRE(after.signer_count == before.signer_count + 1);
    REQUIRE(after.forwarded_messages == 1);
    REQUIRE(after.pending_state_removals == 1);
    REQUIRE(after.hqc_updates == 1);
    REQUIRE(after.qc_finishes == 1);
    REQUIRE(after.timer_stops == 1);
    REQUIRE(after.progress_observations == 1);

    prerequisites.verification.resolve(true);
    prerequisites.delivery.resolve(make_delivered_block());
    REQUIRE(progress.snapshot() == after);
}

TEST_CASE("relay pass-through forwarding waits for both prerequisites",
          "[rem-s02-02][coordination][relay][pass-through]")
{
    REQUIRE(has_verified_delivery_coordinator<VoteRelay>::value);

    BlsTestCore core(7);
    const auto block_hash = delivered_block_hash();
    const auto key = make_test_proposal_key(block_hash);
    const auto relay = make_relay(core, key);
    DeferredPrerequisites prerequisites;
    ProtocolProgressProbe progress(core, key);
    const auto before = progress.snapshot();

    auto completion = start_coordination(
        relay,
        prerequisites,
        [&progress, &relay](const block_t &)
        {
            progress.apply(ProgressPath::RelayPassThrough, relay);
        });
    (void)completion;
    require_one_async_verification(prerequisites);

    SECTION("verification completes first")
    {
        prerequisites.verification.resolve(true);
        REQUIRE(progress.snapshot() == before);
        prerequisites.delivery.resolve(make_delivered_block());
    }

    SECTION("delivery completes first")
    {
        prerequisites.delivery.resolve(make_delivered_block());
        REQUIRE(progress.snapshot() == before);
        prerequisites.verification.resolve(true);
    }

    const auto after = progress.snapshot();
    REQUIRE(after.continuations == 1);
    REQUIRE(after.signer_count == before.signer_count);
    REQUIRE(after.forwarded_messages == 1);
    REQUIRE(after.pending_state_removals == 1);
    REQUIRE(after.progress_observations == 1);

    prerequisites.verification.resolve(true);
    prerequisites.delivery.resolve(make_delivered_block());
    REQUIRE(progress.snapshot() == after);
}

TEST_CASE("non-root direct-vote forwarding waits for both prerequisites",
          "[rem-s02-02][coordination][vote][non-root]")
{
    REQUIRE(has_verified_delivery_coordinator<Vote>::value);

    BlsTestCore core(7);
    const auto block_hash = delivered_block_hash();
    const auto key = make_test_proposal_key(block_hash);
    const auto vote = core.make_vote(1, 1, key);
    DeferredPrerequisites prerequisites;
    ProtocolProgressProbe progress(core, key);
    const auto before = progress.snapshot();

    auto completion = start_coordination(
        vote,
        prerequisites,
        [&progress, &vote](const block_t &)
        {
            progress.apply(ProgressPath::NonRootDirectVote, vote);
        });
    (void)completion;
    require_one_async_verification(prerequisites);

    SECTION("verification completes first")
    {
        prerequisites.verification.resolve(true);
        REQUIRE(progress.snapshot() == before);
        prerequisites.delivery.resolve(make_delivered_block());
    }

    SECTION("delivery completes first")
    {
        prerequisites.delivery.resolve(make_delivered_block());
        REQUIRE(progress.snapshot() == before);
        prerequisites.verification.resolve(true);
    }

    const auto after = progress.snapshot();
    REQUIRE(after.continuations == 1);
    REQUIRE(after.signer_count == before.signer_count + 1);
    REQUIRE(after.forwarded_messages == 1);
    REQUIRE(after.pending_state_removals == 1);
    REQUIRE(after.timer_stops == 1);
    REQUIRE(after.progress_observations == 1);

    prerequisites.verification.resolve(true);
    prerequisites.delivery.resolve(make_delivered_block());
    REQUIRE(progress.snapshot() == after);
}

TEST_CASE("root direct-vote control also uses verified delivery gating",
          "[rem-s02-02][coordination][vote][root]")
{
    REQUIRE(has_verified_delivery_coordinator<Vote>::value);

    BlsTestCore core(7);
    const auto block_hash = delivered_block_hash();
    const auto key = make_test_proposal_key(block_hash);
    const auto vote = core.make_vote(1, 1, key);
    DeferredPrerequisites prerequisites;
    ProtocolProgressProbe progress(core, key);
    const auto before = progress.snapshot();

    auto completion = start_coordination(
        vote,
        prerequisites,
        [&progress, &vote](const block_t &)
        {
            progress.apply(ProgressPath::RootDirectVote, vote);
        });
    (void)completion;
    require_one_async_verification(prerequisites);

    prerequisites.delivery.resolve(make_delivered_block());
    REQUIRE(progress.snapshot() == before);
    prerequisites.verification.resolve(true);

    const auto after = progress.snapshot();
    REQUIRE(after.continuations == 1);
    REQUIRE(after.signer_count == before.signer_count + 1);
    REQUIRE(after.hqc_updates == 1);
    REQUIRE(after.qc_finishes == 1);
    REQUIRE(after.pending_state_removals == 1);
    REQUIRE(after.progress_observations == 1);
}

TEST_CASE("delivery or verification failure suppresses every protocol side effect",
          "[rem-s02-02][coordination][failure]")
{
    BlsTestCore core(7);
    const auto block_hash = delivered_block_hash();
    const auto key = make_test_proposal_key(block_hash);

    SECTION("relay delivery failure after successful verification")
    {
        REQUIRE(has_verified_delivery_coordinator<VoteRelay>::value);
        const auto relay = make_relay(core, key);
        DeferredPrerequisites prerequisites;
        ProtocolProgressProbe progress(core, key);
        const auto before = progress.snapshot();
        auto completion = start_coordination(
            relay,
            prerequisites,
            [&progress, &relay](const block_t &)
            {
                progress.apply(ProgressPath::NormalRelay, relay);
            });
        (void)completion;

        prerequisites.verification.resolve(true);
        prerequisites.delivery.reject(make_delivered_block());
        require_one_async_verification(prerequisites);
        REQUIRE(progress.snapshot() == before);
    }

    SECTION("relay verification failure after successful delivery")
    {
        REQUIRE(has_verified_delivery_coordinator<VoteRelay>::value);
        const auto relay = make_relay(core, key);
        DeferredPrerequisites prerequisites;
        ProtocolProgressProbe progress(core, key);
        const auto before = progress.snapshot();
        auto completion = start_coordination(
            relay,
            prerequisites,
            [&progress, &relay](const block_t &)
            {
                progress.apply(ProgressPath::RelayPassThrough, relay);
            });
        (void)completion;

        prerequisites.delivery.resolve(make_delivered_block());
        prerequisites.verification.resolve(false);
        require_one_async_verification(prerequisites);
        REQUIRE(progress.snapshot() == before);
    }

    SECTION("non-root vote delivery failure after successful verification")
    {
        REQUIRE(has_verified_delivery_coordinator<Vote>::value);
        const auto vote = core.make_vote(1, 1, key);
        DeferredPrerequisites prerequisites;
        ProtocolProgressProbe progress(core, key);
        const auto before = progress.snapshot();
        auto completion = start_coordination(
            vote,
            prerequisites,
            [&progress, &vote](const block_t &)
            {
                progress.apply(ProgressPath::NonRootDirectVote, vote);
            });
        (void)completion;

        prerequisites.verification.resolve(true);
        prerequisites.delivery.reject(make_delivered_block());
        require_one_async_verification(prerequisites);
        REQUIRE(progress.snapshot() == before);
    }

    SECTION("root vote verification failure after successful delivery")
    {
        REQUIRE(has_verified_delivery_coordinator<Vote>::value);
        const auto vote = core.make_vote(1, 1, key);
        DeferredPrerequisites prerequisites;
        ProtocolProgressProbe progress(core, key);
        const auto before = progress.snapshot();
        auto completion = start_coordination(
            vote,
            prerequisites,
            [&progress, &vote](const block_t &)
            {
                progress.apply(ProgressPath::RootDirectVote, vote);
            });
        (void)completion;

        prerequisites.delivery.resolve(make_delivered_block());
        prerequisites.verification.resolve(false);
        require_one_async_verification(prerequisites);
        REQUIRE(progress.snapshot() == before);
    }

    SECTION("mismatched delivered block suppresses progress")
    {
        REQUIRE(has_verified_delivery_coordinator<Vote>::value);
        const auto mismatched_hash = make_digest(0x86);
        const auto mismatched_key = make_test_proposal_key(mismatched_hash);
        const auto vote = core.make_vote(1, 1, mismatched_key);
        DeferredPrerequisites prerequisites;
        ProtocolProgressProbe progress(core, mismatched_key);
        const auto before = progress.snapshot();
        auto completion = start_coordination(
            vote,
            prerequisites,
            [&progress, &vote](const block_t &)
            {
                progress.apply(ProgressPath::RootDirectVote, vote);
            });
        (void)completion;

        prerequisites.verification.resolve(true);
        prerequisites.delivery.resolve(make_delivered_block());
        require_one_async_verification(prerequisites);
        REQUIRE(progress.snapshot() == before);
    }
}
