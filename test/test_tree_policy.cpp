#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <fstream>
#include <limits>
#include <map>
#include <numeric>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/adaptation.h"
#include "hotstuff/configuration.h"

/*
 * T10 pure tree-policy contract
 * -----------------------------
 * Tree placement consumes fixed membership and the immutable R09 snapshot.
 * Eligibility is derived only from AdaptationSnapshot::ranking(); there is no
 * independently supplied crash list, profile label, or ineligible set.
 *
 * The fallback pins the complete public seam while tree_policy.h is absent.
 * Both overloads remain undefined so this tests-first target links RED solely
 * until production supplies the pure policy implementation.
 */
#if __has_include("hotstuff/tree_policy.h")
#include "hotstuff/tree_policy.h"
#define KAURI_HAS_T10_TREE_POLICY_API 1
#else
#define KAURI_HAS_T10_TREE_POLICY_API 0

namespace hotstuff
{

constexpr std::uint32_t kTreePolicySchemaVersion = 1;
constexpr std::size_t kMaximumTreePolicyMembers = 4'096;
constexpr std::uint32_t kMaximumTreePolicyTrees = 256;
constexpr std::uint32_t kMaximumTreePolicyFanout = 4'096;
constexpr std::uint32_t kMaximumTreePolicyPipelineStretch = 4'096;
constexpr std::size_t kMaximumTreePolicyVersionBytes = 64;

enum class TreePolicyKind : std::uint8_t
{
    fault_containment = 1,
    performance_optimization = 2,
};

enum class TreeReplicaRole : std::uint8_t
{
    root = 1,
    internal = 2,
    leaf = 3,
};

enum class RootSelectionReason : std::uint8_t
{
    preserved_eligible_baseline = 1,
    fallback_ineligible_baseline = 2,
    fallback_duplicate_baseline = 3,
    fallback_missing_baseline = 4,
    highest_ranked_eligible = 5,
};

enum class ReplicaPlacementReason : std::uint8_t
{
    selected_root = 1,
    balanced_eligible_internal = 2,
    seeded_eligible_leaf = 3,
    constrained_ineligible_leaf = 4,
};

struct TreeShape
{
    std::uint32_t fanout{0};
    std::uint32_t pipeline_stretch{0};
    std::uint32_t tree_count{0};
};

struct BaselineRoot
{
    std::uint32_t tree_id{0};
    ReplicaID replica_id{0};
};

struct FaultContainmentPolicy
{
    std::vector<BaselineRoot> baseline_roots;
};

struct PerformanceOptimizationPolicy
{};

struct TreePlacementInput
{
    std::vector<ReplicaID> membership;
    TreeShape shape;
    std::uint64_t generation_seed{0};
    std::string policy_version;
};

struct RootDecision
{
    std::uint32_t tree_id{0};
    std::optional<ReplicaID> requested_baseline_root;
    ReplicaID chosen_root{0};
    std::uint32_t chosen_rank{0};
    RootSelectionReason reason{
        RootSelectionReason::highest_ranked_eligible};
};

struct ReplicaRoleDecision
{
    std::uint32_t tree_id{0};
    ReplicaID replica_id{0};
    std::uint32_t rank{0};
    ResponsivenessClass classification{
        ResponsivenessClass::insufficient_evidence};
    bool eligible{false};
    std::uint32_t position{0};
    TreeReplicaRole role{TreeReplicaRole::leaf};
    ReplicaPlacementReason reason{
        ReplicaPlacementReason::constrained_ineligible_leaf};
};

struct TreePlacementExplanation
{
    std::uint32_t schema_version{kTreePolicySchemaVersion};
    TreePolicyKind policy_kind{TreePolicyKind::fault_containment};
    std::string policy_version;
    std::uint64_t generation_seed{0};
    std::string evidence_snapshot_id;
    std::uint64_t evidence_cutoff{0};
    std::vector<RootDecision> root_decisions;
    std::vector<ReplicaRoleDecision> replica_roles;
};

class TreePlacementResult final
{
public:
    TreePlacementResult(const TreePlacementResult &) = default;
    TreePlacementResult(TreePlacementResult &&) = default;
    TreePlacementResult &operator=(const TreePlacementResult &) = delete;
    TreePlacementResult &operator=(TreePlacementResult &&) = delete;

    const std::vector<EpochTreeDefinition> &trees() const noexcept
    {
        return trees_;
    }

    const TreePlacementExplanation &explanation() const noexcept
    {
        return explanation_;
    }

private:
    friend TreePlacementResult build_tree_placement(
        const TreePlacementInput &input,
        const AdaptationSnapshot &snapshot,
        const FaultContainmentPolicy &policy);

    friend TreePlacementResult build_tree_placement(
        const TreePlacementInput &input,
        const AdaptationSnapshot &snapshot,
        const PerformanceOptimizationPolicy &policy);

    TreePlacementResult() = default;

    std::vector<EpochTreeDefinition> trees_;
    TreePlacementExplanation explanation_;
};

TreePlacementResult build_tree_placement(
    const TreePlacementInput &input,
    const AdaptationSnapshot &snapshot,
    const FaultContainmentPolicy &policy);

TreePlacementResult build_tree_placement(
    const TreePlacementInput &input,
    const AdaptationSnapshot &snapshot,
    const PerformanceOptimizationPolicy &policy);

} // namespace hotstuff
#endif

namespace
{

using hotstuff::AcceptedEvidenceRecord;
using hotstuff::AcceptedEvidenceView;
using hotstuff::AdaptationEpochId;
using hotstuff::AdaptationPolicy;
using hotstuff::AdaptationSnapshot;
using hotstuff::BaselineRoot;
using hotstuff::EpochTreeDefinition;
using hotstuff::ExpectedMessageType;
using hotstuff::FaultContainmentPolicy;
using hotstuff::PerformanceOptimizationPolicy;
using hotstuff::ReplicaAdaptationResult;
using hotstuff::ReplicaID;
using hotstuff::ReplicaPlacementReason;
using hotstuff::ResponseObservation;
using hotstuff::ResponseOutcome;
using hotstuff::ResponsivenessClass;
using hotstuff::RootSelectionReason;
using hotstuff::TreePlacementInput;
using hotstuff::TreePlacementResult;
using hotstuff::TreePolicyKind;
using hotstuff::TreeReplicaRole;
using hotstuff::TreeShape;
using hotstuff::uint256_t;

uint256_t fixture_digest(const std::string &label)
{
    return hotstuff::DataStream(label).get_hash();
}

std::vector<ReplicaID> sequential_members(std::size_t count)
{
    std::vector<ReplicaID> members(count);
    std::iota(members.begin(), members.end(), ReplicaID{0});
    return members;
}

AcceptedEvidenceView evidence_view(
    const std::vector<AcceptedEvidenceRecord> &records)
{
    return {records.empty() ? nullptr : records.data(), records.size()};
}

AdaptationPolicy snapshot_policy(std::uint32_t minimum_attempts = 8)
{
    AdaptationPolicy policy;
    policy.attempt_window = std::max<std::uint32_t>(32, minimum_attempts);
    policy.minimum_attempts = minimum_attempts;
    policy.trailing_timeout_streak = 3;
    return policy;
}

AdaptationSnapshot make_snapshot(
    const std::vector<ReplicaID> &membership,
    const std::set<ReplicaID> &ineligible = {},
    std::uint32_t attempts_per_replica = 8,
    std::uint64_t snapshot_seed = 0xA0910)
{
    const AdaptationEpochId epoch{
        41, fixture_digest("t10-adaptation-epoch")};
    auto ordered = membership;
    std::sort(ordered.begin(), ordered.end());

    std::map<ReplicaID, std::uint64_t> eligible_latency;
    std::uint64_t next_latency = 10;
    for (const auto replica : ordered)
    {
        if (ineligible.count(replica) == 0)
        {
            eligible_latency.emplace(replica, next_latency);
            next_latency += 10;
        }
    }

    std::vector<AcceptedEvidenceRecord> records;
    records.reserve(membership.size() * attempts_per_replica);
    std::uint64_t ingestion_sequence = 0;
    std::uint64_t block_number = 0;
    for (const auto replica : ordered)
    {
        for (std::uint32_t attempt = 0;
             attempt < attempts_per_replica;
             ++attempt)
        {
            ResponseObservation observation;
            observation.schema_version =
                hotstuff::kResponseObservationSchemaVersion;
            observation.reporter_id = ordered.front();
            observation.observed_replica_id = replica;
            observation.configuration = {
                epoch.epoch_number,
                static_cast<std::uint32_t>(attempt % 3),
                epoch.epoch_digest};
            observation.block_hash = fixture_digest(
                "t10-block-" + std::to_string(block_number++));
            observation.expected_message_type =
                ExpectedMessageType::direct_vote;
            observation.deadline_duration_us = 1'000;
            observation.reporter_sequence = ingestion_sequence + 1;
            observation.reporter_monotonic_ns =
                (ingestion_sequence + 1) * 1'000;
            if (ineligible.count(replica) != 0)
            {
                observation.outcome = ResponseOutcome::timeout;
                observation.response_duration_us = 0;
                observation.signer_set.clear();
            }
            else
            {
                observation.outcome = ResponseOutcome::on_time;
                observation.response_duration_us =
                    eligible_latency.at(replica);
                observation.signer_set = {replica};
            }
            observation.observation_id =
                hotstuff::compute_response_observation_id(
                    observation.attempt_identity());
            records.push_back({++ingestion_sequence, observation});
        }
    }

    return hotstuff::build_adaptation_snapshot(
        membership,
        epoch,
        evidence_view(records),
        ingestion_sequence,
        snapshot_policy(attempts_per_replica),
        snapshot_seed);
}

TreePlacementInput placement_input(
    std::vector<ReplicaID> membership,
    std::uint32_t fanout,
    std::uint32_t tree_count,
    std::uint64_t seed,
    const std::string &policy_version,
    std::uint32_t pipeline_stretch = 2)
{
    return {
        std::move(membership),
        TreeShape{fanout, pipeline_stretch, tree_count},
        seed,
        policy_version};
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

const ReplicaAdaptationResult &score_for(
    const AdaptationSnapshot &snapshot,
    ReplicaID replica)
{
    const auto found = std::find_if(
        snapshot.ranking().begin(),
        snapshot.ranking().end(),
        [replica](const auto &score) {
            return score.replica_id == replica;
        });
    REQUIRE(found != snapshot.ranking().end());
    return *found;
}

std::vector<ReplicaID> selected_roots(
    const TreePlacementResult &result)
{
    std::vector<ReplicaID> roots;
    for (const auto &tree : result.trees())
    {
        REQUIRE_FALSE(tree.members_breadth_first.empty());
        roots.push_back(tree.members_breadth_first.front());
    }
    return roots;
}

std::string output_fingerprint(const TreePlacementResult &result)
{
    std::ostringstream output;
    for (const auto &tree : result.trees())
    {
        output << "T:" << tree.tree_id << ':' << tree.fanout << ':'
               << tree.pipeline_stretch << ':';
        for (const auto replica : tree.members_breadth_first)
            output << replica << ',';
        output << ';';
    }

    const auto &explanation = result.explanation();
    output << "E:" << explanation.schema_version << ':'
           << static_cast<unsigned>(explanation.policy_kind) << ':'
           << explanation.policy_version << ':'
           << explanation.generation_seed << ':'
           << explanation.evidence_snapshot_id << ':'
           << explanation.evidence_cutoff << ';';
    for (const auto &root : explanation.root_decisions)
    {
        output << "R:" << root.tree_id << ':';
        if (root.requested_baseline_root.has_value())
            output << *root.requested_baseline_root;
        else
            output << '-';
        output << ':' << root.chosen_root << ':' << root.chosen_rank << ':'
               << static_cast<unsigned>(root.reason) << ';';
    }
    for (const auto &replica : explanation.replica_roles)
    {
        output << "P:" << replica.tree_id << ':' << replica.replica_id << ':'
               << replica.rank << ':'
               << static_cast<unsigned>(replica.classification) << ':'
               << replica.eligible << ':' << replica.position << ':'
               << static_cast<unsigned>(replica.role) << ':'
               << static_cast<unsigned>(replica.reason) << ';';
    }
    return output.str();
}

void check_tree_semantics(
    const TreePlacementResult &result,
    const TreePlacementInput &input,
    const AdaptationSnapshot &snapshot,
    TreePolicyKind expected_kind)
{
    REQUIRE(result.trees().size() == input.shape.tree_count);
    REQUIRE(result.explanation().root_decisions.size() ==
            input.shape.tree_count);
    REQUIRE(result.explanation().replica_roles.size() ==
            input.membership.size() * input.shape.tree_count);

    const auto &explanation = result.explanation();
    CHECK(explanation.schema_version == hotstuff::kTreePolicySchemaVersion);
    CHECK(explanation.policy_kind == expected_kind);
    CHECK(explanation.policy_version == input.policy_version);
    CHECK(explanation.generation_seed == input.generation_seed);
    CHECK(explanation.evidence_snapshot_id == snapshot.snapshot_id());
    CHECK(explanation.evidence_cutoff == snapshot.evidence_cutoff());

    auto canonical_membership = input.membership;
    std::sort(canonical_membership.begin(), canonical_membership.end());
    const auto first_leaf = first_leaf_index(
        input.membership.size(), input.shape.fanout);

    std::set<ReplicaID> distinct_roots;
    for (std::size_t tree_index = 0;
         tree_index < result.trees().size();
         ++tree_index)
    {
        const auto &tree = result.trees().at(tree_index);
        CHECK(tree.tree_id == tree_index);
        CHECK(tree.fanout == input.shape.fanout);
        CHECK(tree.pipeline_stretch == input.shape.pipeline_stretch);
        REQUIRE(tree.members_breadth_first.size() == input.membership.size());

        auto actual_members = tree.members_breadth_first;
        std::sort(actual_members.begin(), actual_members.end());
        CHECK(actual_members == canonical_membership);
        REQUIRE(distinct_roots.insert(
                    tree.members_breadth_first.front())
                    .second);

        const auto &root_decision =
            explanation.root_decisions.at(tree_index);
        CHECK(root_decision.tree_id == tree_index);
        CHECK(root_decision.chosen_root ==
              tree.members_breadth_first.front());
        const auto &root_score = score_for(
            snapshot, root_decision.chosen_root);
        CHECK(root_decision.chosen_rank == root_score.rank);
        CHECK(root_score.eligible);

        for (std::size_t position = 0;
             position < tree.members_breadth_first.size();
             ++position)
        {
            const auto replica = tree.members_breadth_first.at(position);
            const auto &score = score_for(snapshot, replica);
            if (position == 0 || position < first_leaf)
                CHECK(score.eligible);
            if (!score.eligible)
                CHECK(position >= first_leaf);

            const auto decision_index =
                tree_index * input.membership.size() + position;
            const auto &decision =
                explanation.replica_roles.at(decision_index);
            CHECK(decision.tree_id == tree_index);
            CHECK(decision.replica_id == replica);
            CHECK(decision.rank == score.rank);
            CHECK(decision.classification == score.classification);
            CHECK(decision.eligible == score.eligible);
            CHECK(decision.position == position);

            if (position == 0)
            {
                CHECK(decision.role == TreeReplicaRole::root);
                CHECK(decision.reason ==
                      ReplicaPlacementReason::selected_root);
            }
            else if (position < first_leaf)
            {
                CHECK(decision.role == TreeReplicaRole::internal);
                CHECK(decision.reason ==
                      ReplicaPlacementReason::balanced_eligible_internal);
            }
            else
            {
                CHECK(decision.role == TreeReplicaRole::leaf);
                CHECK(decision.reason ==
                      (score.eligible
                           ? ReplicaPlacementReason::seeded_eligible_leaf
                           : ReplicaPlacementReason::
                                 constrained_ineligible_leaf));
            }
        }
    }
}

std::string read_source(const std::string &relative_path)
{
#ifdef KAURI_PROJECT_SOURCE_DIR
    const std::string root = KAURI_PROJECT_SOURCE_DIR;
#else
    const std::string root = ".";
#endif
    std::ifstream input(root + "/" + relative_path);
    REQUIRE(input.good());
    return std::string(
        std::istreambuf_iterator<char>(input),
        std::istreambuf_iterator<char>());
}

} // namespace

TEST_CASE("T10 API pins immutable bounded pure placement contracts",
          "[t10][tree-policy][contract][intentional-red]")
{
    static_assert(
        !std::is_default_constructible<TreePlacementResult>::value,
        "only validated policy functions may construct tree results");
    static_assert(
        !std::is_copy_assignable<TreePlacementResult>::value,
        "tree results must be immutable after construction");
    static_assert(
        !std::is_move_assignable<TreePlacementResult>::value,
        "tree results must not support replacement");

    CHECK(KAURI_HAS_T10_TREE_POLICY_API == 1);
    CHECK(hotstuff::kTreePolicySchemaVersion == 1);
    CHECK(hotstuff::kMaximumTreePolicyMembers == 4'096);
    CHECK(hotstuff::kMaximumTreePolicyTrees == 256);
    CHECK(hotstuff::kMaximumTreePolicyFanout == 4'096);
    CHECK(hotstuff::kMaximumTreePolicyPipelineStretch == 4'096);
    CHECK(hotstuff::kMaximumTreePolicyVersionBytes == 64);

    CHECK(static_cast<std::uint8_t>(
              TreePolicyKind::fault_containment) == 1);
    CHECK(static_cast<std::uint8_t>(
              TreePolicyKind::performance_optimization) == 2);
    CHECK(static_cast<std::uint8_t>(TreeReplicaRole::root) == 1);
    CHECK(static_cast<std::uint8_t>(TreeReplicaRole::internal) == 2);
    CHECK(static_cast<std::uint8_t>(TreeReplicaRole::leaf) == 3);
}

TEST_CASE("containment preserves eligible baseline roots and explains every role",
          "[t10][tree-policy][containment][explanation][31-node]"
          "[intentional-red]")
{
    const auto members = sequential_members(31);
    const auto snapshot = make_snapshot(members, {3, 7, 15});
    const auto input = placement_input(
        members, 2, 3, 0xC011, "fault-containment-v1", 4);
    const FaultContainmentPolicy policy{{
        BaselineRoot{0, 10},
        BaselineRoot{1, 11},
        BaselineRoot{2, 12},
    }};

    const auto result = hotstuff::build_tree_placement(
        input, snapshot, policy);

    CHECK(selected_roots(result) ==
          std::vector<ReplicaID>{10, 11, 12});
    check_tree_semantics(
        result, input, snapshot, TreePolicyKind::fault_containment);
    for (std::size_t tree = 0; tree < 3; ++tree)
    {
        const auto &decision =
            result.explanation().root_decisions.at(tree);
        REQUIRE(decision.requested_baseline_root.has_value());
        CHECK(*decision.requested_baseline_root == 10 + tree);
        CHECK(decision.reason ==
              RootSelectionReason::preserved_eligible_baseline);
    }
}

TEST_CASE("containment replaces unusable baseline roots by ranked eligible replicas",
          "[t10][tree-policy][containment][fallback][intentional-red]")
{
    const auto members = sequential_members(13);
    const auto snapshot = make_snapshot(members, {9, 10});
    const auto input = placement_input(
        members, 2, 4, 0xFA11, "fault-containment-fallback-v1");
    const FaultContainmentPolicy policy{{
        BaselineRoot{0, 5},
        BaselineRoot{1, 10},
        BaselineRoot{2, 5},
        BaselineRoot{3, 99},
    }};

    const auto result = hotstuff::build_tree_placement(
        input, snapshot, policy);

    CHECK(selected_roots(result) ==
          std::vector<ReplicaID>{5, 0, 1, 2});
    check_tree_semantics(
        result, input, snapshot, TreePolicyKind::fault_containment);
    const auto &decisions = result.explanation().root_decisions;
    CHECK(decisions.at(0).reason ==
          RootSelectionReason::preserved_eligible_baseline);
    CHECK(decisions.at(1).reason ==
          RootSelectionReason::fallback_ineligible_baseline);
    CHECK(decisions.at(2).reason ==
          RootSelectionReason::fallback_duplicate_baseline);
    CHECK(decisions.at(3).reason ==
          RootSelectionReason::fallback_missing_baseline);

    auto reordered_policy = policy;
    std::reverse(
        reordered_policy.baseline_roots.begin(),
        reordered_policy.baseline_roots.end());
    const auto reordered = hotstuff::build_tree_placement(
        input, snapshot, reordered_policy);
    CHECK(output_fingerprint(reordered) == output_fingerprint(result));

    SECTION("fallback selection reserves every later valid baseline root")
    {
        const auto reserved_input = placement_input(
            members, 2, 3, 0xFA12, "fault-containment-reserved-v1");
        const FaultContainmentPolicy reserved_policy{{
            BaselineRoot{0, 10},
            BaselineRoot{1, 0},
            BaselineRoot{2, 1},
        }};
        const auto reserved = hotstuff::build_tree_placement(
            reserved_input, snapshot, reserved_policy);

        CHECK(selected_roots(reserved) ==
              std::vector<ReplicaID>{2, 0, 1});
        CHECK(reserved.explanation().root_decisions.at(0).reason ==
              RootSelectionReason::fallback_ineligible_baseline);
        CHECK(reserved.explanation().root_decisions.at(1).reason ==
              RootSelectionReason::preserved_eligible_baseline);
        CHECK(reserved.explanation().root_decisions.at(2).reason ==
              RootSelectionReason::preserved_eligible_baseline);
    }
}

TEST_CASE("optimization chooses exactly the highest-ranked eligible roots",
          "[t10][tree-policy][optimization][fr7][intentional-red]")
{
    const auto members = sequential_members(31);
    const auto snapshot = make_snapshot(members, {3, 7, 15});
    const auto input = placement_input(
        members, 2, 3, 0x0F71, "performance-optimization-v1", 4);

    const auto result = hotstuff::build_tree_placement(
        input, snapshot, PerformanceOptimizationPolicy{});

    CHECK(selected_roots(result) ==
          std::vector<ReplicaID>{0, 1, 2});
    check_tree_semantics(
        result,
        input,
        snapshot,
        TreePolicyKind::performance_optimization);
    for (const auto &decision : result.explanation().root_decisions)
    {
        CHECK_FALSE(decision.requested_baseline_root.has_value());
        CHECK(decision.reason ==
              RootSelectionReason::highest_ranked_eligible);
    }
}

TEST_CASE("containment and optimization keep separate causal claims",
          "[t10][tree-policy][causal-separation][intentional-red]")
{
    const auto members = sequential_members(31);
    const auto snapshot = make_snapshot(members, {3, 7, 15});
    const auto containment_input = placement_input(
        members, 2, 3, 0xCA05, "fault-containment-v1");
    const auto optimized_input = placement_input(
        members, 2, 3, 0xCA05, "performance-optimization-v1");
    const FaultContainmentPolicy containment_policy{{
        BaselineRoot{0, 10},
        BaselineRoot{1, 11},
        BaselineRoot{2, 12},
    }};

    const auto contained = hotstuff::build_tree_placement(
        containment_input, snapshot, containment_policy);
    const auto optimized = hotstuff::build_tree_placement(
        optimized_input, snapshot, PerformanceOptimizationPolicy{});

    CHECK(selected_roots(contained) ==
          std::vector<ReplicaID>{10, 11, 12});
    CHECK(selected_roots(optimized) ==
          std::vector<ReplicaID>{0, 1, 2});
    for (const auto crashed : {ReplicaID{3}, ReplicaID{7}, ReplicaID{15}})
    {
        for (const auto &result : {&contained, &optimized})
        {
            for (const auto &tree : result->trees())
            {
                const auto position = std::find(
                    tree.members_breadth_first.begin(),
                    tree.members_breadth_first.end(),
                    crashed);
                REQUIRE(position != tree.members_breadth_first.end());
                CHECK(static_cast<std::size_t>(
                          position - tree.members_breadth_first.begin()) >=
                      first_leaf_index(
                          tree.members_breadth_first.size(), tree.fanout));
            }
        }
    }
}

TEST_CASE("logical input order cannot change canonical tree output",
          "[t10][tree-policy][determinism][canonical-input]"
          "[intentional-red]")
{
    const auto members = sequential_members(31);
    const auto snapshot = make_snapshot(members, {3, 7, 15});
    auto reordered_members = members;
    std::reverse(reordered_members.begin(), reordered_members.end());

    const auto canonical_input = placement_input(
        members, 2, 3, 0xD371, "performance-optimization-v1");
    const auto reordered_input = placement_input(
        reordered_members, 2, 3, 0xD371, "performance-optimization-v1");
    const auto canonical = hotstuff::build_tree_placement(
        canonical_input, snapshot, PerformanceOptimizationPolicy{});
    const auto reordered = hotstuff::build_tree_placement(
        reordered_input, snapshot, PerformanceOptimizationPolicy{});

    CHECK(output_fingerprint(reordered) == output_fingerprint(canonical));
}

TEST_CASE("seed changes only deterministic non-semantic placements",
          "[t10][tree-policy][seed][determinism][intentional-red]")
{
    const auto members = sequential_members(31);
    const auto snapshot = make_snapshot(members, {3, 7, 15});
    const auto first_input = placement_input(
        members, 2, 3, 7, "performance-optimization-v1");
    const auto second_input = placement_input(
        members, 2, 3, 99, "performance-optimization-v1");

    const auto first = hotstuff::build_tree_placement(
        first_input, snapshot, PerformanceOptimizationPolicy{});
    const auto second = hotstuff::build_tree_placement(
        second_input, snapshot, PerformanceOptimizationPolicy{});

    CHECK(selected_roots(first) == selected_roots(second));
    CHECK(selected_roots(first) ==
          std::vector<ReplicaID>{0, 1, 2});
    bool non_root_placement_changed = false;
    for (std::size_t tree = 0; tree < first.trees().size(); ++tree)
    {
        const auto &left =
            first.trees().at(tree).members_breadth_first;
        const auto &right =
            second.trees().at(tree).members_breadth_first;
        non_root_placement_changed = non_root_placement_changed ||
                                     !std::equal(
                                         left.begin() + 1,
                                         left.end(),
                                         right.begin() + 1);
    }
    CHECK(non_root_placement_changed);
    check_tree_semantics(
        first,
        first_input,
        snapshot,
        TreePolicyKind::performance_optimization);
    check_tree_semantics(
        second,
        second_input,
        snapshot,
        TreePolicyKind::performance_optimization);
}

TEST_CASE("eligible internal load is balanced across multiple full trees",
          "[t10][tree-policy][balance][31-node][intentional-red]")
{
    const auto members = sequential_members(31);
    const auto snapshot = make_snapshot(members, {28, 29, 30});
    const auto input = placement_input(
        members, 2, 3, 0xBA1A, "fault-containment-balance-v1");
    const FaultContainmentPolicy policy{{
        BaselineRoot{0, 10},
        BaselineRoot{1, 11},
        BaselineRoot{2, 12},
    }};

    const auto result = hotstuff::build_tree_placement(
        input, snapshot, policy);
    check_tree_semantics(
        result, input, snapshot, TreePolicyKind::fault_containment);

    std::map<ReplicaID, std::uint32_t> internal_counts;
    for (const auto &score : snapshot.ranking())
    {
        if (score.eligible)
            internal_counts.emplace(score.replica_id, 0);
    }
    for (const auto &decision : result.explanation().replica_roles)
    {
        if (decision.role == TreeReplicaRole::internal)
            ++internal_counts.at(decision.replica_id);
    }
    std::vector<std::uint32_t> counts;
    for (const auto &entry : internal_counts)
        counts.push_back(entry.second);
    REQUIRE_FALSE(counts.empty());
    const auto bounds = std::minmax_element(counts.begin(), counts.end());
    CHECK(*bounds.second - *bounds.first <= 1);
}

TEST_CASE("single-member and wide-fanout trees preserve leaf arithmetic",
          "[t10][tree-policy][edge][leaf-index][intentional-red]")
{
    SECTION("one eligible member is both the only root and leaf boundary")
    {
        const auto members = sequential_members(1);
        const auto snapshot = make_snapshot(members);
        const auto input = placement_input(
            members,
            hotstuff::kMaximumTreePolicyFanout,
            1,
            1,
            "single-member-v1",
            0);
        const FaultContainmentPolicy policy{{BaselineRoot{0, 0}}};

        const auto result = hotstuff::build_tree_placement(
            input, snapshot, policy);
        check_tree_semantics(
            result, input, snapshot, TreePolicyKind::fault_containment);
        CHECK(first_leaf_index(1, input.shape.fanout) == 0);
        CHECK(selected_roots(result) == std::vector<ReplicaID>{0});
    }

    SECTION("fanout at least membership makes every non-root a leaf")
    {
        const auto members = sequential_members(5);
        const auto snapshot = make_snapshot(members, {4});
        const auto input = placement_input(
            members, 8, 1, 2, "wide-fanout-v1", 0);
        const FaultContainmentPolicy policy{{BaselineRoot{0, 2}}};

        const auto result = hotstuff::build_tree_placement(
            input, snapshot, policy);
        check_tree_semantics(
            result, input, snapshot, TreePolicyKind::fault_containment);
        CHECK(first_leaf_index(5, input.shape.fanout) == 1);
        for (std::size_t position = 1;
             position < result.trees().front().members_breadth_first.size();
             ++position)
        {
            const auto &decision =
                result.explanation().replica_roles.at(position);
            CHECK(decision.role == TreeReplicaRole::leaf);
        }
    }
}

TEST_CASE("placement fails when roots or leaf capacity are insufficient",
          "[t10][tree-policy][capacity][failure][intentional-red]")
{
    SECTION("optimization needs one distinct eligible root per tree")
    {
        const auto members = sequential_members(7);
        const auto snapshot = make_snapshot(members, {2, 3, 4, 5, 6});
        const auto input = placement_input(
            members, 7, 3, 1, "insufficient-roots-v1");
        CHECK_THROWS_AS(
            hotstuff::build_tree_placement(
                input, snapshot, PerformanceOptimizationPolicy{}),
            std::invalid_argument);
    }

    SECTION("all ineligible replicas must fit in breadth-first leaves")
    {
        const auto members = sequential_members(7);
        const auto snapshot = make_snapshot(members, {2, 3, 4, 5, 6});
        const auto input = placement_input(
            members, 2, 1, 1, "insufficient-leaves-v1");
        const FaultContainmentPolicy policy{{BaselineRoot{0, 0}}};
        CHECK(first_leaf_index(7, 2) == 3);
        CHECK_THROWS_AS(
            hotstuff::build_tree_placement(input, snapshot, policy),
            std::invalid_argument);
    }
}

TEST_CASE("shape membership ranking and policy boundaries fail closed",
          "[t10][tree-policy][validation][bounds][intentional-red]")
{
    const auto members = sequential_members(7);
    const auto snapshot = make_snapshot(members);
    const auto valid = placement_input(
        members, 2, 2, 7, "validation-v1");
    const PerformanceOptimizationPolicy optimized;

    SECTION("fanout and tree count must be nonzero")
    {
        auto input = valid;
        input.shape.fanout = 0;
        CHECK_THROWS_AS(
            hotstuff::build_tree_placement(input, snapshot, optimized),
            std::invalid_argument);

        input = valid;
        input.shape.tree_count = 0;
        CHECK_THROWS_AS(
            hotstuff::build_tree_placement(input, snapshot, optimized),
            std::invalid_argument);
    }

    SECTION("shape arithmetic is capped before expansion")
    {
        auto input = valid;
        input.shape.fanout =
            hotstuff::kMaximumTreePolicyFanout + 1;
        CHECK_THROWS_AS(
            hotstuff::build_tree_placement(input, snapshot, optimized),
            std::invalid_argument);

        input = valid;
        input.shape.pipeline_stretch =
            hotstuff::kMaximumTreePolicyPipelineStretch + 1;
        CHECK_THROWS_AS(
            hotstuff::build_tree_placement(input, snapshot, optimized),
            std::invalid_argument);

        input = valid;
        input.shape.tree_count =
            hotstuff::kMaximumTreePolicyTrees + 1;
        CHECK_THROWS_AS(
            hotstuff::build_tree_placement(input, snapshot, optimized),
            std::invalid_argument);
    }

    SECTION("membership must be nonempty unique bounded and snapshot-exact")
    {
        auto input = valid;
        input.membership.clear();
        CHECK_THROWS_AS(
            hotstuff::build_tree_placement(input, snapshot, optimized),
            std::invalid_argument);

        input = valid;
        input.membership.back() = input.membership.front();
        CHECK_THROWS_AS(
            hotstuff::build_tree_placement(input, snapshot, optimized),
            std::invalid_argument);

        input = valid;
        input.membership.pop_back();
        CHECK_THROWS_AS(
            hotstuff::build_tree_placement(input, snapshot, optimized),
            std::invalid_argument);

        input = valid;
        input.membership = sequential_members(
            hotstuff::kMaximumTreePolicyMembers + 1);
        CHECK_THROWS_AS(
            hotstuff::build_tree_placement(input, snapshot, optimized),
            std::invalid_argument);
    }

    SECTION("policy version is required and byte bounded")
    {
        auto input = valid;
        input.policy_version.clear();
        CHECK_THROWS_AS(
            hotstuff::build_tree_placement(input, snapshot, optimized),
            std::invalid_argument);

        input = valid;
        input.policy_version.assign(
            hotstuff::kMaximumTreePolicyVersionBytes + 1, 'x');
        CHECK_THROWS_AS(
            hotstuff::build_tree_placement(input, snapshot, optimized),
            std::invalid_argument);
    }

    SECTION("baseline metadata covers each canonical tree exactly once")
    {
        CHECK_THROWS_AS(
            hotstuff::build_tree_placement(
                valid,
                snapshot,
                FaultContainmentPolicy{{BaselineRoot{0, 0}}}),
            std::invalid_argument);
        CHECK_THROWS_AS(
            hotstuff::build_tree_placement(
                valid,
                snapshot,
                FaultContainmentPolicy{{
                    BaselineRoot{0, 0}, BaselineRoot{0, 1}}}),
            std::invalid_argument);
        CHECK_THROWS_AS(
            hotstuff::build_tree_placement(
                valid,
                snapshot,
                FaultContainmentPolicy{{
                    BaselineRoot{0, 0}, BaselineRoot{2, 1}}}),
            std::invalid_argument);
    }
}

TEST_CASE("the operational member maximum remains executable and bounded",
          "[t10][tree-policy][maximum][bounded][intentional-red]")
{
    const auto members = sequential_members(
        hotstuff::kMaximumTreePolicyMembers);
    const auto snapshot = make_snapshot(members, {}, 1);
    const auto input = placement_input(
        members,
        hotstuff::kMaximumTreePolicyFanout,
        1,
        std::numeric_limits<std::uint64_t>::max(),
        "operational-maximum-v1",
        hotstuff::kMaximumTreePolicyPipelineStretch);

    const auto result = hotstuff::build_tree_placement(
        input, snapshot, PerformanceOptimizationPolicy{});

    REQUIRE(result.trees().size() == 1);
    CHECK(result.trees().front().members_breadth_first.size() ==
          hotstuff::kMaximumTreePolicyMembers);
    CHECK(result.explanation().replica_roles.size() ==
          hotstuff::kMaximumTreePolicyMembers);
    check_tree_semantics(
        result,
        input,
        snapshot,
        TreePolicyKind::performance_optimization);
}

TEST_CASE("tree generation cannot mutate the frozen adaptation snapshot",
          "[t10][tree-policy][snapshot][immutability][intentional-red]")
{
    const auto members = sequential_members(31);
    const auto snapshot = make_snapshot(members, {3, 7, 15});
    const auto ranking_before = snapshot.ranking();
    const auto id_before = snapshot.snapshot_id();
    const auto cutoff_before = snapshot.evidence_cutoff();
    const auto seed_before = snapshot.seed();

    const auto containment_input = placement_input(
        members, 2, 3, 31, "fault-containment-v1");
    const FaultContainmentPolicy containment{{
        BaselineRoot{0, 10},
        BaselineRoot{1, 11},
        BaselineRoot{2, 12},
    }};
    const auto contained = hotstuff::build_tree_placement(
        containment_input, snapshot, containment);
    const auto optimized = hotstuff::build_tree_placement(
        placement_input(
            members, 2, 3, 32, "performance-optimization-v1"),
        snapshot,
        PerformanceOptimizationPolicy{});

    REQUIRE_FALSE(contained.trees().empty());
    REQUIRE_FALSE(optimized.trees().empty());
    CHECK(snapshot.ranking() == ranking_before);
    CHECK(snapshot.snapshot_id() == id_before);
    CHECK(snapshot.evidence_cutoff() == cutoff_before);
    CHECK(snapshot.seed() == seed_before);
}

TEST_CASE("tree policy source uses only deterministic local policy inputs",
          "[t10][tree-policy][source-audit][purity][intentional-red]")
{
    const auto header = read_source("include/hotstuff/tree_policy.h");
    const auto source = read_source("src/tree_policy.cpp");
    const auto implementation = header + "\n" + source;

    CHECK(header.find("const AdaptationSnapshot &") != std::string::npos);
    CHECK(header.find("FaultContainmentPolicy") != std::string::npos);
    CHECK(header.find("PerformanceOptimizationPolicy") !=
          std::string::npos);
    CHECK(header.find("TreePlacementExplanation") != std::string::npos);
    CHECK(source.find("splitmix64") != std::string::npos);
    CHECK(header.find("std::vector<ReplicaID> ineligible") ==
          std::string::npos);
    CHECK(header.find("std::set<ReplicaID> ineligible") ==
          std::string::npos);
    CHECK(header.find("ineligible_members") == std::string::npos);

    INFO("No global, library, time-seeded, or unordered iteration source");
    for (const auto *forbidden :
         {"std::shuffle",
          "random_shuffle",
          "std::rand(",
          "::rand(",
          "srand",
          "random_device",
          "unordered_map",
          "unordered_set",
          "std::time",
          "::time(",
          "gettimeofday",
          "steady_clock",
          "system_clock"})
    {
        INFO("Forbidden nondeterministic token: " << forbidden);
        CHECK(implementation.find(forbidden) == std::string::npos);
    }

    INFO("Crash schedules and performance labels cannot enter the policy");
    for (const auto *forbidden :
         {"crash", "Crash", "profile", "Profile", "pid_t"})
    {
        CHECK(implementation.find(forbidden) == std::string::npos);
    }

    INFO("The pure policy cannot own networking or consensus behavior");
    for (const auto *forbidden :
         {"PeerNetwork",
          "MsgNetwork",
          "HotStuffCore",
          "HotStuffBase",
          "AggregationContext",
          "EventContext"})
    {
        CHECK(implementation.find(forbidden) == std::string::npos);
    }
}
