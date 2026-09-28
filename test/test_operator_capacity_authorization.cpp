#include "catch.hpp"

#include "hotstuff/operator_capacity_authorization.h"

namespace
{
using hotstuff::OperatorCapacityArm;
using hotstuff::OperatorCapacityAuthorization;
using hotstuff::OperatorCapacityClass;
using hotstuff::OperatorCapacityIssuer;
using hotstuff::OperatorCapacityLabel;
using hotstuff::OperatorCapacityPolicyConfig;
using hotstuff::OperatorCapacitySnapshot;
using hotstuff::ReplicaID;

std::vector<ReplicaID> members()
{
    std::vector<ReplicaID> value;
    for (ReplicaID id = 0; id < 31; ++id) value.push_back(id);
    return value;
}

hotstuff::PrivKeySecp256k1 key()
{
    hotstuff::PrivKeySecp256k1 value;
    value.from_hex("4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
    return value;
}

OperatorCapacityPolicyConfig policy()
{
    OperatorCapacityPolicyConfig value;
    value.arm = OperatorCapacityArm::fast_priority_treatment;
    value.policy_version = "n31-operator-capacity-v1";
    value.decision_monotonic_ns = 150;
    value.expected_responsiveness_snapshot_id = "all-responsive";
    value.expected_evidence_cutoff = 71;
    value.fanout = 5;
    value.tree_count = 21;
    OperatorCapacitySnapshot snapshot;
    snapshot.issuer_reference = "n31-capacity-issuer";
    snapshot.valid_from_monotonic_ns = 100;
    snapshot.valid_until_monotonic_ns = 200;
    for (const auto id : members())
        snapshot.labels.push_back({id, id < 6 ? OperatorCapacityClass::slow : OperatorCapacityClass::fast});
    snapshot.canonical_digest = hotstuff::operator_capacity_snapshot_digest(snapshot);
    value.approved_capacity_digest = snapshot.canonical_digest;
    value.capacity_snapshot = snapshot;
    for (std::uint32_t tree = 0; tree < 21; ++tree)
        value.baseline_trees.push_back({tree, 5, 2, members(), {}});
    return value;
}

OperatorCapacityIssuer issuer(const OperatorCapacityPolicyConfig &policy)
{
    const auto private_key = key();
    return {17, policy.capacity_snapshot.issuer_reference,
            policy.capacity_snapshot.canonical_digest,
            hotstuff::PubKeySecp256k1(private_key)};
}

OperatorCapacityAuthorization authorization(const OperatorCapacityPolicyConfig &policy)
{
    return hotstuff::authorize_operator_capacity(members(), policy, 17, key());
}
} // namespace

TEST_CASE("operator-capacity authorization authenticates the complete signed input",
          "[operator-capacity][authorization]")
{
    const auto input = policy();
    const auto signed_input = authorization(input);
    const auto verified = hotstuff::verify_operator_capacity_authorization(
        signed_input, issuer(input), members());
    REQUIRE(verified);
    CHECK(verified->issuer_id() == 17);
    CHECK(verified->membership() == members());
    CHECK(verified->capacity_digest() == input.capacity_snapshot.canonical_digest);
    REQUIRE(verified->policy().baseline_trees.size() == input.baseline_trees.size());
    CHECK(verified->policy().baseline_trees.front().tree_id ==
          input.baseline_trees.front().tree_id);
}

TEST_CASE("operator-capacity authorization rejects every bound field mutation",
          "[operator-capacity][authorization]")
{
    const auto input = policy();
    const auto trusted_issuer = issuer(input);
    const auto valid = authorization(input);
    const auto rejects = [&](const OperatorCapacityAuthorization &candidate) {
        CHECK_FALSE(hotstuff::verify_operator_capacity_authorization(
            candidate, trusted_issuer, members()));
    };

    auto changed = valid;
    changed.policy.arm = OperatorCapacityArm::exact_copy_sham;
    rejects(changed);
    changed = valid;
    changed.policy.capacity_snapshot.predecessor.epoch_number = 1;
    rejects(changed);
    changed = valid;
    changed.policy.capacity_snapshot.labels[0].capacity = OperatorCapacityClass::fast;
    rejects(changed);
    changed = valid;
    changed.policy.baseline_trees[0].members_breadth_first[0] = 30;
    rejects(changed);
    changed = valid;
    ++changed.policy.decision_monotonic_ns;
    rejects(changed);
    changed = valid;
    changed.membership.pop_back();
    rejects(changed);

    auto wrong_issuer = trusted_issuer;
    wrong_issuer.issuer_reference = "different-issuer";
    CHECK_FALSE(hotstuff::verify_operator_capacity_authorization(
        valid, wrong_issuer, members()));
}

TEST_CASE("operator-capacity authorization wire is bounded and canonical",
          "[operator-capacity][authorization][wire]")
{
    const auto input = policy();
    const auto signed_input = authorization(input);
    const hotstuff::OperatorCapacityAuthorizationWireLimits limits{128 * 1024};
    const auto wire = hotstuff::encode_operator_capacity_authorization(
        signed_input, limits);
    CHECK_THROWS_AS(hotstuff::encode_operator_capacity_authorization(
        signed_input, {63}), std::length_error);
    const auto decoded = hotstuff::decode_operator_capacity_authorization(wire, limits);
    REQUIRE(decoded);
    CHECK(hotstuff::encode_operator_capacity_authorization(*decoded.value, limits) == wire);

    auto trailing = wire;
    trailing.push_back(0);
    CHECK(hotstuff::decode_operator_capacity_authorization(trailing, limits).error ==
          hotstuff::OperatorCapacityAuthorizationWireError::trailing_bytes);
    auto truncated = wire;
    truncated.pop_back();
    CHECK_FALSE(hotstuff::decode_operator_capacity_authorization(truncated, limits));
    auto noncanonical = wire;
    // Membership begins after the fixed domain, schema, issuer, and count.
    const auto first_member_offset = std::string{"kauri-operator-capacity-authorization-v1"}.size() + 12;
    noncanonical[first_member_offset + sizeof(hotstuff::ReplicaID) - 1] = 1;
    CHECK_FALSE(hotstuff::decode_operator_capacity_authorization(noncanonical, limits));
}

TEST_CASE("operator-capacity snapshot transport is canonical and distinct from policy digest",
          "[operator-capacity][snapshot][wire]")
{
    const auto snapshot = policy().capacity_snapshot;
    const hotstuff::OperatorCapacitySnapshotWireLimits limits{16 * 1024};
    const auto wire = hotstuff::encode_operator_capacity_snapshot(snapshot, limits);
    const auto decoded = hotstuff::decode_operator_capacity_snapshot(wire, limits);
    REQUIRE(decoded);
    CHECK(decoded.value->canonical_digest ==
          hotstuff::operator_capacity_snapshot_digest(*decoded.value));
    CHECK(hotstuff::encode_operator_capacity_snapshot(*decoded.value, limits) == wire);

    auto trailing = wire;
    trailing.push_back(0);
    CHECK(hotstuff::decode_operator_capacity_snapshot(trailing, limits).error ==
          hotstuff::OperatorCapacitySnapshotWireError::trailing_bytes);
    auto unordered = snapshot;
    std::swap(unordered.labels[0], unordered.labels[1]);
    unordered.canonical_digest =
        hotstuff::operator_capacity_snapshot_digest(unordered);
    CHECK_THROWS(hotstuff::encode_operator_capacity_snapshot(unordered, limits));
}
