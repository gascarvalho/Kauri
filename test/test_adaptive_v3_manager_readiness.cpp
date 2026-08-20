#include "catch.hpp"

#include <memory>
#include <vector>

#include "hotstuff/adaptive_v3_manager_readiness.h"

using namespace hotstuff;

namespace
{
uint256_t digest(const char *label)
{
    return DataStream(label).get_hash();
}

struct Fixture
{
    std::vector<std::unique_ptr<PrivKeyBLS>> keys;
    std::vector<std::pair<ReplicaID, PubKeyBLS>> members;
    ActivationReadyIdentityV1 identity;

    explicit Fixture(std::size_t member_count)
    {
        for (ReplicaID id = 0; id < member_count; ++id)
        {
            auto key = std::make_unique<PrivKeyBLS>();
            key->from_rand();
            members.emplace_back(id, PubKeyBLS(*key));
            keys.emplace_back(std::move(key));
        }

        identity.membership_digest =
            canonical_activation_readiness_membership_digest(members);
        identity.predecessor_boundary_configuration =
            {7, 0, digest("predecessor")};
        identity.predecessor_boundary_generation =
            *checked_activation_generation(7, 1);
        identity.successor_configuration = {8, 0, digest("successor")};
        identity.successor_activation_generation =
            *checked_activation_generation(8, 0);
        identity.command_payload_digest = digest("command");
        identity.command_block_height = 100;
        identity.command_block_hash = digest("command-block");
        identity.activation_delay_blocks = 2;
        identity.activation_height = 102;
        identity.activation_boundary_block_hash = digest("boundary-block");
    }

    ActivationReadyObservationV1 observation(
        ReplicaID signer,
        std::uint64_t sequence = 1,
        std::uint64_t clock = 1) const
    {
        return sign_activation_ready_observation(
            identity, signer, sequence, clock, *keys.at(signer));
    }

    ActivationReadyObservationV1 observation_for(
        const ActivationReadyIdentityV1 &exact_identity,
        ReplicaID signer,
        ReplicaID signing_key,
        std::uint64_t sequence = 1) const
    {
        return sign_activation_ready_observation(
            exact_identity,
            signer,
            sequence,
            sequence,
            *keys.at(signing_key));
    }

    std::vector<std::pair<ReplicaID, PubKeyBLS>> membership(
        const std::vector<ReplicaID> &order) const
    {
        std::vector<std::pair<ReplicaID, PubKeyBLS>> result;
        result.reserve(order.size());
        for (const auto id : order)
            result.emplace_back(id, PubKeyBLS(*keys.at(id)));
        return result;
    }
};
} // namespace

TEST_CASE("M1 N7 releases exactly at R with a canonical Q-valid certificate")
{
    Fixture fixture(7);
    AdaptiveV3ManagerReadinessCollector collector(
        fixture.identity, {fixture.members, 5});

    const std::vector<ReplicaID> arrival_order{6, 2, 4, 1, 5};
    for (std::size_t index = 0; index + 1 < arrival_order.size(); ++index)
    {
        const auto signer = arrival_order[index];
        REQUIRE(
            collector.ingest(signer, fixture.observation(signer)) ==
            AdaptiveV3ManagerReadinessDisposition::accepted);
    }
    REQUIRE_FALSE(collector.released());
    REQUIRE(collector.accepted_count() == 4);

    const auto last = arrival_order.back();
    REQUIRE(
        collector.ingest(last, fixture.observation(last)) ==
        AdaptiveV3ManagerReadinessDisposition::released);
    REQUIRE(collector.released());
    REQUIRE(collector.accepted_count() == 5);

    const auto *certificate = collector.certificate();
    REQUIRE(certificate != nullptr);
    REQUIRE(certificate->observations.size() == 5);
    REQUIRE(verify_activation_readiness_certificate(
        *certificate,
        fixture.identity,
        fixture.identity.membership_digest,
        fixture.members));
    for (std::size_t i = 1; i < certificate->observations.size(); ++i)
    {
        REQUIRE(
            certificate->observations[i - 1].signer_replica_id <
            certificate->observations[i].signer_replica_id);
    }

    const auto released_digest = certificate->certificate_digest;
    REQUIRE(
        collector.ingest(0, fixture.observation(0)) ==
        AdaptiveV3ManagerReadinessDisposition::released);
    REQUIRE(collector.certificate()->certificate_digest == released_digest);
}

TEST_CASE("M1 rejects source and identity mutations without contributing")
{
    Fixture fixture(7);
    AdaptiveV3ManagerReadinessCollector collector(
        fixture.identity, {fixture.members, 5});

    const auto exact = fixture.observation(0);
    REQUIRE(
        collector.ingest(1, exact) ==
        AdaptiveV3ManagerReadinessDisposition::rejected_peer_binding);
    REQUIRE(collector.accepted_count() == 0);

    REQUIRE(
        collector.ingest(99, fixture.observation_for(
                                 fixture.identity, 99, 0)) ==
        AdaptiveV3ManagerReadinessDisposition::rejected_nonmember);
    REQUIRE(collector.accepted_count() == 0);

    auto wrong_identity = fixture.identity;
    wrong_identity.command_block_hash = digest("wrong-command-block");
    const auto stale = fixture.observation_for(wrong_identity, 0, 0);
    REQUIRE(
        collector.ingest(0, stale) ==
        AdaptiveV3ManagerReadinessDisposition::rejected_wrong_identity);
    REQUIRE(collector.accepted_count() == 0);

    const auto wrong_key =
        fixture.observation_for(fixture.identity, 0, 1);
    REQUIRE(
        collector.ingest(0, wrong_key) ==
        AdaptiveV3ManagerReadinessDisposition::
            rejected_invalid_observation);
    REQUIRE(collector.accepted_count() == 0);

    REQUIRE(
        collector.ingest(0, exact) ==
        AdaptiveV3ManagerReadinessDisposition::accepted);
    REQUIRE(
        collector.ingest(0, exact) ==
        AdaptiveV3ManagerReadinessDisposition::duplicate);
    REQUIRE(collector.accepted_count() == 1);

    const auto conflict = fixture.observation(0, 2, 2);
    REQUIRE(
        collector.ingest(0, conflict) ==
        AdaptiveV3ManagerReadinessDisposition::rejected_conflict);
    REQUIRE(collector.quarantined(0));
    REQUIRE(collector.accepted_count() == 0);
    REQUIRE(
        collector.ingest(0, exact) ==
        AdaptiveV3ManagerReadinessDisposition::quarantined);
}

TEST_CASE("M1 projection binds dynamic readiness identity only at Q")
{
    Fixture fixture(7);
    const AdaptiveV3TransitionProjection projection{
        fixture.identity.predecessor_boundary_configuration.epoch_number,
        fixture.identity.predecessor_boundary_configuration.epoch_digest,
        {0, 1},
        fixture.identity.successor_configuration,
        fixture.identity.successor_activation_generation,
        fixture.identity.membership_digest,
        fixture.identity.command_payload_digest,
        fixture.identity.activation_delay_blocks,
        digest("bundle"), 1};
    AdaptiveV3ManagerReadinessCollector collector(
        projection, {fixture.members, 6});

    // A static-compatible alternative cannot pin the session below Q.
    auto alternate = fixture.identity;
    alternate.predecessor_boundary_configuration.tree_id = 1;
    alternate.predecessor_boundary_generation =
        *checked_activation_generation(7, 17);
    alternate.command_block_height = 101;
    alternate.command_block_hash = digest("other-command");
    alternate.activation_height = 103;
    alternate.activation_boundary_block_hash = digest("other-boundary");
    for (ReplicaID signer = 0; signer < 3; ++signer)
        REQUIRE(collector.ingest(
            signer, fixture.observation_for(alternate, signer, signer)) ==
            AdaptiveV3ManagerReadinessDisposition::accepted);

    for (ReplicaID signer = 3; signer < 7; ++signer)
        REQUIRE(collector.ingest(signer, fixture.observation(signer)) ==
            AdaptiveV3ManagerReadinessDisposition::accepted);
    REQUIRE_FALSE(collector.released()); // neither dynamic candidate reaches Q.

    // The fifth matching signer releases; the Q verifier is unchanged.
    // Use fresh setup because sources cannot legitimately sign two identities.
    AdaptiveV3ManagerReadinessCollector releaser(projection, {fixture.members, 6});
    for (ReplicaID signer = 0; signer < 5; ++signer)
    {
        const auto disposition = releaser.ingest(signer, fixture.observation(signer));
        REQUIRE(disposition == AdaptiveV3ManagerReadinessDisposition::accepted);
    }
    REQUIRE_FALSE(releaser.released()); // Q binds, but operational R is six.
    REQUIRE(releaser.ingest(5, fixture.observation(5)) ==
        AdaptiveV3ManagerReadinessDisposition::released);
    REQUIRE(releaser.certificate() != nullptr);
    REQUIRE_FALSE(releaser.certificate()->identity != fixture.identity);

    auto unknown_tree = fixture.identity;
    unknown_tree.predecessor_boundary_configuration.tree_id = 2;
    AdaptiveV3ManagerReadinessCollector tree_guard(
        projection, {fixture.members, 6});
    REQUIRE(tree_guard.ingest(
                0, fixture.observation_for(unknown_tree, 0, 0)) ==
            AdaptiveV3ManagerReadinessDisposition::rejected_wrong_identity);
    REQUIRE(tree_guard.accepted_count() == 0);
}

TEST_CASE("M1 validates release cardinality and canonical membership ownership")
{
    Fixture fixture(7);

    REQUIRE_THROWS_AS(
        AdaptiveV3ManagerReadinessCollector(
            fixture.identity, {fixture.members, 4}),
        std::invalid_argument);
    REQUIRE_THROWS_AS(
        AdaptiveV3ManagerReadinessCollector(
            fixture.identity, {fixture.members, 8}),
        std::invalid_argument);

    const auto reordered = fixture.membership({1, 0, 2, 3, 4, 5, 6});
    REQUIRE_THROWS_AS(
        AdaptiveV3ManagerReadinessCollector(
            fixture.identity, {reordered, 5}),
        std::invalid_argument);

    const auto duplicate = fixture.membership({0, 1, 2, 3, 4, 5, 5});
    REQUIRE_THROWS_AS(
        AdaptiveV3ManagerReadinessCollector(
            fixture.identity, {duplicate, 5}),
        std::invalid_argument);

    auto drifted_identity = fixture.identity;
    drifted_identity.membership_digest = digest("wrong-membership");
    REQUIRE_THROWS_AS(
        AdaptiveV3ManagerReadinessCollector(
            drifted_identity, {fixture.members, 5}),
        std::invalid_argument);

    // This construction/destruction is the direct ownership regression: the
    // old implementation sorted key-owning pairs and double-freed N31 keys.
    Fixture n31(31);
    REQUIRE_NOTHROW(AdaptiveV3ManagerReadinessCollector(
        n31.identity, {n31.members, 28}));
}

TEST_CASE("M1 N31 requires R28 while certificate validation retains Q21")
{
    Fixture fixture(31);

    SECTION("a signed identity conflict removes and quarantines its source")
    {
        AdaptiveV3ManagerReadinessCollector conflict_collector(
            fixture.identity, {fixture.members, 28});
        REQUIRE(
            conflict_collector.ingest(30, fixture.observation(30)) ==
            AdaptiveV3ManagerReadinessDisposition::accepted);
        auto conflicting_identity = fixture.identity;
        conflicting_identity.activation_boundary_block_hash =
            digest("conflicting-boundary");
        const auto conflict = fixture.observation_for(
            conflicting_identity, 30, 30, 2);
        REQUIRE(
            conflict_collector.ingest(30, conflict) ==
            AdaptiveV3ManagerReadinessDisposition::rejected_conflict);
        REQUIRE(conflict_collector.quarantined(30));
        REQUIRE(conflict_collector.accepted_count() == 0);
    }

    AdaptiveV3ManagerReadinessCollector collector(
        fixture.identity, {fixture.members, 28});

    for (ReplicaID signer = 30; signer >= 4; --signer)
    {
        REQUIRE(
            collector.ingest(signer, fixture.observation(signer)) ==
            AdaptiveV3ManagerReadinessDisposition::accepted);
    }
    REQUIRE(collector.accepted_count() == 27);
    REQUIRE_FALSE(collector.released());

    REQUIRE(
        collector.ingest(3, fixture.observation(3)) ==
        AdaptiveV3ManagerReadinessDisposition::released);
    REQUIRE(collector.accepted_count() == 28);
    REQUIRE(collector.certificate() != nullptr);
    REQUIRE(collector.certificate()->observations.size() == 28);
    REQUIRE(verify_activation_readiness_certificate(
        *collector.certificate(),
        fixture.identity,
        fixture.identity.membership_digest,
        fixture.members));
}
