#include "catch.hpp"

#include <memory>
#include <vector>

#include "hotstuff/adaptive_v3_manager_activation.h"

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

    explicit Fixture(std::size_t member_count = 7)
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
            {12, 0, digest("predecessor")};
        identity.predecessor_boundary_generation =
            *checked_activation_generation(12, 1);
        identity.successor_configuration = {13, 0, digest("successor")};
        identity.successor_activation_generation =
            *checked_activation_generation(13, 0);
        identity.command_payload_digest = digest("command");
        identity.command_block_height = 100;
        identity.command_block_hash = digest("command-block");
        identity.activation_delay_blocks = 2;
        identity.activation_height = 102;
        identity.activation_boundary_block_hash = digest("boundary");
    }

    bytearray_t observation(
        ReplicaID signer,
        const ActivationReadyIdentityV1 *override_identity = nullptr,
        std::uint64_t sequence = 1) const
    {
        const auto value = sign_activation_ready_observation(
            override_identity == nullptr ? identity : *override_identity,
            signer,
            sequence,
            sequence,
            *keys.at(signer));
        return encode_activation_ready_observation(value, {64 * 1024, 7});
    }

    AdaptiveV3ManagerActivation manager() const
    {
        return AdaptiveV3ManagerActivation({
            identity,
            members,
            5,
            2,
            10,
            {64 * 1024, members.size()}});
    }
};

ActivationReadinessAckV1 acknowledgement(
    ReplicaID recipient,
    const ActivationReadinessCertificateV1 &certificate,
    const AdaptiveV3CertificateDelivery &delivery)
{
    ActivationReadinessAckV1 result;
    result.acknowledged_opcode =
        MsgActivationReadinessCertificate::opcode;
    result.recipient_replica_id = recipient;
    result.identity = certificate.identity;
    result.certificate_digest = certificate.certificate_digest;
    result.payload_digest = delivery.payload_digest;
    return result;
}
} // namespace

TEST_CASE("M1 manager transport binds TLS, releases once, and ACKs exact bytes")
{
    Fixture fixture;
    auto manager = fixture.manager();
    REQUIRE(manager.status() ==
            AdaptiveV3ManagerActivationStatus::collecting);

    const auto wrong_tls = manager.record_observation(1, fixture.observation(2));
    REQUIRE(wrong_tls.disposition ==
            AdaptiveV3ManagerReadinessDisposition::rejected_peer_binding);
    REQUIRE(manager.accepted_count() == 0);

    auto malformed = fixture.observation(2);
    malformed.pop_back();
    const auto rejected = manager.record_observation(2, malformed);
    REQUIRE(rejected.wire_error != ActivationReadinessWireError::none);
    REQUIRE(manager.accepted_count() == 0);

    for (const auto signer : std::vector<ReplicaID>{6, 2, 4, 1})
    {
        const auto result =
            manager.record_observation(signer, fixture.observation(signer));
        REQUIRE(result.disposition ==
                AdaptiveV3ManagerReadinessDisposition::accepted);
        REQUIRE_FALSE(result.certificate_assembled);
    }
    const auto released = manager.record_observation(5, fixture.observation(5));
    REQUIRE(released.disposition ==
            AdaptiveV3ManagerReadinessDisposition::released);
    REQUIRE(released.certificate_assembled);
    REQUIRE(manager.status() ==
            AdaptiveV3ManagerActivationStatus::distributing);
    REQUIRE(manager.certificate() != nullptr);
    REQUIRE(manager.canonical_certificate_bytes() != nullptr);
    const auto immutable = *manager.canonical_certificate_bytes();

    const auto duplicate = manager.record_observation(0, fixture.observation(0));
    REQUIRE(duplicate.disposition ==
            AdaptiveV3ManagerReadinessDisposition::released);
    REQUIRE_FALSE(duplicate.certificate_assembled);
    REQUIRE(*manager.canonical_certificate_bytes() == immutable);

    REQUIRE_FALSE(manager.begin_delivery(0, 0).has_value());
    REQUIRE_FALSE(manager.begin_delivery(3, 0).has_value());
    std::uint64_t delivery_tick = 2;
    for (const auto recipient :
         std::vector<ReplicaID>{1, 2, 4, 5, 6})
    {
        const auto delivery = manager.begin_delivery(recipient, delivery_tick);
        REQUIRE(delivery.has_value());
        REQUIRE(*delivery->bytes == immutable);
        REQUIRE(manager.record_delivery_result(
                    recipient, delivery->attempt, true, delivery_tick) ==
                AdaptiveV3CertificateDeliveryDisposition::queued);
        const auto ack = acknowledgement(
            recipient, *manager.certificate(), *delivery);
        const auto payload = encode_activation_readiness_ack(
            ack, {64 * 1024, 7});
        REQUIRE(manager.record_acknowledgement(
                    recipient == 1 ? 2 : recipient, delivery_tick + 1, payload) ==
                (recipient == 1
                     ? AdaptiveV3CertificateDeliveryDisposition::invalid_ack
                     : AdaptiveV3CertificateDeliveryDisposition::acknowledged));
        if (recipient == 1)
            REQUIRE(manager.record_acknowledgement(1, delivery_tick + 1, payload) ==
                    AdaptiveV3CertificateDeliveryDisposition::acknowledged);
        delivery_tick += 2;
    }
    REQUIRE(manager.status() ==
            AdaptiveV3ManagerActivationStatus::terminal);
}

TEST_CASE("M1 manager quarantines valid cross-identity equivocation")
{
    Fixture fixture;
    auto manager = fixture.manager();
    REQUIRE(manager.record_observation(3, fixture.observation(3)).disposition ==
            AdaptiveV3ManagerReadinessDisposition::accepted);

    auto conflicting = fixture.identity;
    conflicting.activation_boundary_block_hash = digest("other-boundary");
    const auto conflict = manager.record_observation(
        3, fixture.observation(3, &conflicting, 2));
    REQUIRE(conflict.disposition ==
            AdaptiveV3ManagerReadinessDisposition::rejected_conflict);
    REQUIRE(manager.quarantined(3));
    REQUIRE(manager.accepted_count() == 0);
    REQUIRE(manager.status() ==
            AdaptiveV3ManagerActivationStatus::collecting);
}

TEST_CASE("M1 post-release equivocation is visible without changing certificate")
{
    Fixture fixture;
    auto manager = fixture.manager();
    for (ReplicaID signer = 0; signer < 5; ++signer)
        manager.record_observation(signer, fixture.observation(signer));
    REQUIRE(manager.canonical_certificate_bytes() != nullptr);
    const auto immutable = *manager.canonical_certificate_bytes();

    auto conflicting = fixture.identity;
    conflicting.activation_boundary_block_hash =
        digest("post-release-conflict");
    const auto result = manager.record_observation(
        2, fixture.observation(2, &conflicting, 2));
    REQUIRE(result.disposition ==
            AdaptiveV3ManagerReadinessDisposition::rejected_conflict);
    REQUIRE(manager.quarantined(2));
    REQUIRE(*manager.canonical_certificate_bytes() == immutable);
}

TEST_CASE("M1 manager rejects ACK identity and payload mutations atomically")
{
    Fixture fixture;
    auto manager = fixture.manager();
    for (ReplicaID signer = 0; signer < 5; ++signer)
        manager.record_observation(signer, fixture.observation(signer));
    const auto delivery = manager.begin_delivery(0, 0);
    REQUIRE(delivery.has_value());
    REQUIRE(manager.record_delivery_result(0, delivery->attempt, true, 5) ==
            AdaptiveV3CertificateDeliveryDisposition::queued);

    auto ack = acknowledgement(0, *manager.certificate(), *delivery);
    ack.identity.command_block_hash = digest("wrong-command");
    const auto payload = encode_activation_readiness_ack(
        ack, {64 * 1024, 7});
    REQUIRE(manager.record_acknowledgement(0, 5, payload) ==
            AdaptiveV3CertificateDeliveryDisposition::invalid_ack);
    REQUIRE(manager.status() ==
            AdaptiveV3ManagerActivationStatus::distributing);
}

TEST_CASE("M1 manager exposes final ACK timeout without rebuilding bytes")
{
    Fixture fixture;
    auto manager = fixture.manager();
    for (ReplicaID signer = 0; signer < 5; ++signer)
        manager.record_observation(signer, fixture.observation(signer));

    const auto immutable = *manager.canonical_certificate_bytes();
    const auto first = manager.begin_delivery(0, 0);
    REQUIRE(first.has_value());
    REQUIRE(first->attempt == 1);
    REQUIRE(manager.record_delivery_result(0, 1, true, 0) ==
            AdaptiveV3CertificateDeliveryDisposition::queued);
    REQUIRE_FALSE(manager.delivery_retry_exhausted(9));

    const auto second = manager.begin_delivery(0, 10);
    REQUIRE(second.has_value());
    REQUIRE(second->attempt == 2);
    REQUIRE(*second->bytes == immutable);
    REQUIRE(manager.record_delivery_result(0, 2, true, 10) ==
            AdaptiveV3CertificateDeliveryDisposition::queued);
    REQUIRE_FALSE(manager.delivery_retry_exhausted(19));
    REQUIRE(manager.delivery_retry_exhausted(20));
    REQUIRE_FALSE(manager.begin_delivery(0, 20).has_value());
    const auto payload = encode_activation_readiness_ack(
        acknowledgement(0, *manager.certificate(), *second), {64 * 1024, 7});
    const auto regressing_ack = manager.record_acknowledgement(0, 19, payload);
    REQUIRE((regressing_ack == AdaptiveV3CertificateDeliveryDisposition::invalid_ack ||
             regressing_ack == AdaptiveV3CertificateDeliveryDisposition::retry_exhausted));
    REQUIRE(*manager.canonical_certificate_bytes() == immutable);
    REQUIRE(manager.status() ==
            AdaptiveV3ManagerActivationStatus::terminal);
}

TEST_CASE("M1 N31 owner distributes only to the R28 authenticated signers")
{
    Fixture fixture(31);
    AdaptiveV3ManagerActivation manager({
        fixture.identity,
        fixture.members,
        28,
        2,
        10,
        {64 * 1024, 31}});
    for (ReplicaID signer = 3; signer < 31; ++signer)
        manager.record_observation(signer, fixture.observation(signer));
    REQUIRE(manager.certificate() != nullptr);
    REQUIRE(manager.certificate()->observations.size() == 28);
    for (ReplicaID absent = 0; absent < 3; ++absent)
        REQUIRE_FALSE(manager.begin_delivery(absent, 0).has_value());

    std::uint64_t delivery_tick = 2;
    for (ReplicaID recipient = 3; recipient < 31; ++recipient)
    {
        const auto delivery = manager.begin_delivery(recipient, delivery_tick);
        REQUIRE(delivery.has_value());
        REQUIRE(manager.record_delivery_result(
                    recipient, delivery->attempt, true, delivery_tick) ==
                AdaptiveV3CertificateDeliveryDisposition::queued);
        const auto payload = encode_activation_readiness_ack(
            acknowledgement(
                recipient, *manager.certificate(), *delivery),
            {64 * 1024, 31});
        REQUIRE(manager.record_acknowledgement(recipient, delivery_tick + 1, payload) ==
                AdaptiveV3CertificateDeliveryDisposition::acknowledged);
        delivery_tick += 2;
    }
    REQUIRE(manager.status() ==
            AdaptiveV3ManagerActivationStatus::terminal);
}
