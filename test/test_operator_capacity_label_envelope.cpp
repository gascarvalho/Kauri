#include "catch.hpp"

#include "hotstuff/operator_capacity_label_envelope.h"

namespace
{
using hotstuff::AdaptationEpochId;
using hotstuff::EpochTreeDefinition;
using hotstuff::OperatorCapacityArm;
using hotstuff::OperatorCapacityClass;
using hotstuff::OperatorCapacityLabel;
using hotstuff::OperatorCapacityLabelEnvelope;
using hotstuff::OperatorCapacityLabelEnvelopeIssuer;
using hotstuff::OperatorCapacitySnapshot;
using hotstuff::ReplicaID;

std::vector<ReplicaID> members()
{
    return {0, 1, 2, 3, 4, 5, 6};
}

hotstuff::PrivKeySecp256k1 key()
{
    hotstuff::PrivKeySecp256k1 value;
    value.from_hex("4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
    return value;
}

AdaptationEpochId epoch0()
{
    AdaptationEpochId value;
    value.epoch_number = 0;
    value.epoch_digest = hotstuff::uint256_t(hotstuff::from_hex(
        "a1a2a3a4a5a6a7a8a9aaabacadaeafb0b1b2b3b4b5b6b7b8b9babbbcbdbebfc0"));
    return value;
}

std::vector<EpochTreeDefinition> baseline_trees()
{
    return {
        {0, 2, 1, {0, 1, 2, 3, 4, 5, 6}, {}},
        {1, 2, 1, {1, 0, 2, 3, 4, 5, 6}, {}},
    };
}

OperatorCapacitySnapshot snapshot()
{
    OperatorCapacitySnapshot value;
    value.issuer_reference = "inesc-operator-capacity";
    value.predecessor = epoch0();
    value.valid_from_monotonic_ns = 1'000;
    value.valid_until_monotonic_ns = 2'000;
    for (const auto id : members())
        value.labels.push_back({id, id < 2 ? OperatorCapacityClass::slow :
            OperatorCapacityClass::fast});
    value.canonical_digest = hotstuff::operator_capacity_snapshot_digest(value);
    return value;
}

uint256_t topology_digest()
{
    return hotstuff::operator_capacity_baseline_topology_digest(
        epoch0(), members(), baseline_trees());
}

OperatorCapacityLabelEnvelope envelope()
{
    return hotstuff::sign_operator_capacity_label_envelope(
        members(), epoch0(), topology_digest(),
        OperatorCapacityArm::fast_priority_treatment, snapshot(), 21, key());
}

OperatorCapacityLabelEnvelopeIssuer issuer()
{
    const auto labels = snapshot();
    const auto private_key = key();
    return {21, labels.issuer_reference, labels.canonical_digest,
        hotstuff::PubKeySecp256k1(private_key)};
}

bool verifies(const OperatorCapacityLabelEnvelope &candidate,
              std::uint64_t now_monotonic_raw_ns = 1'500)
{
    return hotstuff::verify_operator_capacity_label_envelope(
        candidate, issuer(), members(), epoch0(), topology_digest(),
        now_monotonic_raw_ns).has_value();
}
} // namespace

TEST_CASE("operator-capacity label envelope binds the complete prelaunch input",
          "[operator-capacity][label-envelope]")
{
    const auto signed_envelope = envelope();
    REQUIRE(verifies(signed_envelope));

    const auto verified = hotstuff::verify_operator_capacity_label_envelope(
        signed_envelope, issuer(), members(), epoch0(), topology_digest(), 1'500);
    REQUIRE(verified);
    CHECK(verified->epoch0() == epoch0());
    CHECK(verified->baseline_topology_digest() == topology_digest());
    CHECK(verified->arm() == OperatorCapacityArm::fast_priority_treatment);
    CHECK(verified->capacity_snapshot().canonical_digest ==
        snapshot().canonical_digest);
    CHECK(verified->digest() ==
        hotstuff::operator_capacity_label_envelope_digest(signed_envelope));
}

TEST_CASE("operator-capacity label envelope rejects relabeling and context drift",
          "[operator-capacity][label-envelope]")
{
    const auto valid = envelope();
    CHECK(verifies(valid));

    auto changed = valid;
    changed.capacity_snapshot.labels[0].capacity = OperatorCapacityClass::fast;
    CHECK_FALSE(verifies(changed));
    changed = valid;
    changed.baseline_topology_digest = hotstuff::uint256_t(hotstuff::from_hex(
        "0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20"));
    CHECK_FALSE(verifies(changed));
    changed = valid;
    changed.epoch0.epoch_number = 1;
    CHECK_FALSE(verifies(changed));
    changed = valid;
    changed.membership.pop_back();
    CHECK_FALSE(verifies(changed));

    auto wrong_digest_issuer = issuer();
    wrong_digest_issuer.approved_capacity_digest = hotstuff::uint256_t(
        hotstuff::from_hex("ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"));
    CHECK_FALSE(hotstuff::verify_operator_capacity_label_envelope(
        valid, wrong_digest_issuer, members(), epoch0(), topology_digest(), 1'500));

    auto wrong_id_issuer = issuer();
    wrong_id_issuer.issuer_id = 22;
    CHECK_FALSE(hotstuff::verify_operator_capacity_label_envelope(
        valid, wrong_id_issuer, members(), epoch0(), topology_digest(), 1'500));
    auto wrong_reference_issuer = issuer();
    wrong_reference_issuer.issuer_reference = "different-issuer";
    CHECK_FALSE(hotstuff::verify_operator_capacity_label_envelope(
        valid, wrong_reference_issuer, members(), epoch0(), topology_digest(), 1'500));
    auto wrong_key_issuer = issuer();
    const auto other_private_key = [] {
        hotstuff::PrivKeySecp256k1 value;
        value.from_hex("5aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
        return value;
    }();
    wrong_key_issuer.public_key = hotstuff::PubKeySecp256k1(other_private_key);
    CHECK_FALSE(hotstuff::verify_operator_capacity_label_envelope(
        valid, wrong_key_issuer, members(), epoch0(), topology_digest(), 1'500));

    auto epoch1 = epoch0();
    epoch1.epoch_number = 1;
    auto forged_epoch1 = valid;
    forged_epoch1.epoch0 = epoch1;
    forged_epoch1.capacity_snapshot.predecessor = epoch1;
    forged_epoch1.capacity_snapshot.canonical_digest =
        hotstuff::operator_capacity_snapshot_digest(forged_epoch1.capacity_snapshot);
    forged_epoch1.baseline_topology_digest =
        hotstuff::operator_capacity_baseline_topology_digest(
            epoch1, members(), baseline_trees());
    forged_epoch1.signature.sign(
        hotstuff::operator_capacity_label_envelope_digest(forged_epoch1), key());
    auto forged_epoch1_issuer = issuer();
    forged_epoch1_issuer.approved_capacity_digest =
        forged_epoch1.capacity_snapshot.canonical_digest;
    CHECK_FALSE(hotstuff::verify_operator_capacity_label_envelope(
        forged_epoch1, forged_epoch1_issuer, members(), epoch1,
        forged_epoch1.baseline_topology_digest, 1'500));

    auto reordered_trees = baseline_trees();
    std::swap(reordered_trees[0], reordered_trees[1]);
    CHECK_FALSE(hotstuff::verify_operator_capacity_label_envelope(
        valid, issuer(), members(), epoch0(),
        hotstuff::operator_capacity_baseline_topology_digest(
            epoch0(), members(), reordered_trees), 1'500));
}

TEST_CASE("operator-capacity label envelope expiry is independent of live state",
          "[operator-capacity][label-envelope]")
{
    const auto valid = envelope();
    CHECK_FALSE(verifies(valid, 0));
    CHECK_FALSE(verifies(valid, 999));
    CHECK(verifies(valid, 1'000));
    CHECK(verifies(valid, 2'000));
    CHECK_FALSE(verifies(valid, 2'001));
}

TEST_CASE("operator-capacity label envelope wire is bounded and canonical",
          "[operator-capacity][label-envelope][wire]")
{
    const auto signed_envelope = envelope();
    const hotstuff::OperatorCapacityLabelEnvelopeWireLimits limits{32 * 1024};
    const auto wire = hotstuff::encode_operator_capacity_label_envelope(
        signed_envelope, limits);
    CHECK_THROWS_AS(hotstuff::encode_operator_capacity_label_envelope(
        signed_envelope, {63}), std::length_error);

    const auto decoded = hotstuff::decode_operator_capacity_label_envelope(wire, limits);
    REQUIRE(decoded);
    CHECK(hotstuff::encode_operator_capacity_label_envelope(*decoded.value, limits) == wire);

    auto trailing = wire;
    trailing.push_back(0);
    CHECK(hotstuff::decode_operator_capacity_label_envelope(trailing, limits).error ==
        hotstuff::OperatorCapacityLabelEnvelopeWireError::trailing_bytes);
    auto truncated = wire;
    truncated.pop_back();
    CHECK_FALSE(hotstuff::decode_operator_capacity_label_envelope(truncated, limits));
    auto malformed = wire;
    const auto first_member_offset =
        std::string{"kauri-operator-capacity-label-envelope-v1"}.size() +
        sizeof(std::uint32_t) * 2 + sizeof(std::uint32_t) +
        snapshot().issuer_reference.size();
    malformed[first_member_offset + sizeof(ReplicaID) - 1] = 1;
    CHECK_FALSE(hotstuff::decode_operator_capacity_label_envelope(malformed, limits));
}
