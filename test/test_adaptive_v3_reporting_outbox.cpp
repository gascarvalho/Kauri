#include "catch.hpp"

#include <limits>
#include <memory>
#include <vector>

#include "hotstuff/adaptive_v3_reporting_outbox.h"

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
            {11, 0, digest("predecessor")};
        identity.predecessor_boundary_generation =
            *checked_activation_generation(11, 1);
        identity.successor_configuration = {12, 0, digest("successor")};
        identity.successor_activation_generation =
            *checked_activation_generation(12, 0);
        identity.command_payload_digest = digest("command");
        identity.command_block_height = 200;
        identity.command_block_hash = digest("command-block");
        identity.activation_delay_blocks = 2;
        identity.activation_height = 202;
        identity.activation_boundary_block_hash = digest("boundary-block");
    }

    ActivationReadinessCertificateV1 certificate(
        const std::vector<ReplicaID> &signers) const
    {
        std::vector<ActivationReadyObservationV1> observations;
        observations.reserve(signers.size());
        for (const auto signer : signers)
        {
            observations.push_back(sign_activation_ready_observation(
                identity, signer, 1, 1, *keys.at(signer)));
        }
        return make_activation_readiness_certificate(
            identity, std::move(observations));
    }
};

AdaptiveV3CertificateDeliveryConfig config(
    std::vector<ReplicaID> recipients,
    std::uint32_t attempts = 3,
    std::uint64_t retry = 10)
{
    AdaptiveV3CertificateDeliveryConfig result;
    result.recipients = std::move(recipients);
    result.maximum_attempts = attempts;
    result.retry_interval_ticks = retry;
    result.wire_limits.maximum_members = 31;
    result.wire_limits.maximum_payload_bytes = 64 * 1024;
    return result;
}

ActivationReadinessAckV1 ack(
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

TEST_CASE("M1 N7 outbox retries byte-identical certificate until exact ACK")
{
    Fixture fixture(7);
    const auto certificate = fixture.certificate({0, 1, 2, 3, 4});
    AdaptiveV3CertificateOutbox outbox(
        certificate, config({2, 3}, 2, 10));
    const auto canonical = outbox.canonical_bytes();

    const auto first = outbox.begin(2, 100);
    REQUIRE(first.has_value());
    REQUIRE(first->attempt == 1);
    REQUIRE(first->bytes == &outbox.canonical_bytes());
    REQUIRE(*first->bytes == canonical);
    REQUIRE_FALSE(outbox.begin(2, 100).has_value());
    REQUIRE(
        outbox.result(2, 2, true, 100) ==
        AdaptiveV3CertificateDeliveryDisposition::invalid_ack);
    REQUIRE(
        outbox.result(2, 1, true, 100) ==
        AdaptiveV3CertificateDeliveryDisposition::queued);
    REQUIRE_FALSE(outbox.begin(2, 109).has_value());

    const auto retry = outbox.begin(2, 110);
    REQUIRE(retry.has_value());
    REQUIRE(retry->attempt == 2);
    REQUIRE(retry->bytes == first->bytes);
    REQUIRE(retry->payload_digest == first->payload_digest);
    REQUIRE(*retry->bytes == canonical);

    const auto exact_ack = ack(2, certificate, *retry);
    REQUIRE(
        outbox.acknowledge(exact_ack, 110) ==
        AdaptiveV3CertificateDeliveryDisposition::acknowledged);
    REQUIRE(
        outbox.acknowledge(exact_ack, 110) ==
        AdaptiveV3CertificateDeliveryDisposition::duplicate_ack);
    REQUIRE_FALSE(outbox.terminal());

    const auto other = outbox.begin(3, 110);
    REQUIRE(other.has_value());
    REQUIRE(
        outbox.result(3, 1, false, 110) ==
        AdaptiveV3CertificateDeliveryDisposition::queued);
    REQUIRE_FALSE(outbox.begin(3, 119).has_value());
    const auto other_retry = outbox.begin(3, 120);
    REQUIRE(other_retry.has_value());
    REQUIRE(
        outbox.result(3, 2, false, 120) ==
        AdaptiveV3CertificateDeliveryDisposition::retry_exhausted);
    REQUIRE_FALSE(outbox.terminal());
    REQUIRE(
        outbox.acknowledge(ack(3, certificate, *other_retry), 120) ==
        AdaptiveV3CertificateDeliveryDisposition::retry_exhausted);
    REQUIRE_FALSE(outbox.terminal());
}

TEST_CASE("M1 outbox rejects every ACK identity mutation without progress")
{
    Fixture fixture(7);
    const auto certificate = fixture.certificate({0, 1, 2, 3, 4});
    AdaptiveV3CertificateOutbox outbox(certificate, config({2}));

    const auto expected_delivery = AdaptiveV3CertificateDelivery{
        2,
        1,
        &outbox.canonical_bytes(),
        activation_readiness_ack_payload_digest(
            MsgActivationReadinessCertificate::opcode,
            outbox.canonical_bytes())};
    const auto exact = ack(2, certificate, expected_delivery);
    REQUIRE(
        outbox.acknowledge(exact, 0) ==
        AdaptiveV3CertificateDeliveryDisposition::invalid_ack);

    const auto delivery = outbox.begin(2, 0);
    REQUIRE(delivery.has_value());

    auto changed_schema = exact;
    changed_schema.schema_version += 1;
    REQUIRE(
        outbox.acknowledge(changed_schema, 0) ==
        AdaptiveV3CertificateDeliveryDisposition::invalid_ack);

    auto changed_opcode = exact;
    changed_opcode.acknowledged_opcode =
        MsgActivationReadyObservation::opcode;
    REQUIRE(
        outbox.acknowledge(changed_opcode, 0) ==
        AdaptiveV3CertificateDeliveryDisposition::invalid_ack);

    auto changed_recipient = exact;
    changed_recipient.recipient_replica_id = 3;
    REQUIRE(
        outbox.acknowledge(changed_recipient, 0) ==
        AdaptiveV3CertificateDeliveryDisposition::invalid_ack);

    auto changed_identity = exact;
    changed_identity.identity.activation_boundary_block_hash =
        digest("wrong-boundary");
    REQUIRE(
        outbox.acknowledge(changed_identity, 0) ==
        AdaptiveV3CertificateDeliveryDisposition::invalid_ack);

    auto changed_certificate = exact;
    changed_certificate.certificate_digest = digest("wrong-certificate");
    REQUIRE(
        outbox.acknowledge(changed_certificate, 0) ==
        AdaptiveV3CertificateDeliveryDisposition::invalid_ack);

    auto changed_payload = exact;
    changed_payload.payload_digest = digest("wrong-payload");
    REQUIRE(
        outbox.acknowledge(changed_payload, 0) ==
        AdaptiveV3CertificateDeliveryDisposition::invalid_ack);

    auto rejected = exact;
    rejected.disposition =
        ActivationReadinessAckDisposition::permanent_rejection;
    REQUIRE(
        outbox.acknowledge(rejected, 0) ==
        AdaptiveV3CertificateDeliveryDisposition::rejected_ack);
    REQUIRE_FALSE(outbox.terminal());

    REQUIRE(
        outbox.acknowledge(exact, 0) ==
        AdaptiveV3CertificateDeliveryDisposition::acknowledged);
    REQUIRE(outbox.terminal());
}

TEST_CASE("M1 outbox validates bounds and saturates retry arithmetic")
{
    Fixture fixture(7);
    const auto certificate = fixture.certificate({0, 1, 2, 3, 4});

    REQUIRE_THROWS_AS(
        AdaptiveV3CertificateOutbox(certificate, config({})),
        std::invalid_argument);
    REQUIRE_THROWS_AS(
        AdaptiveV3CertificateOutbox(certificate, config({2, 2})),
        std::invalid_argument);
    REQUIRE_THROWS_AS(
        AdaptiveV3CertificateOutbox(certificate, config({3, 2})),
        std::invalid_argument);
    REQUIRE_THROWS_AS(
        AdaptiveV3CertificateOutbox(certificate, config({2}, 0)),
        std::invalid_argument);
    REQUIRE_THROWS_AS(
        AdaptiveV3CertificateOutbox(certificate, config({2}, 1, 0)),
        std::invalid_argument);

    AdaptiveV3CertificateOutbox outbox(certificate, config({2}, 2, 10));
    const auto first = outbox.begin(
        2, std::numeric_limits<std::uint64_t>::max() - 5);
    REQUIRE(first.has_value());
    REQUIRE(
        outbox.result(
            2,
            first->attempt,
            false,
            std::numeric_limits<std::uint64_t>::max() - 5) ==
        AdaptiveV3CertificateDeliveryDisposition::queued);
    REQUIRE_FALSE(outbox.begin(
        2, std::numeric_limits<std::uint64_t>::max() - 1));
    REQUIRE(outbox.begin(
        2, std::numeric_limits<std::uint64_t>::max()));
}

TEST_CASE("M1 outbox declares missing final ACK only at its retry boundary")
{
    Fixture fixture(7);
    const auto certificate = fixture.certificate({0, 1, 2, 3, 4});
    AdaptiveV3CertificateOutbox outbox(
        certificate, config({2}, 2, 10));

    const auto first = outbox.begin(2, 0);
    REQUIRE(first.has_value());
    REQUIRE(outbox.result(2, 1, true, 0) ==
            AdaptiveV3CertificateDeliveryDisposition::queued);
    const auto final_attempt = outbox.begin(2, 10);
    REQUIRE(final_attempt.has_value());
    REQUIRE(outbox.result(2, 2, true, 10) ==
            AdaptiveV3CertificateDeliveryDisposition::queued);
    REQUIRE_FALSE(outbox.retry_exhausted(19));
    REQUIRE(outbox.retry_exhausted(20));

    const auto due_ack = outbox.acknowledge(ack(2, certificate, *final_attempt), 20);
    REQUIRE((due_ack == AdaptiveV3CertificateDeliveryDisposition::invalid_ack ||
             due_ack == AdaptiveV3CertificateDeliveryDisposition::retry_exhausted));
    // Observing the half-open final boundary is irreversible: a valid ACK
    // cannot regain delivery by presenting an earlier logical tick.
    const auto rewound_ack = outbox.acknowledge(ack(2, certificate, *final_attempt), 19);
    REQUIRE((rewound_ack == AdaptiveV3CertificateDeliveryDisposition::invalid_ack ||
             rewound_ack == AdaptiveV3CertificateDeliveryDisposition::retry_exhausted));
    REQUIRE(outbox.retry_exhausted(20));
    REQUIRE_FALSE(outbox.terminal());

    AdaptiveV3CertificateOutbox before_boundary(
        certificate, config({2}, 2, 10));
    const auto first_before = before_boundary.begin(2, 0);
    REQUIRE(first_before.has_value());
    REQUIRE(before_boundary.result(2, 1, true, 0) ==
            AdaptiveV3CertificateDeliveryDisposition::queued);
    const auto final_before = before_boundary.begin(2, 10);
    REQUIRE(final_before.has_value());
    REQUIRE(before_boundary.result(2, 2, true, 10) ==
            AdaptiveV3CertificateDeliveryDisposition::queued);
    REQUIRE(before_boundary.acknowledge(
                ack(2, certificate, *final_before), 19) ==
            AdaptiveV3CertificateDeliveryDisposition::acknowledged);
    REQUIRE_FALSE(before_boundary.retry_exhausted(20));
    REQUIRE(before_boundary.terminal());
}

TEST_CASE("M1 N31 outbox requires all 28 exact recipient ACKs")
{
    Fixture fixture(31);
    std::vector<ReplicaID> survivors;
    for (ReplicaID id = 3; id < 31; ++id)
        survivors.push_back(id);
    const auto certificate = fixture.certificate(survivors);
    AdaptiveV3CertificateOutbox outbox(
        certificate, config(survivors, 2, 10));

    for (const auto recipient : survivors)
    {
        const auto delivery = outbox.begin(recipient, 0);
        REQUIRE(delivery.has_value());
        REQUIRE(
            outbox.result(recipient, delivery->attempt, true, 0) ==
            AdaptiveV3CertificateDeliveryDisposition::queued);
        REQUIRE(
            outbox.acknowledge(ack(recipient, certificate, *delivery), 0) ==
            AdaptiveV3CertificateDeliveryDisposition::acknowledged);
        if (recipient != survivors.back())
            REQUIRE_FALSE(outbox.terminal());
    }
    REQUIRE(outbox.terminal());
}
