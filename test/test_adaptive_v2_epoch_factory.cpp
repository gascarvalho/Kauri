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
#include "hotstuff/adaptive_v2_manager_controller.h"
#include "hotstuff/epoch_activation.h"

namespace
{

using hotstuff::AcceptedEvidenceRecord;
using hotstuff::AcceptedEvidenceView;
using hotstuff::AdaptationEpochId;
using hotstuff::AdaptationPolicy;
using hotstuff::AdaptiveV2ManagerControllerConfig;
using hotstuff::AdaptiveV2CandidateAudit;
using hotstuff::AdaptiveV2EpochFactoryResult;
using hotstuff::AdaptiveV2EpochFactoryStatus;
using hotstuff::AdaptiveV2SelectionResult;
using hotstuff::AdaptiveV2SelectionConstraintBasis;
using hotstuff::AdaptiveV2SelectionStatus;
using hotstuff::ConfigurationId;
using hotstuff::BaselineRoot;
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
using hotstuff::TreePolicyKind;
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
    std::vector<ReplicaID> members,
    std::vector<ReplicaID> wait_exempt = {})
{
    return {
        tree_id,
        2,
        2,
        std::move(members),
        std::move(wait_exempt)};
}

EpochDefinitionInput epoch_zero_input()
{
    EpochDefinitionInput input;
    input.schema_version = hotstuff::kEpochDefinitionSchemaVersionV2;
    input.epoch_number = 0;
    input.membership_digest =
        hotstuff::canonical_membership_digest(membership());
    input.trees = {
        tree(0, {0, 1, 2, 3, 4, 5, 6}),
        tree(1, {1, 2, 3, 4, 5, 6, 0}),
        tree(2, {2, 3, 4, 5, 6, 0, 1}),
        tree(3, {3, 4, 5, 6, 0, 1, 2}),
        tree(4, {4, 5, 6, 0, 1, 2, 3})};
    input.activation_height = 0;
    input.generation_seed = 11;
    input.policy_version = "adaptive-v2-baseline";
    input.evidence_snapshot_id = "baseline-snapshot";
    input.evidence_cutoff = 7;
    return input;
}

EpochDefinitionInput inherited_epoch_input()
{
    auto input = epoch_zero_input();
    input.trees = {
        tree(0, {2, 3, 4, 5, 6, 0, 1}, {0, 1}),
        tree(1, {3, 4, 5, 6, 2, 0, 1}, {0, 1}),
        tree(2, {4, 5, 6, 2, 3, 0, 1}, {0, 1}),
        tree(3, {5, 6, 2, 3, 4, 0, 1}, {0, 1}),
        tree(4, {6, 2, 3, 4, 5, 0, 1}, {0, 1})};
    input.policy_version = "adaptive-v2-contained-e1";
    input.evidence_snapshot_id = "contained-e1-snapshot";
    input.evidence_cutoff = 5;
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
        audit.current_score = 25;
        audit.baseline_score_delta = 24;
        audit.guard_drawdown = -6;
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

AdaptiveV2SelectionResult inherited_selection(
    const EpochDefinition &current,
    bool selected_replicas_recovered = false)
{
    const AdaptationEpochId epoch{
        current.epoch_number(), current.epoch_digest()};
    std::vector<AcceptedEvidenceRecord> records;
    const auto targets = selected_replicas_recovered
                             ? membership()
                             : std::vector<ReplicaID>{2, 3, 4, 5, 6};
    std::uint64_t sequence = 5;
    for (const auto target : targets)
    {
        ResponseObservation observation;
        observation.reporter_id = target == 6 ? ReplicaID{2}
                                               : ReplicaID{6};
        observation.observed_replica_id = target;
        observation.configuration = ConfigurationId{
            epoch.epoch_number, 0, epoch.epoch_digest};
        observation.block_hash = digest(
            "inherited-factory-attempt-" + std::to_string(target));
        observation.expected_message_type =
            ExpectedMessageType::aggregate_relay;
        observation.outcome = ResponseOutcome::on_time;
        observation.response_duration_us =
            target < 2 ? target + 1U : (7U - target) * 10U;
        observation.deadline_duration_us = 100;
        observation.reporter_sequence = sequence + 1U;
        observation.reporter_monotonic_ns = (sequence + 1U) * 1'000;
        observation.signer_set = {target};
        observation.observation_id =
            hotstuff::compute_response_observation_id(
                observation.attempt_identity());
        records.push_back({++sequence, std::move(observation)});
    }

    AdaptationPolicy policy;
    policy.policy_version = "adaptive-v2-inherited-snapshot-v1";
    policy.attempt_window = 8;
    policy.minimum_attempts = 1;
    policy.minimum_response_rate_ppm = 750'000;
    policy.maximum_timeout_rate_ppm = 250'000;
    policy.trailing_timeout_streak = 2;
    policy.latency_percentile_basis_points = 5'000;
    auto snapshot = hotstuff::build_adaptation_snapshot(
        membership(),
        epoch,
        AcceptedEvidenceView{records.data(), records.size()},
        sequence,
        policy,
        kPlacementSeed);

    AdaptiveV2SelectionResult result;
    result.status = AdaptiveV2SelectionStatus::selected;
    result.constraint_basis = AdaptiveV2SelectionConstraintBasis::
        inherited_consensus_wait_exempt;
    result.metadata = {7, 2, 5, 2, 3, 1, 1, 5, sequence};
    result.snapshot =
        std::make_unique<hotstuff::AdaptationSnapshot>(
            std::move(snapshot));
    result.selected_replicas = {0, 1};
    result.eligible_roots = {6, 5, 4, 3, 2};
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

struct InheritedFixture
{
    std::vector<ReplicaID> members{membership()};
    EpochStore store{members};
    const EpochDefinition *current{nullptr};
    AdaptiveV2SelectionResult selection;
    TreePlacementInput placement{placement_input()};
    PrivKeySecp256k1 key{private_key()};
    EpochChangeBundleLimits limits{bundle_limits()};

    explicit InheritedFixture(
        EpochDefinitionInput input = inherited_epoch_input(),
        bool selected_replicas_recovered = false)
    {
        current = &store.stage(
            std::move(input), EpochValidationContext{});
        selection = inherited_selection(
            *current, selected_replicas_recovered);
    }

    AdaptiveV2EpochFactoryResult build(
        TreePolicyKind intent =
            TreePolicyKind::performance_optimization) const
    {
        hotstuff::AdaptiveV2TransitionPolicy policy;
        policy.intent = intent;
        if (intent == TreePolicyKind::fault_containment)
        {
            policy.containment_baseline_roots = {
                BaselineRoot{0, 2},
                BaselineRoot{1, 3},
                BaselineRoot{2, 4},
                BaselineRoot{3, 5},
                BaselineRoot{4, 6}};
        }
        return hotstuff::build_adaptive_v2_successor_bundle(
            *current,
            selection,
            policy,
            placement,
            5,
            kIssuerId,
            key,
            limits);
    }
};

template<typename Config, typename = void>
struct has_transition_policy_config : std::false_type
{};

template<typename Config>
struct has_transition_policy_config<
    Config,
    std::void_t<decltype(
        std::declval<Config &>().transition_policy)>> : std::true_type
{};

template<typename Policy, typename = void>
struct has_transition_policy_factory : std::false_type
{};

template<typename Policy>
struct has_transition_policy_factory<
    Policy,
    std::void_t<decltype(hotstuff::build_adaptive_v2_successor_bundle(
        std::declval<const EpochDefinition &>(),
        std::declval<const AdaptiveV2SelectionResult &>(),
        std::declval<const Policy &>(),
        std::declval<const TreePlacementInput &>(),
        std::uint64_t{},
        std::uint32_t{},
        std::declval<const PrivKeySecp256k1 &>(),
        std::declval<const EpochChangeBundleLimits &>()))>>
    : std::true_type
{};

std::vector<ReplicaID> bundle_roots(
    const hotstuff::AdaptiveV2EpochChangeBundle &bundle)
{
    std::vector<ReplicaID> roots;
    for (const auto &candidate : bundle.definition().trees)
    {
        REQUIRE_FALSE(candidate.members_breadth_first.empty());
        roots.push_back(candidate.members_breadth_first.front());
    }
    return roots;
}

void check_bundle_authority_and_leaves(
    const Fixture &fixture,
    const hotstuff::AdaptiveV2EpochChangeBundle &bundle)
{
    CHECK(bundle.definition().membership_digest ==
          fixture.current->membership_digest());
    const auto quorum = hotstuff::derive_byzantine_quorum(
        fixture.members.size());
    REQUIRE(quorum.has_value());
    CHECK(quorum->replica_count == 7);
    CHECK(quorum->fault_threshold == 2);
    CHECK(quorum->quorum == 5);
    for (const auto &candidate : bundle.definition().trees)
    {
        CHECK(candidate.wait_exempt_leaves ==
              std::vector<ReplicaID>{0, 1});
        const auto leaf_start = first_leaf_index(
            candidate.members_breadth_first.size(),
            candidate.fanout);
        for (const auto selected : fixture.selection.selected_replicas)
        {
            const auto position = std::find(
                candidate.members_breadth_first.begin(),
                candidate.members_breadth_first.end(),
                selected);
            REQUIRE(position !=
                    candidate.members_breadth_first.end());
            CHECK(static_cast<std::size_t>(std::distance(
                      candidate.members_breadth_first.begin(),
                      position)) >= leaf_start);
        }
    }
}

template<typename Config>
void verify_explicit_factory_policy_contract()
{
    if constexpr (!has_transition_policy_config<Config>::value)
    {
        FAIL(
            "M12-R01 RED: AdaptiveV2ManagerControllerConfig has no "
            "explicit transition_policy");
    }
    else
    {
        using Policy = std::decay_t<decltype(
            std::declval<Config &>().transition_policy)>;
        if constexpr (!has_transition_policy_factory<Policy>::value)
        {
            FAIL(
                "M12-R01 RED: the epoch factory has no overload accepting "
                "the explicit transition policy");
        }
        else
        {
            Fixture containment;
            Config containment_config;
            containment_config.transition_policy.intent =
                TreePolicyKind::fault_containment;
            containment_config.transition_policy
                .containment_baseline_roots = {
                    BaselineRoot{0, 0},
                    BaselineRoot{1, 1},
                    BaselineRoot{2, 2},
                    BaselineRoot{3, 3},
                    BaselineRoot{4, 4}};
            const auto contained =
                hotstuff::build_adaptive_v2_successor_bundle(
                    *containment.current,
                    containment.selection,
                    containment_config.transition_policy,
                    containment.placement,
                    5,
                    kIssuerId,
                    containment.key,
                    containment.limits);
            REQUIRE(contained);
            REQUIRE(contained.bundle != nullptr);
            CHECK(bundle_roots(*contained.bundle) ==
                  std::vector<ReplicaID>{5, 6, 2, 3, 4});
            check_bundle_authority_and_leaves(
                containment, *contained.bundle);

            Fixture optimization;
            Config optimization_config;
            optimization_config.transition_policy.intent =
                TreePolicyKind::performance_optimization;
            optimization_config.transition_policy
                .containment_baseline_roots.clear();
            const auto optimized =
                hotstuff::build_adaptive_v2_successor_bundle(
                    *optimization.current,
                    optimization.selection,
                    optimization_config.transition_policy,
                    optimization.placement,
                    5,
                    kIssuerId,
                    optimization.key,
                    optimization.limits);
            REQUIRE(optimized);
            REQUIRE(optimized.bundle != nullptr);
            CHECK(bundle_roots(*optimized.bundle) ==
                  std::vector<ReplicaID>{2, 3, 4, 5, 6});
            check_bundle_authority_and_leaves(
                optimization, *optimized.bundle);

            CHECK(contained.bundle->definition().epoch_number == 1);
            CHECK(optimized.bundle->definition().epoch_number == 1);
            CHECK(bundle_roots(*contained.bundle) !=
                  bundle_roots(*optimized.bundle));
        }
    }
}

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

    SECTION("selection drawdown must satisfy the configured guard")
    {
        fixture.selection.eligible_candidates.front().guard_drawdown = -1;
        const auto result = fixture.build();
        CHECK(result.status ==
              AdaptiveV2EpochFactoryStatus::invalid_selection);
        CHECK(result.bundle == nullptr);
    }

    SECTION("selection drawdown cannot exceed uncompensated timeouts")
    {
        fixture.selection.eligible_candidates.front().guard_drawdown = -7;
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

TEST_CASE(
    "factory routes explicit containment and optimization independent of epoch",
    "[adaptive-v2][epoch-factory][transition-policy][n7][intentional-red]")
{
    verify_explicit_factory_policy_contract<
        AdaptiveV2ManagerControllerConfig>();
}

TEST_CASE(
    "factory accepts consensus-inherited constraints only for optimization",
    "[adaptive-v2][epoch-factory][inheritance][optimization][n7]")
{
    InheritedFixture fixture;
    const auto result = fixture.build();

    REQUIRE(result);
    REQUIRE(result.bundle != nullptr);
    CHECK(fixture.selection.constraint_basis ==
          AdaptiveV2SelectionConstraintBasis::
              inherited_consensus_wait_exempt);
    CHECK(fixture.selection.eligible_candidates.empty());
    CHECK(bundle_roots(*result.bundle) ==
          std::vector<ReplicaID>{6, 5, 4, 3, 2});
    REQUIRE(result.bundle->definition().trees.size() == 5);
    for (const auto &candidate : result.bundle->definition().trees)
    {
        CHECK(candidate.wait_exempt_leaves ==
              std::vector<ReplicaID>{0, 1});
    }

    const auto rejected_containment =
        fixture.build(TreePolicyKind::fault_containment);
    CHECK(rejected_containment.status ==
          AdaptiveV2EpochFactoryStatus::invalid_selection);
    CHECK(rejected_containment.bundle == nullptr);
}

TEST_CASE(
    "factory allows guarded containment to refine an exact predecessor set",
    "[adaptive-v2][epoch-factory][containment][recurring][n7]")
{
    InheritedFixture fixture;
    fixture.selection = successful_selection(*fixture.current);
    REQUIRE(fixture.selection.constraint_basis ==
            AdaptiveV2SelectionConstraintBasis::guarded_evidence);

    const auto result = fixture.build(TreePolicyKind::fault_containment);

    REQUIRE(result);
    REQUIRE(result.bundle != nullptr);
    CHECK(bundle_roots(*result.bundle) ==
          std::vector<ReplicaID>{2, 3, 4, 5, 6});
    REQUIRE(result.bundle->definition().trees.size() == 5);
    for (const auto &candidate : result.bundle->definition().trees)
    {
        CHECK(candidate.wait_exempt_leaves ==
              std::vector<ReplicaID>{0, 1});
    }
}

TEST_CASE(
    "factory keeps freshly responsive inherited constraints as leaves",
    "[adaptive-v2][epoch-factory][inheritance][recovered][n7]")
{
    InheritedFixture fixture(inherited_epoch_input(), true);
    for (const auto selected : fixture.selection.selected_replicas)
    {
        const auto entry = std::find_if(
            fixture.selection.snapshot->ranking().begin(),
            fixture.selection.snapshot->ranking().end(),
            [selected](const auto &candidate) {
                return candidate.replica_id == selected;
            });
        REQUIRE(entry != fixture.selection.snapshot->ranking().end());
        REQUIRE(entry->classification == ResponsivenessClass::responsive);
        REQUIRE(entry->eligible);
    }

    const auto ranking_before = fixture.selection.snapshot->ranking();
    const auto snapshot_id_before =
        fixture.selection.snapshot->snapshot_id();
    const auto result = fixture.build();

    REQUIRE(result);
    REQUIRE(result.bundle != nullptr);
    CHECK(bundle_roots(*result.bundle) ==
          std::vector<ReplicaID>{6, 5, 4, 3, 2});
    CHECK(fixture.selection.snapshot->ranking() == ranking_before);
    CHECK(fixture.selection.snapshot->snapshot_id() == snapshot_id_before);
    for (const auto &candidate : result.bundle->definition().trees)
    {
        const auto leaf_start = first_leaf_index(
            candidate.members_breadth_first.size(), candidate.fanout);
        CHECK(candidate.wait_exempt_leaves ==
              std::vector<ReplicaID>{0, 1});
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
}

TEST_CASE(
    "factory independently rejects forged inherited constraints",
    "[adaptive-v2][epoch-factory][inheritance][negative][n7]")
{
    SECTION("an exact predecessor cannot be relabeled guarded evidence")
    {
        InheritedFixture fixture;
        fixture.selection = successful_selection(*fixture.current);
        REQUIRE(fixture.selection.constraint_basis ==
                AdaptiveV2SelectionConstraintBasis::guarded_evidence);
        const auto result = fixture.build();
        CHECK(result.status ==
              AdaptiveV2EpochFactoryStatus::invalid_selection);
        CHECK(result.bundle == nullptr);
    }

    SECTION("a malformed predecessor fails even with guarded evidence")
    {
        auto input = inherited_epoch_input();
        for (auto &candidate : input.trees)
            candidate.wait_exempt_leaves = {0};
        InheritedFixture fixture(std::move(input));
        fixture.selection = successful_selection(*fixture.current);
        REQUIRE(fixture.selection.constraint_basis ==
                AdaptiveV2SelectionConstraintBasis::guarded_evidence);
        const auto result = fixture.build();
        CHECK(result.status ==
              AdaptiveV2EpochFactoryStatus::invalid_selection);
        CHECK(result.bundle == nullptr);
    }

    SECTION("inconsistent predecessor sets fail with guarded evidence")
    {
        auto input = inherited_epoch_input();
        input.trees[1].wait_exempt_leaves = {0, 2};
        InheritedFixture fixture(std::move(input));
        fixture.selection = successful_selection(*fixture.current);
        REQUIRE(fixture.selection.constraint_basis ==
                AdaptiveV2SelectionConstraintBasis::guarded_evidence);
        const auto result = fixture.build();
        CHECK(result.status ==
              AdaptiveV2EpochFactoryStatus::invalid_selection);
        CHECK(result.bundle == nullptr);
    }

    SECTION("the predecessor cannot have empty inherited constraints")
    {
        auto input = inherited_epoch_input();
        for (auto &candidate : input.trees)
            candidate.wait_exempt_leaves.clear();
        InheritedFixture fixture(std::move(input));
        const auto result = fixture.build();
        CHECK(result.status ==
              AdaptiveV2EpochFactoryStatus::invalid_selection);
        CHECK(result.bundle == nullptr);
    }

    SECTION("the predecessor constraints must have exact size f")
    {
        auto input = inherited_epoch_input();
        for (auto &candidate : input.trees)
            candidate.wait_exempt_leaves = {0};
        InheritedFixture fixture(std::move(input));
        const auto result = fixture.build();
        CHECK(result.status ==
              AdaptiveV2EpochFactoryStatus::invalid_selection);
        CHECK(result.bundle == nullptr);
    }

    SECTION("every predecessor tree must carry the same canonical set")
    {
        auto input = inherited_epoch_input();
        input.trees[1].wait_exempt_leaves = {0, 2};
        InheritedFixture fixture(std::move(input));
        const auto result = fixture.build();
        CHECK(result.status ==
              AdaptiveV2EpochFactoryStatus::invalid_selection);
        CHECK(result.bundle == nullptr);
    }

    SECTION("the selection cannot replace the consensus inherited set")
    {
        InheritedFixture fixture;
        fixture.selection.selected_replicas = {0, 2};
        const auto result = fixture.build();
        CHECK(result.status ==
              AdaptiveV2EpochFactoryStatus::invalid_selection);
        CHECK(result.bundle == nullptr);
    }

    SECTION("the selection cannot forge the fresh root ranking")
    {
        InheritedFixture fixture;
        std::swap(
            fixture.selection.eligible_roots[0],
            fixture.selection.eligible_roots[1]);
        const auto result = fixture.build();
        CHECK(result.status ==
              AdaptiveV2EpochFactoryStatus::root_mismatch);
        CHECK(result.bundle == nullptr);
    }

    SECTION("inherited constraints cannot carry guarded candidate audits")
    {
        InheritedFixture fixture;
        fixture.selection.eligible_candidates.push_back(
            AdaptiveV2CandidateAudit{});
        const auto result = fixture.build();
        CHECK(result.status ==
              AdaptiveV2EpochFactoryStatus::invalid_selection);
        CHECK(result.bundle == nullptr);
    }
}

TEST_CASE(
    "maximum epoch has no checked successor",
    "[adaptive-v2][epoch-factory][overflow][fail-closed]")
{
    CHECK_FALSE(hotstuff::checked_successor_epoch(
        std::numeric_limits<std::uint32_t>::max()));
    hotstuff::AdaptiveV2EpochFactoryResult exhausted;
    exhausted.status =
        AdaptiveV2EpochFactoryStatus::epoch_number_exhausted;
    CHECK(exhausted.bundle == nullptr);
}
