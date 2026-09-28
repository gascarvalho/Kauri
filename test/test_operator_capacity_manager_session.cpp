#include "catch.hpp"

#include <vector>

#include "hotstuff/operator_capacity_authorization.h"
#include "hotstuff/operator_capacity_policy.h"
#include "support/adaptive_v3_manager_session_fixture.h"

namespace
{
using namespace hotstuff;
using namespace kauri::test_support::cert13;

OperatorCapacityPolicyConfig capacity_policy_for(
    const Fixture &fixture, const AdaptationSnapshot &snapshot,
    OperatorCapacityArm arm)
{
    OperatorCapacityPolicyConfig policy;
    policy.arm = arm;
    policy.policy_version = "operator-capacity-v1";
    policy.decision_clock_domain = OperatorCapacityClockDomain::monotonic_raw_ns;
    policy.decision_monotonic_ns = 500;
    policy.expected_responsiveness_snapshot_id = snapshot.snapshot_id();
    policy.expected_evidence_cutoff = snapshot.evidence_cutoff();
    policy.fanout = fixture.initial.trees.front().fanout;
    policy.tree_count = static_cast<std::uint32_t>(fixture.initial.trees.size());
    policy.baseline_trees = fixture.initial.trees;
    policy.capacity_snapshot.issuer_reference = "manager-session-test";
    policy.capacity_snapshot.predecessor = snapshot.epoch();
    policy.capacity_snapshot.clock_domain =
        OperatorCapacityClockDomain::monotonic_raw_ns;
    policy.capacity_snapshot.valid_from_monotonic_ns = 1;
    policy.capacity_snapshot.valid_until_monotonic_ns = 1000;
    for (const auto replica : fixture.replicas)
    {
        policy.capacity_snapshot.labels.push_back({
            replica, replica < 6 ? OperatorCapacityClass::slow
                                 : OperatorCapacityClass::fast});
    }
    policy.capacity_snapshot.canonical_digest =
        operator_capacity_snapshot_digest(policy.capacity_snapshot);
    policy.approved_capacity_digest = policy.capacity_snapshot.canonical_digest;
    return policy;
}

OperatorCapacityIssuer test_capacity_issuer(
    const EpochDefinitionInput &initial, const std::vector<ReplicaID> &replicas)
{
    auto key = issuer_key();
    OperatorCapacitySnapshot snapshot;
    snapshot.issuer_reference = "manager-session-test";
    snapshot.predecessor = {initial.epoch_number, *initial.epoch_digest};
    snapshot.clock_domain = OperatorCapacityClockDomain::monotonic_raw_ns;
    snapshot.valid_from_monotonic_ns = 1;
    snapshot.valid_until_monotonic_ns = 1000;
    for (const auto replica : replicas)
        snapshot.labels.push_back({replica, replica < 6
            ? OperatorCapacityClass::slow : OperatorCapacityClass::fast});
    snapshot.canonical_digest = operator_capacity_snapshot_digest(snapshot);
    return {73, "manager-session-test", snapshot.canonical_digest,
            PubKeySecp256k1(key)};
}

EpochDefinitionInput n31_epoch_zero(const std::vector<ReplicaID> &replicas)
{
    EpochDefinitionInput input;
    input.schema_version = kEpochDefinitionSchemaVersionV2;
    input.epoch_number = 0;
    input.membership_digest = canonical_membership_digest(replicas);
    input.generation_seed = 0x53544154494331ULL;
    input.policy_version = "operator-capacity-baseline-v1";
    input.evidence_snapshot_id = "operator-capacity-baseline";
    for (std::uint32_t tree = 0; tree < 21; ++tree)
    {
        auto order = replicas;
        std::rotate(order.begin(), order.begin() + tree, order.end());
        input.trees.push_back({tree, 5, 2, std::move(order), {}});
    }
    input.epoch_digest = compute_epoch_digest(input);
    return input;
}

OperatorCapacityAuthorization capacity_authorization(
    const Fixture &fixture, const AdaptationSnapshot &snapshot,
    OperatorCapacityArm arm)
{
    auto key = issuer_key();
    auto policy = capacity_policy_for(fixture, snapshot, arm);
    return authorize_operator_capacity(fixture.replicas, policy, 73, key);
}

bool exact_trees(const std::vector<EpochTreeDefinition> &left,
                 const std::vector<EpochTreeDefinition> &right)
{
    if (left.size() != right.size()) return false;
    for (std::size_t index = 0; index < left.size(); ++index)
    {
        if (left[index].tree_id != right[index].tree_id ||
            left[index].fanout != right[index].fanout ||
            left[index].pipeline_stretch != right[index].pipeline_stretch ||
            left[index].members_breadth_first != right[index].members_breadth_first ||
            left[index].wait_exempt_leaves != right[index].wait_exempt_leaves)
            return false;
    }
    return true;
}
} // namespace

TEST_CASE("operator-capacity manager session freezes an all-live N31 snapshot before a verified v3 bundle",
          "[operator-capacity][n31][adaptive-v3][manager-session][integration]")
{
    const auto all_live = members(31);
    const auto initial = n31_epoch_zero(all_live);
    auto issuer = test_capacity_issuer(initial, all_live);
    auto raw_clock_now = std::make_shared<std::uint64_t>(500);
    Fixture multi_cycle_fixture(31, all_live, 2,
        BlsMembershipConstruction::random, 1, false, issuer, initial);
    REQUIRE_FALSE(multi_cycle_fixture.session.begin_operator_capacity_epoch1());
    Fixture fixture(31, all_live, 1, BlsMembershipConstruction::random, 1,
                    false, issuer, initial,
                    [raw_clock_now] { return *raw_clock_now; });

    REQUIRE(fixture.session.begin_operator_capacity_epoch1());
    REQUIRE(fixture.session.arm_hard_deadline(1'000'000));
    REQUIRE(fixture.session.evaluate() ==
            AdaptiveV2ManagerControllerStatus::awaiting_readiness);
    fixture.evidence.ready_all();
    std::uint64_t attempt = 1;
    for (const auto replica : all_live)
        for (std::uint32_t count = 0; count < 2; ++count)
            fixture.evidence.record_for_tree(
                replica, fixture.tree_with_reporter_in(replica, all_live),
                ResponseOutcome::on_time,
                "operator-capacity-baseline", attempt++);
    REQUIRE(fixture.session.evaluate() ==
            AdaptiveV2ManagerControllerStatus::baseline_frozen);
    const auto *snapshot = fixture.session.operator_capacity_baseline_snapshot();
    REQUIRE(snapshot != nullptr);
    REQUIRE(snapshot->ranking().size() == all_live.size());
    fixture.session.advance(500);

    SECTION("treatment ranks capacity-labelled fast members first")
    {
        auto authorization = capacity_authorization(
            fixture, *snapshot, OperatorCapacityArm::fast_priority_treatment);
        REQUIRE(fixture.session.authorize_operator_capacity_epoch1(authorization));
        const auto *bundle = fixture.session.successor_bundle();
        REQUIRE(bundle != nullptr);
        REQUIRE(bundle->definition().epoch_number == 1);
        REQUIRE_FALSE(bundle->definition().evidence_snapshot_id.empty());
        REQUIRE(bundle->definition().trees.size() == 21);
        for (std::uint32_t tree = 0; tree < 21; ++tree)
            CHECK(bundle->definition().trees[tree].members_breadth_first.front() ==
                  tree + 6);
        complete_readiness(fixture, all_live, 510, 1000, "capacity-treatment");
        CHECK(fixture.session.status() == AdaptiveV3ManagerSessionStatus::terminal);
        REQUIRE(fixture.session.terminal_records().size() == 1);
    }

    SECTION("sham preserves every predecessor tree exactly")
    {
        const auto authorization = capacity_authorization(
            fixture, *snapshot, OperatorCapacityArm::exact_copy_sham);
        REQUIRE(fixture.session.authorize_operator_capacity_epoch1(authorization));
        const auto *bundle = fixture.session.successor_bundle();
        REQUIRE(bundle != nullptr);
        REQUIRE(exact_trees(bundle->definition().trees, fixture.initial.trees));
        complete_readiness(fixture, all_live, 510, 1000, "capacity-sham");
        CHECK(fixture.session.status() == AdaptiveV3ManagerSessionStatus::terminal);
    }

    SECTION("a snapshot-bound authorization cannot be applied twice")
    {
        const auto authorization = capacity_authorization(
            fixture, *snapshot, OperatorCapacityArm::fast_priority_treatment);
        REQUIRE(fixture.session.authorize_operator_capacity_epoch1(authorization));
        REQUIRE_FALSE(fixture.session.authorize_operator_capacity_epoch1(authorization));
    }

    SECTION("a different key cannot impersonate the pinned capacity issuer")
    {
        auto attacker_key = issuer_key();
        attacker_key.from_hex(
            "1aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
        const auto policy = capacity_policy_for(
            fixture, *snapshot, OperatorCapacityArm::fast_priority_treatment);
        const auto attacker_authorization = authorize_operator_capacity(
            fixture.replicas, policy, 73, attacker_key);
        REQUIRE_FALSE(
            fixture.session.authorize_operator_capacity_epoch1(
                attacker_authorization));
        CHECK(fixture.session.successor_bundle() == nullptr);
        CHECK(fixture.session.status() ==
              AdaptiveV3ManagerSessionStatus::selecting);
    }

    SECTION("a signed decision cannot lie in the manager's raw-clock future")
    {
        auto policy = capacity_policy_for(
            fixture, *snapshot, OperatorCapacityArm::fast_priority_treatment);
        policy.decision_monotonic_ns = 501;
        const auto authorization = authorize_operator_capacity(
            fixture.replicas, policy, 73, issuer_key());
        REQUIRE_FALSE(
            fixture.session.authorize_operator_capacity_epoch1(authorization));
        CHECK(fixture.session.successor_bundle() == nullptr);
    }

    SECTION("the signed validity-window endpoint remains admissible")
    {
        *raw_clock_now = 1000;
        const auto authorization = capacity_authorization(
            fixture, *snapshot, OperatorCapacityArm::fast_priority_treatment);
        REQUIRE(fixture.session.authorize_operator_capacity_epoch1(authorization));
        REQUIRE(fixture.session.successor_bundle() != nullptr);
    }

    SECTION("an authorization is rejected once the trusted raw clock passes its window")
    {
        *raw_clock_now = 1001;
        const auto authorization = capacity_authorization(
            fixture, *snapshot, OperatorCapacityArm::fast_priority_treatment);
        REQUIRE_FALSE(fixture.session.authorize_operator_capacity_epoch1(authorization));
        CHECK(fixture.session.successor_bundle() == nullptr);
    }
}
