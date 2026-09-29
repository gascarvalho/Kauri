#include <algorithm>
#include <cstdint>
#include <string>
#include <vector>

#include "catch.hpp"
#include "hotstuff/adaptive_v2_epoch_factory.h"
#include "hotstuff/epoch_activation.h"
#include "hotstuff/operator_capacity_policy.h"

namespace
{

using hotstuff::AcceptedEvidenceRecord;
using hotstuff::AcceptedEvidenceView;
using hotstuff::AdaptationEpochId;
using hotstuff::AdaptationPolicy;
using hotstuff::EpochTreeDefinition;
using hotstuff::OperatorCapacityArm;
using hotstuff::OperatorCapacityClass;
using hotstuff::OperatorCapacityLabel;
using hotstuff::OperatorCapacityPlacementResult;
using hotstuff::OperatorCapacityPolicyConfig;
using hotstuff::OperatorCapacityPolicyStatus;
using hotstuff::OperatorCapacitySnapshot;
using hotstuff::ReplicaID;
using hotstuff::ResponseObservation;
using hotstuff::ResponseOutcome;

std::vector<ReplicaID> members()
{
    std::vector<ReplicaID> value(31);
    for (ReplicaID id = 0; id < value.size(); ++id) value[id] = id;
    return value;
}

hotstuff::uint256_t digest(const std::string &label)
{
    return hotstuff::DataStream(label).get_hash();
}

AdaptationEpochId epoch()
{
    return {8, digest("operator-capacity-predecessor")};
}

std::vector<AcceptedEvidenceRecord> evidence(const AdaptationEpochId &id)
{
    std::vector<AcceptedEvidenceRecord> records;
    for (const auto member : members())
    {
        ResponseObservation observation;
        observation.schema_version = hotstuff::kResponseObservationSchemaVersion;
        observation.reporter_id = member;
        observation.observed_replica_id = member;
        observation.configuration = {id.epoch_number, 0, id.epoch_digest};
        observation.block_hash = digest("operator-capacity-block-" + std::to_string(member));
        observation.expected_message_type = hotstuff::ExpectedMessageType::direct_vote;
        observation.outcome = ResponseOutcome::on_time;
        observation.response_duration_us = 10;
        observation.deadline_duration_us = 100;
        observation.reporter_monotonic_ns = member + 1;
        observation.reporter_sequence = member + 1;
        observation.signer_set = {member};
        observation.observation_id = hotstuff::compute_response_observation_id(
            observation.attempt_identity());
        records.push_back({static_cast<std::uint64_t>(member) + 1U,
                           std::move(observation)});
    }
    return records;
}

hotstuff::AdaptationSnapshot snapshot_with_timeout(bool timeout_for_zero = false)
{
    const auto id = epoch();
    const auto records = evidence(id);
    AdaptationPolicy policy;
    policy.minimum_attempts = 1;
    policy.minimum_response_rate_ppm = 0;
    policy.maximum_timeout_rate_ppm = hotstuff::kRatePpmScale;
    policy.trailing_timeout_streak = policy.attempt_window;
    auto adjusted = records;
    if (timeout_for_zero)
    {
        adjusted.front().observation.outcome = ResponseOutcome::timeout;
        adjusted.front().observation.response_duration_us = 0;
        adjusted.front().observation.signer_set.clear();
        policy.maximum_timeout_rate_ppm = 0;
    }
    return hotstuff::build_adaptation_snapshot(
        members(), id, {adjusted.data(), adjusted.size()}, adjusted.size(), policy, 71);
}

hotstuff::AdaptationSnapshot responsive_snapshot()
{
    return snapshot_with_timeout();
}

hotstuff::AdaptationSnapshot snapshot_for_epoch(
    const AdaptationEpochId &id, bool timeout_for_zero = false)
{
    auto records = evidence(id);
    AdaptationPolicy policy;
    policy.minimum_attempts = 1;
    policy.minimum_response_rate_ppm = 0;
    policy.maximum_timeout_rate_ppm = hotstuff::kRatePpmScale;
    policy.trailing_timeout_streak = policy.attempt_window;
    if (timeout_for_zero)
    {
        records.front().observation.outcome = ResponseOutcome::timeout;
        records.front().observation.response_duration_us = 0;
        records.front().observation.signer_set.clear();
        policy.maximum_timeout_rate_ppm = 0;
    }
    return hotstuff::build_adaptation_snapshot(
        members(), id, {records.data(), records.size()}, records.size(),
        policy, 71);
}

OperatorCapacitySnapshot capacity_snapshot()
{
    OperatorCapacitySnapshot snapshot;
    snapshot.issuer_reference = "n31-operator-capacity-fixture";
    snapshot.predecessor = epoch();
    snapshot.valid_from_monotonic_ns = 100;
    snapshot.valid_until_monotonic_ns = 200;
    for (const auto member : members())
        snapshot.labels.push_back({member,
            member < 6 ? OperatorCapacityClass::slow : OperatorCapacityClass::fast});
    snapshot.canonical_digest = hotstuff::operator_capacity_snapshot_digest(snapshot);
    return snapshot;
}

std::vector<EpochTreeDefinition> baseline();

OperatorCapacityPolicyConfig treatment()
{
    OperatorCapacityPolicyConfig config;
    config.arm = OperatorCapacityArm::fast_priority_treatment;
    config.policy_version = "n31-operator-capacity-v1";
    config.decision_monotonic_ns = 150;
    config.fanout = 5;
    config.tree_count = 21;
    config.capacity_snapshot = capacity_snapshot();
    const auto expected = responsive_snapshot();
    config.approved_capacity_digest = config.capacity_snapshot.canonical_digest;
    config.expected_responsiveness_snapshot_id = expected.snapshot_id();
    config.expected_evidence_cutoff = expected.evidence_cutoff();
    config.baseline_trees = baseline();
    return config;
}

std::vector<EpochTreeDefinition> baseline()
{
    std::vector<EpochTreeDefinition> trees;
    for (std::uint32_t tree = 0; tree < 21; ++tree)
    {
        auto order = members();
        std::rotate(order.begin(), order.begin() + tree, order.end());
        trees.push_back({tree, 5, 2, std::move(order), {}});
    }
    return trees;
}

hotstuff::EpochDefinitionInput baseline_epoch_zero()
{
    hotstuff::EpochDefinitionInput input;
    input.schema_version = hotstuff::kEpochDefinitionSchemaVersionV2;
    input.epoch_number = 0;
    input.membership_digest = hotstuff::canonical_membership_digest(members());
    input.trees = baseline();
    input.generation_seed = 0x53544154494331ULL;
    input.policy_version = "operator-capacity-baseline-v1";
    input.evidence_snapshot_id = "operator-capacity-baseline";
    return input;
}

OperatorCapacityPolicyConfig config_for_epoch(
    const hotstuff::EpochDefinition &current,
    const hotstuff::AdaptationSnapshot &responsive)
{
    auto config = treatment();
    config.capacity_snapshot.predecessor =
        {current.epoch_number(), current.epoch_digest()};
    config.capacity_snapshot.canonical_digest =
        hotstuff::operator_capacity_snapshot_digest(config.capacity_snapshot);
    config.approved_capacity_digest = config.capacity_snapshot.canonical_digest;
    config.expected_responsiveness_snapshot_id = responsive.snapshot_id();
    config.expected_evidence_cutoff = responsive.evidence_cutoff();
    config.baseline_trees = current.trees();
    return config;
}

hotstuff::PrivKeySecp256k1 issuer_key()
{
    hotstuff::PrivKeySecp256k1 key;
    key.from_hex(
        "4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
    return key;
}

hotstuff::EpochChangeBundleLimits bundle_limits()
{
    return {128 * 1024, 4096,
            hotstuff::EpochWireLimits{96 * 1024, 32, 64, 128, 31}};
}

bool same_ordered_trees(const std::vector<EpochTreeDefinition> &left,
                        const std::vector<EpochTreeDefinition> &right)
{
    if (left.size() != right.size()) return false;
    for (std::size_t index = 0; index < left.size(); ++index)
    {
        const auto &a = left[index];
        const auto &b = right[index];
        if (a.tree_id != b.tree_id || a.fanout != b.fanout ||
            a.pipeline_stretch != b.pipeline_stretch ||
            a.members_breadth_first != b.members_breadth_first ||
            a.wait_exempt_leaves != b.wait_exempt_leaves)
            return false;
    }
    return true;
}

TEST_CASE("N31 operator-capacity treatment selects fast all-live roots deterministically",
          "[operator-capacity][n31][policy]")
{
    const auto snapshot = responsive_snapshot();
    const auto config = treatment();
    const auto first = hotstuff::build_operator_capacity_placement(members(), snapshot, config);
    const auto second = hotstuff::build_operator_capacity_placement(members(), snapshot, config);

    REQUIRE(first);
    REQUIRE(second);
    CHECK(first.policy_snapshot_id == second.policy_snapshot_id);
    CHECK(same_ordered_trees(first.trees, second.trees));
    REQUIRE(first.trees.size() == 21);
    for (std::uint32_t tree = 0; tree < first.trees.size(); ++tree)
    {
        CHECK(first.trees[tree].tree_id == config.baseline_trees[tree].tree_id);
        CHECK(first.trees[tree].fanout == config.baseline_trees[tree].fanout);
        CHECK(first.trees[tree].pipeline_stretch ==
              config.baseline_trees[tree].pipeline_stretch);
        CHECK(first.trees[tree].wait_exempt_leaves ==
              config.baseline_trees[tree].wait_exempt_leaves);
        CHECK(first.trees[tree].members_breadth_first.front() == tree + 6);
        CHECK(first.trees[tree].members_breadth_first.front() >= 6);
        for (std::size_t position = 0; position < 6; ++position)
            CHECK(first.trees[tree].members_breadth_first[position] >= 6);
    }
}

TEST_CASE("N31 operator-capacity sham exact-copies predecessor at the native decision point",
          "[operator-capacity][n31][sham]")
{
    const auto snapshot = responsive_snapshot();
    auto config = treatment();
    config.arm = OperatorCapacityArm::exact_copy_sham;
    config.baseline_trees = baseline();
    const auto sham = hotstuff::build_operator_capacity_placement(members(), snapshot, config);

    REQUIRE(sham);
    CHECK(same_ordered_trees(sham.trees, config.baseline_trees));
    CHECK(sham.policy_snapshot_id != "");
}

TEST_CASE("N31 capacity placement preserves either uniform tree shape and rejects a mixed predecessor",
          "[operator-capacity][n31][shape]")
{
    const auto snapshot = responsive_snapshot();
    for (const std::uint32_t fanout : {2U, 5U})
    {
        auto config = treatment();
        config.fanout = fanout;
        for (auto &tree : config.baseline_trees)
            tree.fanout = fanout;

        const auto selected = hotstuff::build_operator_capacity_placement(
            members(), snapshot, config);
        REQUIRE(selected);
        REQUIRE(selected.trees.size() == config.baseline_trees.size());
        for (const auto &tree : selected.trees)
        {
            CHECK(tree.fanout == fanout);
            CHECK(tree.pipeline_stretch == 2);
            CHECK(tree.members_breadth_first.front() >= 6);
        }

        config.arm = OperatorCapacityArm::exact_copy_sham;
        const auto sham = hotstuff::build_operator_capacity_placement(
            members(), snapshot, config);
        REQUIRE(sham);
        CHECK(same_ordered_trees(sham.trees, config.baseline_trees));
    }

    auto mixed = treatment();
    mixed.baseline_trees.front().fanout = 2;
    CHECK(hotstuff::build_operator_capacity_placement(
              members(), snapshot, mixed).status ==
          OperatorCapacityPolicyStatus::invalid_exact_copy_baseline);
}

TEST_CASE("N31 operator-capacity policy fails closed on invalid binding or nonresponsive input",
          "[operator-capacity][n31][binding]")
{
    const auto snapshot = responsive_snapshot();
    auto config = treatment();
    config.capacity_snapshot.labels[0].capacity = OperatorCapacityClass::fast;
    CHECK(hotstuff::build_operator_capacity_placement(members(), snapshot, config).status ==
          OperatorCapacityPolicyStatus::invalid_capacity_snapshot);

    config = treatment();
    config.capacity_snapshot.predecessor.epoch_number++;
    config.capacity_snapshot.canonical_digest =
        hotstuff::operator_capacity_snapshot_digest(config.capacity_snapshot);
    config.approved_capacity_digest = config.capacity_snapshot.canonical_digest;
    CHECK(hotstuff::build_operator_capacity_placement(members(), snapshot, config).status ==
          OperatorCapacityPolicyStatus::snapshot_epoch_mismatch);

    config = treatment();
    auto altered = config.capacity_snapshot;
    altered.labels[0].capacity = OperatorCapacityClass::fast;
    altered.canonical_digest = hotstuff::operator_capacity_snapshot_digest(altered);
    config.capacity_snapshot = altered;
    CHECK(hotstuff::build_operator_capacity_placement(
              members(), snapshot, config).status ==
          OperatorCapacityPolicyStatus::invalid_capacity_snapshot);

    const auto timeout_snapshot = snapshot_with_timeout(true);
    config = treatment();
    CHECK(hotstuff::build_operator_capacity_placement(
              members(), timeout_snapshot, config).status ==
          OperatorCapacityPolicyStatus::responsiveness_snapshot_mismatch);
    config.expected_responsiveness_snapshot_id = timeout_snapshot.snapshot_id();
    config.expected_evidence_cutoff = timeout_snapshot.evidence_cutoff();
    CHECK(hotstuff::build_operator_capacity_placement(
              members(), timeout_snapshot, config).status ==
          OperatorCapacityPolicyStatus::not_all_responsive);

    config = treatment();
    ++config.expected_evidence_cutoff;
    CHECK(hotstuff::build_operator_capacity_placement(
              members(), snapshot, config).status ==
          OperatorCapacityPolicyStatus::responsiveness_snapshot_mismatch);

    config = treatment();
    for (auto &label : config.capacity_snapshot.labels)
        label.capacity = OperatorCapacityClass::slow;
    config.capacity_snapshot.canonical_digest =
        hotstuff::operator_capacity_snapshot_digest(config.capacity_snapshot);
    config.approved_capacity_digest = config.capacity_snapshot.canonical_digest;
    CHECK(hotstuff::build_operator_capacity_placement(members(), snapshot, config).status ==
          OperatorCapacityPolicyStatus::insufficient_fast_members);
}

TEST_CASE("operator-capacity policy rejects membership beyond the tree-policy bound",
          "[operator-capacity][bound]")
{
    std::vector<ReplicaID> oversized;
    oversized.reserve(hotstuff::kMaximumTreePolicyMembers + 1);
    for (std::size_t index = 0; index <= hotstuff::kMaximumTreePolicyMembers; ++index)
        oversized.push_back(static_cast<ReplicaID>(index));

    CHECK(hotstuff::build_operator_capacity_placement(
              oversized, responsive_snapshot(), treatment()).status ==
          OperatorCapacityPolicyStatus::invalid_config);
}

TEST_CASE("N31 operator-capacity factory signs treatment and exact-copy sham without exempt leaves",
          "[operator-capacity][n31][factory]")
{
    hotstuff::EpochStore store(members());
    const auto &current = store.stage(
        baseline_epoch_zero(), hotstuff::EpochValidationContext{0, 0, {}});
    const auto responsive = snapshot_for_epoch(
        {current.epoch_number(), current.epoch_digest()});
    auto config = config_for_epoch(current, responsive);
    const auto key = issuer_key();
    const auto limits = bundle_limits();

    const auto selected = hotstuff::build_operator_capacity_placement(
        members(), responsive, config);
    REQUIRE(selected);
    const auto treatment_bundle = hotstuff::build_operator_capacity_epoch1_bundle(
        current, members(), responsive, config, 5, 17, key, limits);
    REQUIRE(treatment_bundle);
    const auto &treatment_definition = treatment_bundle.bundle->definition();
    CHECK(treatment_bundle.bundle->protocol_mode() ==
          hotstuff::EpochProtocolMode::adaptive_v3);
    CHECK(treatment_definition.epoch_number == 1);
    CHECK(treatment_definition.previous_epoch_digest == current.epoch_digest());
    CHECK(treatment_definition.membership_digest == current.membership_digest());
    CHECK(treatment_definition.evidence_snapshot_id == selected.policy_snapshot_id);
    CHECK(treatment_definition.evidence_cutoff == responsive.evidence_cutoff());
    REQUIRE(treatment_definition.trees.size() == 21);
    for (std::size_t index = 0; index < treatment_definition.trees.size(); ++index)
    {
        const auto &tree = treatment_definition.trees[index];
        CHECK(tree.wait_exempt_leaves.empty());
        CHECK(tree.fanout == current.trees()[index].fanout);
        CHECK(tree.pipeline_stretch == current.trees()[index].pipeline_stretch);
        for (std::size_t position = 0; position < 6; ++position)
            CHECK(tree.members_breadth_first[position] >= 6);
    }
    CHECK(hotstuff::verify_epoch_change_signature(
        treatment_bundle.bundle->command(),
        hotstuff::EpochChangeIssuer{17, hotstuff::PubKeySecp256k1(key)}));
    CHECK(hotstuff::decode_adaptive_v3_epoch_change_bundle(
        treatment_bundle.bundle->canonical_bytes(), limits));

    config.arm = OperatorCapacityArm::exact_copy_sham;
    const auto sham_selection = hotstuff::build_operator_capacity_placement(
        members(), responsive, config);
    REQUIRE(sham_selection);
    const auto sham_bundle = hotstuff::build_operator_capacity_epoch1_bundle(
        current, members(), responsive, config, 5, 17, key, limits);
    REQUIRE(sham_bundle);
    CHECK(same_ordered_trees(sham_bundle.bundle->definition().trees,
                             current.trees()));
    CHECK(sham_bundle.bundle->definition().evidence_snapshot_id ==
          sham_selection.policy_snapshot_id);
    CHECK(sham_bundle.bundle->definition().epoch_digest !=
          treatment_definition.epoch_digest);
}

TEST_CASE("N31 operator-capacity factory fails closed on unauthorised or mismatched input",
          "[operator-capacity][n31][factory]")
{
    hotstuff::EpochStore store(members());
    const auto &current = store.stage(
        baseline_epoch_zero(), hotstuff::EpochValidationContext{0, 0, {}});
    const auto responsive = snapshot_for_epoch(
        {current.epoch_number(), current.epoch_digest()});
    auto config = config_for_epoch(current, responsive);
    const auto key = issuer_key();
    const auto limits = bundle_limits();
    const auto build = [&](const auto &snapshot,
                           const OperatorCapacityPolicyConfig &policy,
                           std::uint64_t delay = 5) {
        return hotstuff::build_operator_capacity_epoch1_bundle(
            current, members(), snapshot, policy, delay, 17, key, limits);
    };

    config.approved_capacity_digest = {};
    CHECK_FALSE(build(responsive, config));

    config = config_for_epoch(current, responsive);
    ++config.expected_evidence_cutoff;
    CHECK_FALSE(build(responsive, config));

    config = config_for_epoch(current, responsive);
    config.baseline_trees[0].members_breadth_first[1] = 30;
    CHECK_FALSE(build(responsive, config));

    config = config_for_epoch(current, responsive);
    CHECK_FALSE(build(responsive, config, 0));

    const auto timed_out = snapshot_for_epoch(
        {current.epoch_number(), current.epoch_digest()}, true);
    config = config_for_epoch(current, timed_out);
    CHECK_FALSE(build(timed_out, config));

    const AdaptationEpochId foreign_epoch{
        current.epoch_number(), digest("foreign-capacity-predecessor")};
    const auto foreign_snapshot = snapshot_for_epoch(foreign_epoch);
    config = config_for_epoch(current, foreign_snapshot);
    config.capacity_snapshot.predecessor = foreign_epoch;
    config.capacity_snapshot.canonical_digest =
        hotstuff::operator_capacity_snapshot_digest(config.capacity_snapshot);
    config.approved_capacity_digest = config.capacity_snapshot.canonical_digest;
    CHECK_FALSE(build(foreign_snapshot, config));
}

} // namespace
