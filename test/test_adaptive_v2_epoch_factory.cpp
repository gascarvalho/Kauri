#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <set>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/adaptive_v2_epoch_factory.h"

namespace
{

using hotstuff::AcceptedEvidenceRecord;
using hotstuff::AcceptedEvidenceView;
using hotstuff::AdaptationEpochId;
using hotstuff::AdaptationPolicy;
using hotstuff::AdaptiveV2CandidateAudit;
using hotstuff::AdaptiveV2EpochFactoryResult;
using hotstuff::AdaptiveV2EpochFactoryStatus;
using hotstuff::AdaptiveV2SelectionResult;
using hotstuff::AdaptiveV2SelectionStatus;
using hotstuff::ConfigurationId;
using hotstuff::EpochChangeBundleLimits;
using hotstuff::EpochChangeIssuer;
using hotstuff::EpochDefinition;
using hotstuff::EpochDefinitionInput;
using hotstuff::EpochStore;
using hotstuff::EpochTreeDefinition;
using hotstuff::EpochValidationContext;
using hotstuff::EpochWireLimits;
using hotstuff::ExpectedMessageType;
using hotstuff::PrivKeySecp256k1;
using hotstuff::ReplicaID;
using hotstuff::ResponseObservation;
using hotstuff::ResponseOutcome;
using hotstuff::ResponsivenessClass;
using hotstuff::TreePlacementInput;
using hotstuff::TreeShape;
using hotstuff::uint256_t;

constexpr std::uint32_t kIssuerId = 17;
constexpr std::uint64_t kPlacementSeed = 0xA2F7;

using FactoryBundlePointer =
    decltype(std::declval<AdaptiveV2EpochFactoryResult>().bundle);
static_assert(
    std::is_const<typename FactoryBundlePointer::element_type>::value,
    "the factory must expose only an immutable bundle");
static_assert(noexcept(hotstuff::build_adaptive_v2_successor_bundle(
    std::declval<const EpochDefinition &>(),
    std::declval<const AdaptiveV2SelectionResult &>(),
    std::declval<const TreePlacementInput &>(),
    std::uint64_t{},
    std::uint32_t{},
    std::declval<const PrivKeySecp256k1 &>(),
    std::declval<const EpochChangeBundleLimits &>())));

uint256_t digest(const std::string &label)
{
    return hotstuff::DataStream(label).get_hash();
}

std::vector<ReplicaID> membership()
{
    return {0, 1, 2, 3, 4, 5, 6};
}

EpochTreeDefinition tree(
    std::uint32_t tree_id,
    std::vector<ReplicaID> members)
{
    return {tree_id, 2, 2, std::move(members), {}};
}

EpochDefinitionInput epoch_zero_input()
{
    EpochDefinitionInput input;
    input.schema_version = hotstuff::kEpochDefinitionSchemaVersionV2;
    input.epoch_number = 0;
    input.membership_digest =
        hotstuff::canonical_membership_digest(membership());
    input.trees = {tree(0, membership())};
    input.activation_height = 0;
    input.generation_seed = 11;
    input.policy_version = "adaptive-v2-baseline";
    input.evidence_snapshot_id = "baseline-snapshot";
    input.evidence_cutoff = 7;
    return input;
}

PrivKeySecp256k1 private_key()
{
    PrivKeySecp256k1 key;
    key.from_hex(
        "4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
    return key;
}

EpochChangeBundleLimits bundle_limits()
{
    return {
        64 * 1024,
        4096,
        EpochWireLimits{32 * 1024, 8, 8, 128, 2}};
}

TreePlacementInput placement_input()
{
    return {
        membership(),
        TreeShape{2, 2, 5},
        kPlacementSeed,
        "adaptive-v2-performance-optimization-v1"};
}

std::size_t first_leaf_index(
    std::size_t member_count,
    std::uint32_t fanout)
{
    REQUIRE(fanout != 0);
    return member_count == 1
               ? 0
               : ((member_count - 2) / fanout) + 1;
}

AdaptiveV2SelectionResult successful_selection(
    const EpochDefinition &current)
{
    const auto members = membership();
    const AdaptationEpochId epoch{
        current.epoch_number(), current.epoch_digest()};
    std::vector<AcceptedEvidenceRecord> records;
    std::uint64_t sequence = 0;
    for (const auto target : members)
    {
        for (std::uint32_t attempt = 0; attempt < 2; ++attempt)
        {
            ResponseObservation observation;
            observation.reporter_id = 6;
            observation.observed_replica_id = target;
            observation.configuration = ConfigurationId{
                epoch.epoch_number, 0, epoch.epoch_digest};
            observation.block_hash = digest(
                "factory-attempt-" + std::to_string(target) + "-" +
                std::to_string(attempt));
            observation.expected_message_type =
                ExpectedMessageType::aggregate_relay;
            observation.deadline_duration_us = 100;
            observation.reporter_sequence = sequence + 1;
            observation.reporter_monotonic_ns = (sequence + 1) * 1'000;
            if (target < 2)
            {
                observation.outcome = ResponseOutcome::timeout;
                observation.response_duration_us = 0;
            }
            else
            {
                observation.outcome = ResponseOutcome::on_time;
                observation.response_duration_us = 20 + target;
                observation.signer_set = {target};
            }
            observation.observation_id =
                hotstuff::compute_response_observation_id(
                    observation.attempt_identity());
            records.push_back({++sequence, std::move(observation)});
        }
    }

    AdaptationPolicy policy;
    policy.policy_version = "adaptive-v2-factory-snapshot-v1";
    policy.attempt_window = 8;
    policy.minimum_attempts = 2;
    policy.minimum_response_rate_ppm = 750'000;
    policy.maximum_timeout_rate_ppm = 250'000;
    policy.trailing_timeout_streak = 2;
    policy.latency_percentile_basis_points = 5'000;
    auto snapshot = hotstuff::build_adaptation_snapshot(
        members,
        epoch,
        AcceptedEvidenceView{records.data(), records.size()},
        sequence,
        policy,
        kPlacementSeed);

    AdaptiveV2SelectionResult result;
    result.status = AdaptiveV2SelectionStatus::selected;
    result.metadata = {
        7,
        2,
        5,
        2,
        3,
        2,
        2,
        7,
        sequence};
    result.snapshot =
        std::make_unique<hotstuff::AdaptationSnapshot>(
            std::move(snapshot));
    for (const auto target : {ReplicaID{0}, ReplicaID{1}})
    {
        AdaptiveV2CandidateAudit audit;
        audit.replica_id = target;
        audit.snapshot_classification =
            ResponsivenessClass::nonresponsive;
        audit.baseline_score = 1;
        audit.current_score = -5;
        audit.baseline_score_delta = -6;
        audit.total_uncompensated_timeouts = 6;
        audit.qualifying_reporters = {2, 3, 4};
        audit.snapshot_nonresponsive = true;
        audit.score_drop_satisfied = true;
        audit.reporter_guard_satisfied = true;
        audit.guarded_eligible = true;
        result.eligible_candidates.push_back(std::move(audit));
    }
    result.selected_replicas = {0, 1};
    result.eligible_roots = {2, 3, 4, 5, 6};
    return result;
}

struct Fixture
{
    std::vector<ReplicaID> members{membership()};
    EpochStore store{members};
    const EpochDefinition *current{nullptr};
    AdaptiveV2SelectionResult selection;
    TreePlacementInput placement{placement_input()};
    PrivKeySecp256k1 key{private_key()};
    EpochChangeBundleLimits limits{bundle_limits()};

    Fixture()
    {
        current = &store.stage(
            epoch_zero_input(), EpochValidationContext{});
        selection = successful_selection(*current);
    }

    AdaptiveV2EpochFactoryResult build(
        std::uint64_t delay = 5) const
    {
        return hotstuff::build_adaptive_v2_successor_bundle(
            *current,
            selection,
            placement,
            delay,
            kIssuerId,
            key,
            limits);
    }
};

} // namespace

TEST_CASE(
    "N7 factory signs one optimized successor with Q roots and f leaves",
    "[adaptive-v2][epoch-factory][n7][success]")
{
    Fixture fixture;
    const auto result = fixture.build();

    REQUIRE(result);
    REQUIRE(result.bundle != nullptr);
    const auto &bundle = *result.bundle;
    const auto &definition = bundle.definition();
    CHECK(definition.schema_version ==
          hotstuff::kEpochDefinitionSchemaVersionV2);
    CHECK(definition.epoch_number == 1);
    CHECK(definition.previous_epoch_digest ==
          fixture.current->epoch_digest());
    CHECK(definition.membership_digest ==
          fixture.current->membership_digest());
    CHECK(definition.activation_height == 0);
    CHECK(definition.generation_seed == kPlacementSeed);
    CHECK(definition.policy_version ==
          fixture.placement.policy_version);
    CHECK(definition.evidence_snapshot_id ==
          fixture.selection.snapshot->snapshot_id());
    CHECK(definition.evidence_cutoff ==
          fixture.selection.snapshot->evidence_cutoff());
    REQUIRE(definition.epoch_digest.has_value());
    CHECK(*definition.epoch_digest ==
          hotstuff::compute_epoch_digest(definition));

    REQUIRE(definition.trees.size() == 5);
    const auto leaf_start = first_leaf_index(7, 2);
    for (std::size_t index = 0; index < definition.trees.size(); ++index)
    {
        const auto &candidate = definition.trees[index];
        REQUIRE_FALSE(candidate.members_breadth_first.empty());
        CHECK(candidate.tree_id == index);
        CHECK(candidate.members_breadth_first.front() ==
              fixture.selection.eligible_roots[index]);
        CHECK(candidate.wait_exempt_leaves ==
              std::vector<ReplicaID>{0, 1});
        auto canonical_members = candidate.members_breadth_first;
        std::sort(canonical_members.begin(), canonical_members.end());
        CHECK(canonical_members == fixture.members);
        for (const auto selected : fixture.selection.selected_replicas)
        {
            const auto position = std::find(
                candidate.members_breadth_first.begin(),
                candidate.members_breadth_first.end(),
                selected);
            REQUIRE(position != candidate.members_breadth_first.end());
            CHECK(static_cast<std::size_t>(std::distance(
                      candidate.members_breadth_first.begin(), position)) >=
                  leaf_start);
        }
    }

    const auto &command = bundle.command();
    CHECK(command.payload.successor_epoch_number == 1);
    CHECK(command.payload.predecessor_epoch_digest ==
          fixture.current->epoch_digest());
    CHECK(command.payload.successor_epoch_digest ==
          *definition.epoch_digest);
    CHECK(command.payload.activation_delay_blocks == 5);
    CHECK(command.issuer_id == kIssuerId);
    CHECK(hotstuff::verify_epoch_change_signature(
        command,
        EpochChangeIssuer{
            kIssuerId, hotstuff::PubKeySecp256k1(fixture.key)}));
}

TEST_CASE(
    "factory bundle round trips without changing membership or identity",
    "[adaptive-v2][epoch-factory][bundle][roundtrip]")
{
    Fixture fixture;
    const auto built = fixture.build();
    REQUIRE(built);

    const auto decoded =
        hotstuff::decode_adaptive_v2_epoch_change_bundle(
            built.bundle->canonical_bytes(), fixture.limits);
    REQUIRE(decoded);
    CHECK(decoded.value->canonical_bytes() ==
          built.bundle->canonical_bytes());
    CHECK(decoded.value->definition().membership_digest ==
          fixture.current->membership_digest());
    CHECK(decoded.value->command().payload ==
          built.bundle->command().payload);
}

TEST_CASE(
    "factory rejects selection membership root tree leaf and delay drift",
    "[adaptive-v2][epoch-factory][negative]")
{
    Fixture fixture;

    SECTION("selection must be successful")
    {
        fixture.selection.status =
            AdaptiveV2SelectionStatus::insufficient_guarded_candidates;
        const auto result = fixture.build();
        CHECK(result.status ==
              AdaptiveV2EpochFactoryStatus::invalid_selection);
        CHECK(result.bundle == nullptr);
    }

    SECTION("selection must contain exactly f targets")
    {
        fixture.selection.selected_replicas.pop_back();
        const auto result = fixture.build();
        CHECK(result.status ==
              AdaptiveV2EpochFactoryStatus::invalid_selection);
        CHECK(result.bundle == nullptr);
    }

    SECTION("membership cannot change")
    {
        fixture.placement.membership.back() = 7;
        const auto result = fixture.build();
        CHECK(result.status ==
              AdaptiveV2EpochFactoryStatus::membership_mismatch);
        CHECK(result.bundle == nullptr);
    }

    SECTION("roots must match the snapshot ranking order")
    {
        std::swap(
            fixture.selection.eligible_roots[0],
            fixture.selection.eligible_roots[1]);
        const auto result = fixture.build();
        CHECK(result.status ==
              AdaptiveV2EpochFactoryStatus::root_mismatch);
        CHECK(result.bundle == nullptr);
    }

    SECTION("tree count must equal Q")
    {
        fixture.placement.shape.tree_count = 4;
        const auto result = fixture.build();
        CHECK(result.status ==
              AdaptiveV2EpochFactoryStatus::tree_count_mismatch);
        CHECK(result.bundle == nullptr);
    }

    SECTION("every selected target must fit in a leaf")
    {
        fixture.placement.shape.fanout = 1;
        const auto result = fixture.build();
        CHECK(result.status ==
              AdaptiveV2EpochFactoryStatus::insufficient_leaf_capacity);
        CHECK(result.bundle == nullptr);
    }

    SECTION("activation delay must be positive")
    {
        const auto result = fixture.build(0);
        CHECK(result.status ==
              AdaptiveV2EpochFactoryStatus::invalid_activation_delay);
        CHECK(result.bundle == nullptr);
    }

    SECTION("bundle limits must cover the immutable definition")
    {
        fixture.limits.definition_limits.maximum_trees = 4;
        const auto result = fixture.build();
        CHECK(result.status ==
              AdaptiveV2EpochFactoryStatus::capacity_exceeded);
        CHECK(result.bundle == nullptr);
    }
}
