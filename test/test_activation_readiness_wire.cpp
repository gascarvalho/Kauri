#include "catch.hpp"

#include <algorithm>
#include <limits>
#include <memory>
#include <string>
#include <utility>
#include <vector>

#include "hotstuff/activation_readiness_wire.h"

using namespace hotstuff;

namespace
{

uint256_t test_digest(const std::string &text)
{
    return DataStream(text).get_hash();
}

struct ReadinessFixture
{
    std::vector<std::unique_ptr<PrivKeyBLS>> private_keys;
    std::vector<std::pair<ReplicaID, PubKeyBLS>> members;
    ActivationReadyIdentityV1 identity;

    explicit ReadinessFixture(std::size_t count)
    {
        for (ReplicaID id = 0; id < count; ++id)
        {
            auto key = std::make_unique<PrivKeyBLS>();
            key->from_rand();
            members.emplace_back(id, PubKeyBLS(*key));
            private_keys.emplace_back(std::move(key));
        }
        identity.membership_digest =
            canonical_activation_readiness_membership_digest(members);
        identity.predecessor_boundary_configuration =
            ConfigurationId{7, 1, test_digest("predecessor")};
        identity.predecessor_boundary_generation =
            *checked_activation_generation(7, 3);
        identity.successor_configuration =
            ConfigurationId{8, 0, test_digest("successor")};
        identity.successor_activation_generation =
            *checked_activation_generation(8, 0);
        identity.command_payload_digest = test_digest("command");
        identity.command_block_height = 100;
        identity.command_block_hash = test_digest("command-block");
        identity.activation_delay_blocks = 5;
        identity.activation_height = 105;
        identity.activation_boundary_block_hash = test_digest("boundary-block");
    }

    ActivationReadinessCertificateV1 certificate(std::size_t signer_count) const
    {
        std::vector<ActivationReadyObservationV1> observations;
        for (ReplicaID id = 0; id < signer_count; ++id)
            observations.emplace_back(sign_activation_ready_observation(
                identity, id, id + 1, 1000 + id, *private_keys.at(id)));
        return make_activation_readiness_certificate(identity, std::move(observations));
    }
};

bool verifies(const ReadinessFixture &fixture,
              const ActivationReadinessCertificateV1 &certificate)
{
    return verify_activation_readiness_certificate(
        certificate, fixture.identity, fixture.identity.membership_digest,
        fixture.members);
}

} // namespace

TEST_CASE("W1 derives readiness quorum from canonical N7 and N31 BLS membership")
{
    ReadinessFixture n7(7);
    const auto n7_four = n7.certificate(4);
    const auto n7_five = n7.certificate(5);
    REQUIRE_FALSE(verifies(n7, n7_four));
    REQUIRE(verifies(n7, n7_five));

    ReadinessFixture n31(31);
    const auto n31_twenty = n31.certificate(20);
    const auto n31_twenty_one = n31.certificate(21);
    REQUIRE_FALSE(verifies(n31, n31_twenty));
    REQUIRE(verifies(n31, n31_twenty_one));
}

TEST_CASE("W1 rejects membership, identity, signature, and certificate equivocation")
{
    ReadinessFixture fixture(7);
    const auto valid = fixture.certificate(5);
    REQUIRE(verifies(fixture, valid));

    auto id_substituted = fixture.members;
    id_substituted[0].first = 8;
    REQUIRE_FALSE(verify_activation_readiness_certificate(
        valid, fixture.identity, fixture.identity.membership_digest, id_substituted));

    std::vector<std::pair<ReplicaID, PubKeyBLS>> key_substituted;
    for (std::size_t i = 0; i < fixture.members.size(); ++i)
        key_substituted.emplace_back(
            fixture.members[i].first,
            PubKeyBLS(*fixture.private_keys[(i == 0) ? 1 : i]));
    REQUIRE_FALSE(verify_activation_readiness_certificate(
        valid, fixture.identity, fixture.identity.membership_digest, key_substituted));

    std::vector<std::pair<ReplicaID, PubKeyBLS>> unsorted;
    unsorted.emplace_back(fixture.members[1]);
    unsorted.emplace_back(fixture.members[0]);
    for (std::size_t i = 2; i < fixture.members.size(); ++i)
        unsorted.emplace_back(fixture.members[i]);
    REQUIRE_FALSE(verify_activation_readiness_certificate(
        valid, fixture.identity, fixture.identity.membership_digest, unsorted));
    std::vector<std::pair<ReplicaID, PubKeyBLS>> duplicate_member;
    duplicate_member.emplace_back(fixture.members[0]);
    duplicate_member.emplace_back(fixture.members[0]);
    for (std::size_t i = 2; i < fixture.members.size(); ++i)
        duplicate_member.emplace_back(fixture.members[i]);
    REQUIRE_FALSE(verify_activation_readiness_certificate(
        valid, fixture.identity, fixture.identity.membership_digest, duplicate_member));

    std::vector<ActivationReadyObservationV1> bad_signature_observations;
    bad_signature_observations.emplace_back(ActivationReadyObservationV1{
        fixture.identity, 0, 1, 1000, true,
        SigSecBLS(test_digest("other-protocol-domain"), *fixture.private_keys[0])});
    for (std::size_t i = 1; i < valid.observations.size(); ++i)
        bad_signature_observations.emplace_back(valid.observations[i]);
    ActivationReadinessCertificateV1 bad_signature;
    bad_signature.identity = fixture.identity;
    bad_signature.observations = std::move(bad_signature_observations);
    bad_signature.certificate_digest =
        activation_readiness_certificate_digest(bad_signature);
    REQUIRE_FALSE(verifies(fixture, bad_signature));

    ReadinessFixture generation_fixture(7);
    generation_fixture.identity.predecessor_boundary_generation =
        *checked_activation_generation(7, 17);
    const auto valid_nonzero_predecessor = generation_fixture.certificate(5);
    REQUIRE(verifies(generation_fixture, valid_nonzero_predecessor));
    auto invalid_predecessor = valid_nonzero_predecessor;
    invalid_predecessor.identity.predecessor_boundary_generation = 0;
    REQUIRE_FALSE(verifies(generation_fixture, invalid_predecessor));
    ReadinessFixture successor_fixture(7);
    successor_fixture.identity.successor_activation_generation =
        *checked_activation_generation(8, 1);
    REQUIRE_THROWS(successor_fixture.certificate(5));
    auto mixed_identities = valid;
    ++mixed_identities.observations[0].identity.activation_height;
    REQUIRE_FALSE(verifies(fixture, mixed_identities));
    auto overflow_height = valid;
    overflow_height.identity.command_block_height =
        std::numeric_limits<std::uint64_t>::max();
    overflow_height.identity.activation_delay_blocks = 1;
    overflow_height.identity.activation_height = 0;
    REQUIRE_FALSE(verifies(fixture, overflow_height));

    std::vector<ActivationReadyObservationV1> reordered_observations;
    reordered_observations.emplace_back(valid.observations[1]);
    reordered_observations.emplace_back(valid.observations[0]);
    for (std::size_t i = 2; i < valid.observations.size(); ++i)
        reordered_observations.emplace_back(valid.observations[i]);
    auto reordered = valid;
    reordered.observations = std::move(reordered_observations);
    REQUIRE_FALSE(verifies(fixture, reordered));
    auto duplicate_observation = valid;
    std::vector<ActivationReadyObservationV1> duplicate_observations;
    duplicate_observations.emplace_back(valid.observations[0]);
    duplicate_observations.emplace_back(valid.observations[0]);
    for (std::size_t i = 2; i < valid.observations.size(); ++i)
        duplicate_observations.emplace_back(valid.observations[i]);
    duplicate_observation.observations = std::move(duplicate_observations);
    REQUIRE_FALSE(verifies(fixture, duplicate_observation));
}

TEST_CASE("W1 wire bounds and acknowledgement digest bind canonical certificate bytes")
{
    ReadinessFixture fixture(7);
    const auto certificate = fixture.certificate(5);
    const ActivationReadinessWireLimits limits{4096, 7};
    const auto observation_payload = encode_activation_ready_observation(
        certificate.observations[0], limits);
    const auto decoded_observation = decode_activation_ready_observation(
        observation_payload, limits);
    INFO("observation decode error=" << static_cast<int>(decoded_observation.error));
    REQUIRE(decoded_observation);
    constexpr std::size_t identity_bytes =
        sizeof(std::uint32_t) + 32 + 2 * (sizeof(std::uint32_t) * 2 + 32) +
        sizeof(std::uint64_t) * 5 + 32 * 3;
    REQUIRE(observation_payload.size() ==
            activation_ready_observation_domain().size() + sizeof(std::uint8_t) +
                identity_bytes + sizeof(ReplicaID) + sizeof(std::uint64_t) * 2 +
                sizeof(std::uint8_t) + bls::G2Element::SIZE);
    REQUIRE(activation_ready_observation_digest(certificate.observations[0]) ==
            activation_ready_observation_digest(*decoded_observation.value));
    const auto payload = encode_activation_readiness_certificate(certificate, limits);
    const auto decoded = decode_activation_readiness_certificate(payload, limits);
    INFO("certificate decode error=" << static_cast<int>(decoded.error));
    REQUIRE(decoded);
    REQUIRE(verifies(fixture, *decoded.value));

    const ActivationReadinessWireLimits too_small_members{4096, 4};
    const auto oversized = decode_activation_readiness_certificate(payload, too_small_members);
    REQUIRE_FALSE(oversized);
    REQUIRE(oversized.error == ActivationReadinessWireError::too_many_observations);
    auto truncated_payload = payload;
    truncated_payload.pop_back();
    REQUIRE_FALSE(decode_activation_readiness_certificate(truncated_payload, limits));
    auto trailing_payload = payload;
    trailing_payload.push_back(0);
    REQUIRE_FALSE(decode_activation_readiness_certificate(trailing_payload, limits));
    auto legacy_boolean_width = observation_payload;
    legacy_boolean_width.insert(legacy_boolean_width.end() - bls::G2Element::SIZE, 3, 0);
    REQUIRE_FALSE(decode_activation_ready_observation(legacy_boolean_width, limits));

    ReadinessFixture n31(31);
    const auto n31_certificate = n31.certificate(21);
    const ActivationReadinessWireLimits n31_limits{16384, 31};
    const auto n31_payload = encode_activation_readiness_certificate(
        n31_certificate, n31_limits);
    const auto n31_decoded = decode_activation_readiness_certificate(
        n31_payload, n31_limits);
    REQUIRE(n31_decoded);
    REQUIRE(verifies(n31, *n31_decoded.value));

    ActivationReadinessAckV1 ack;
    ack.acknowledged_opcode = MsgActivationReadinessCertificate::opcode;
    ack.recipient_replica_id = 6;
    ack.identity = fixture.identity;
    ack.certificate_digest = certificate.certificate_digest;
    ack.payload_digest = activation_readiness_ack_payload_digest(
        ack.acknowledged_opcode, payload);
    const auto ack_payload = encode_activation_readiness_ack(ack, limits);
    const auto decoded_ack = decode_activation_readiness_ack(ack_payload, limits);
    REQUIRE(decoded_ack);
    REQUIRE(decoded_ack.value->payload_digest == activation_readiness_ack_payload_digest(
        MsgActivationReadinessCertificate::opcode, payload));
    auto other_payload = payload;
    other_payload.back() ^= 1;
    REQUIRE(decoded_ack.value->payload_digest != activation_readiness_ack_payload_digest(
        MsgActivationReadinessCertificate::opcode, other_payload));
}

TEST_CASE("W1 standalone readiness identity wire is canonical and domain separated")
{
    ReadinessFixture fixture(7);
    const ActivationReadinessWireLimits limits{4096, 7};

    const auto payload = encode_activation_ready_identity_v1(
        fixture.identity, limits);
    const auto decoded = decode_activation_ready_identity_v1(
        payload, limits);
    INFO("identity decode error=" << static_cast<int>(decoded.error));
    REQUIRE(decoded);
    REQUIRE(decoded.value.has_value());
    REQUIRE(*decoded.value == fixture.identity);
    REQUIRE(payload == encode_activation_ready_identity_v1(
        *decoded.value, limits));

    const auto observation = sign_activation_ready_observation(
        fixture.identity, 0, 1, 1000, *fixture.private_keys[0]);
    const auto observation_payload = encode_activation_ready_observation(
        observation, limits);
    REQUIRE_FALSE(decode_activation_ready_identity_v1(
        observation_payload, limits));
    REQUIRE_FALSE(decode_activation_ready_observation(payload, limits));

    auto trailing = payload;
    trailing.push_back(0);
    REQUIRE_FALSE(decode_activation_ready_identity_v1(trailing, limits));

    auto wrong_domain = payload;
    REQUIRE_FALSE(wrong_domain.empty());
    wrong_domain.front() ^= 1;
    REQUIRE_FALSE(decode_activation_ready_identity_v1(
        wrong_domain, limits));

    const ActivationReadinessWireLimits too_small{payload.size() - 1, 7};
    const auto oversized = decode_activation_ready_identity_v1(
        payload, too_small);
    REQUIRE_FALSE(oversized);
    REQUIRE(oversized.error ==
            ActivationReadinessWireError::payload_too_large);
}
