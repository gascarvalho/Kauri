#include <algorithm>
#include <cstdint>
#include <fstream>
#include <iterator>
#include <string>
#include <vector>

#include "catch.hpp"
#include "hotstuff/adaptive_v2_shape_selection.h"

namespace
{

using namespace hotstuff;

uint256_t digest(const std::string &label)
{
    return DataStream(label).get_hash();
}

std::vector<ReplicaID> membership13()
{
    std::vector<ReplicaID> members;
    for (ReplicaID member = 0; member < 13; ++member)
        members.push_back(member);
    return members;
}

ShapeV1Input shape_input()
{
    ShapeV1Input input;
    input.epoch_number = 4;
    input.epoch_digest = digest("shape-v1-current-epoch");
    input.evidence_cutoff = 91;
    input.candidate_fanouts = {2, 3, 5};
    input.tree_count = 9;
    input.fixed_pipeline_stretch = 2;
    input.deterministic_seed = 0x25;
    input.selector_version = kShapeV1SelectorVersion;
    input.tie_rule = kShapeV1TieRule;
    input.reference_tree_rule = kShapeV1ReferenceTreeRule;

    const auto members = membership13();
    for (std::uint32_t tree_id = 0; tree_id < input.tree_count; ++tree_id)
    {
        input.current_trees.push_back(EpochTreeDefinition{
            tree_id, 2, 2, members, {}});
    }
    for (const auto member : members)
    {
        input.evidence.push_back(ShapeV1ReplicaEvidence{
            member,
            ResponsivenessClass::responsive,
            8,
            member == 2 ? 500'000U : 0U,
            100U});
    }
    return input;
}

const ShapeV1CandidateScore &candidate(
    const ShapeDecisionRecord &record,
    std::uint32_t fanout)
{
    const auto found = std::find_if(
        record.candidates.begin(), record.candidates.end(),
        [fanout](const auto &value) {
            return value.fanout == fanout;
        });
    REQUIRE(found != record.candidates.end());
    return *found;
}

ShapeV1CandidateScore feasible_score(
    std::uint32_t fanout,
    std::uint64_t risk,
    std::uint64_t latency,
    std::uint64_t churn)
{
    ShapeV1CandidateScore score;
    score.fanout = fanout;
    score.depth = 1;
    score.risk = risk;
    score.latency = latency;
    score.churn = churn;
    return score;
}

} // namespace

TEST_CASE("shape-v1 is deterministic and canonicalizes candidate order",
          "[shape25][shape-v1][determinism]")
{
    auto first_input = shape_input();
    const auto first = select_shape_v1(first_input);
    REQUIRE(first.status == ShapeV1Status::selected);
    CHECK(first.current_fanout == 2);
    CHECK(first.selected_fanout == 5);
    CHECK(first.applied_fanout == 5);
    CHECK(first.fixed_pipeline_stretch == 2);
    CHECK(first.predecessor_tree_count == 9);
    CHECK(first.tree_count == 9);
    CHECK(first.reference_tree_rule == kShapeV1ReferenceTreeRule);
    CHECK(first.candidates.size() == 3);
    CHECK(candidate(first, 2).depth == 3);
    CHECK(candidate(first, 3).depth == 2);
    CHECK(candidate(first, 5).depth == 2);
    CHECK(candidate(first, 5).switch_threshold_satisfied);
    CHECK(candidate(first, 5).risk < candidate(first, 2).risk);
    CHECK(candidate(first, 5).latency < candidate(first, 2).latency);
    CHECK(valid_shape_decision_record(first));

    auto reordered = shape_input();
    reordered.candidate_fanouts = {5, 2, 3, 5, 2};
    std::reverse(reordered.evidence.begin(), reordered.evidence.end());
    std::reverse(
        reordered.current_trees.begin(), reordered.current_trees.end());
    const auto second = select_shape_v1(reordered);
    REQUIRE(second.status == ShapeV1Status::selected);
    CHECK(second.selected_fanout == first.selected_fanout);
    CHECK(second.current_topology_digest == first.current_topology_digest);
    CHECK(second.evidence_digest == first.evidence_digest);
    CHECK(second.decision_digest == first.decision_digest);
    CHECK(canonical_serialize_shape_decision_record(second) ==
          canonical_serialize_shape_decision_record(first));

    auto changed_depth = first;
    ++changed_depth.candidates.front().depth;
    CHECK(canonical_serialize_shape_decision_record(changed_depth) !=
          canonical_serialize_shape_decision_record(first));
    CHECK(compute_shape_decision_digest(changed_depth) !=
          first.decision_digest);
}

TEST_CASE("shape-v1 records rejected candidates and missing evidence",
          "[shape25][shape-v1][rejection]")
{
    auto input = shape_input();
    input.current_trees.front().wait_exempt_leaves = {11};
    for (std::size_t index = 1; index < input.current_trees.size(); ++index)
        input.current_trees[index].wait_exempt_leaves = {11};
    input.candidate_fanouts = {0, 1, 2, 256};
    const auto result = select_shape_v1(input);
    REQUIRE(result.status == ShapeV1Status::selected);
    CHECK(candidate(result, 0).rejection ==
          ShapeV1CandidateRejection::fanout_out_of_range);
    CHECK(candidate(result, 0).depth == 0);
    CHECK(candidate(result, 1).rejection ==
          ShapeV1CandidateRejection::wait_exempt_would_influence);
    CHECK(candidate(result, 1).depth == 12);
    CHECK(candidate(result, 256).rejection ==
          ShapeV1CandidateRejection::fanout_out_of_range);
    CHECK(result.selected_fanout == 2);

    auto missing = shape_input();
    missing.evidence.erase(missing.evidence.begin() + 1);
    const auto incomplete = select_shape_v1(missing);
    CHECK(incomplete.status == ShapeV1Status::no_feasible_candidate);
    for (const auto &score : incomplete.candidates)
    {
        CHECK(score.rejection ==
              ShapeV1CandidateRejection::missing_influential_evidence);
    }

    auto untrusted = shape_input();
    untrusted.evidence[1].classification =
        ResponsivenessClass::insufficient_evidence;
    const auto rejected = select_shape_v1(untrusted);
    CHECK(rejected.status == ShapeV1Status::no_feasible_candidate);
    for (const auto &score : rejected.candidates)
    {
        CHECK(score.rejection ==
              ShapeV1CandidateRejection::missing_influential_evidence);
    }

    auto nonresponsive = shape_input();
    nonresponsive.evidence[1].classification =
        ResponsivenessClass::nonresponsive;
    const auto risk_scored = select_shape_v1(nonresponsive);
    CHECK(risk_scored.status == ShapeV1Status::selected);

    auto late_rejection = shape_input();
    late_rejection.candidate_fanouts = {1, 2};
    late_rejection.current_trees[1].wait_exempt_leaves = {6};
    const auto recorded = select_shape_v1(late_rejection);
    REQUIRE(recorded.status == ShapeV1Status::selected);
    CHECK(candidate(recorded, 1).rejection ==
          ShapeV1CandidateRejection::wait_exempt_would_influence);
    CHECK(candidate(recorded, 1).depth == 12);
    CHECK(candidate(recorded, 1).risk == 0);
    CHECK(candidate(recorded, 1).latency == 0);
    CHECK(candidate(recorded, 1).churn == 0);
    CHECK(valid_shape_decision_record(recorded));

    auto extra_tree = shape_input();
    extra_tree.current_trees.push_back(EpochTreeDefinition{
        9, 2, 2, membership13(), {}});
    const auto with_extra = select_shape_v1(extra_tree);
    REQUIRE(with_extra.status == ShapeV1Status::selected);
    const auto reference = select_shape_v1(shape_input());
    CHECK(with_extra.predecessor_tree_count == 10);
    CHECK(with_extra.tree_count == 9);
    CHECK(with_extra.current_topology_digest !=
          reference.current_topology_digest);
    CHECK(candidate(with_extra, 5).risk ==
          candidate(reference, 5).risk);

    extra_tree.current_trees.back().pipeline_stretch = 3;
    CHECK(select_shape_v1(extra_tree).status ==
          ShapeV1Status::invalid_input);
}

TEST_CASE("shape-v1 application changes no selector decision",
          "[shape25][shape-v1][factor-blind]")
{
    const auto selected = select_shape_v1(shape_input());
    REQUIRE(selected.status == ShapeV1Status::selected);

    auto control = selected;
    REQUIRE(finalize_shape_v1_application(control, false));
    CHECK(control.selected_fanout == selected.selected_fanout);
    CHECK(control.applied_fanout == control.current_fanout);
    CHECK(control.decision_digest != selected.decision_digest);
    CHECK(valid_shape_decision_record(control));

    auto adaptive = selected;
    REQUIRE(finalize_shape_v1_application(adaptive, true));
    CHECK(adaptive.selected_fanout == selected.selected_fanout);
    CHECK(adaptive.applied_fanout == adaptive.selected_fanout);
    CHECK(adaptive.decision_digest == selected.decision_digest);
    CHECK(valid_shape_decision_record(adaptive));

    const auto independently_recomputed = select_shape_v1(shape_input());
    CHECK(independently_recomputed.decision_digest ==
          selected.decision_digest);
}

TEST_CASE("shape-v1 rejects forged canonical choice fields",
          "[shape25][shape-v1][audit-integrity]")
{
    const auto selected = select_shape_v1(shape_input());
    REQUIRE(selected.status == ShapeV1Status::selected);
    REQUIRE(selected.current_fanout == 2);
    REQUIRE(selected.selected_fanout == 5);
    REQUIRE(candidate(selected, 3).switch_threshold_satisfied);

    auto forged_winner = selected;
    forged_winner.selected_fanout = 3;
    forged_winner.applied_fanout = 3;
    forged_winner.decision_digest =
        compute_shape_decision_digest(forged_winner);
    CHECK_FALSE(valid_shape_decision_record(forged_winner));

    auto forged_threshold = selected;
    auto current = std::find_if(
        forged_threshold.candidates.begin(),
        forged_threshold.candidates.end(),
        [&forged_threshold](const auto &score) {
            return score.fanout == forged_threshold.current_fanout;
        });
    REQUIRE(current != forged_threshold.candidates.end());
    current->switch_threshold_satisfied = true;
    forged_threshold.decision_digest =
        compute_shape_decision_digest(forged_threshold);
    CHECK_FALSE(valid_shape_decision_record(forged_threshold));

    auto suppressed_threshold = selected;
    auto winner = std::find_if(
        suppressed_threshold.candidates.begin(),
        suppressed_threshold.candidates.end(),
        [&suppressed_threshold](const auto &score) {
            return score.fanout ==
                suppressed_threshold.selected_fanout;
        });
    REQUIRE(winner != suppressed_threshold.candidates.end());
    REQUIRE(winner->switch_threshold_satisfied);
    winner->switch_threshold_satisfied = false;
    suppressed_threshold.decision_digest =
        compute_shape_decision_digest(suppressed_threshold);
    CHECK_FALSE(valid_shape_decision_record(suppressed_threshold));
}

TEST_CASE("shape-v1 enforces the exact five-percent switch boundary",
          "[shape25][shape-v1][threshold]")
{
    const auto current = feasible_score(2, 100, 100, 0);

    SECTION("just below five percent in either metric retains current")
    {
        const std::vector<ShapeV1CandidateScore> scores{
            current,
            feasible_score(3, 100, 96, 1),
            feasible_score(5, 96, 100, 1)};
        const auto choice = recompute_shape_v1_canonical_choice(
            scores, current.fanout);
        REQUIRE(choice.has_value());
        CHECK(choice->selected_fanout == current.fanout);
        CHECK(choice->switch_thresholds ==
              std::vector<bool>{false, false, false});
    }

    SECTION("exactly five percent switches on latency alone")
    {
        const std::vector<ShapeV1CandidateScore> scores{
            current, feasible_score(3, 100, 95, 1)};
        const auto choice = recompute_shape_v1_canonical_choice(
            scores, current.fanout);
        REQUIRE(choice.has_value());
        CHECK(choice->selected_fanout == 3);
        CHECK(choice->switch_thresholds ==
              std::vector<bool>{false, true});
    }

    SECTION("exactly five percent switches on risk alone")
    {
        const std::vector<ShapeV1CandidateScore> scores{
            current, feasible_score(3, 95, 100, 1)};
        const auto choice = recompute_shape_v1_canonical_choice(
            scores, current.fanout);
        REQUIRE(choice.has_value());
        CHECK(choice->selected_fanout == 3);
        CHECK(choice->switch_thresholds ==
              std::vector<bool>{false, true});
    }

    SECTION("an improvement cannot hide regression in the other metric")
    {
        const std::vector<ShapeV1CandidateScore> scores{
            current,
            feasible_score(3, 101, 90, 1),
            feasible_score(5, 90, 101, 1)};
        const auto choice = recompute_shape_v1_canonical_choice(
            scores, current.fanout);
        REQUIRE(choice.has_value());
        CHECK(choice->selected_fanout == current.fanout);
        CHECK(choice->switch_thresholds ==
              std::vector<bool>{false, false, false});
    }
}

TEST_CASE("shape-v1 applies every canonical tie-break stage",
          "[shape25][shape-v1][tie-break]")
{
    const auto current = feasible_score(2, 1'000, 1'000, 0);

    SECTION("latency precedes risk and churn")
    {
        const std::vector<ShapeV1CandidateScore> scores{
            current,
            feasible_score(3, 700, 800, 100),
            feasible_score(5, 100, 900, 0)};
        const auto choice = recompute_shape_v1_canonical_choice(
            scores, current.fanout);
        REQUIRE(choice.has_value());
        CHECK(choice->selected_fanout == 3);
    }

    SECTION("risk breaks an exact latency tie")
    {
        const std::vector<ShapeV1CandidateScore> scores{
            current,
            feasible_score(3, 700, 800, 0),
            feasible_score(5, 600, 800, 100)};
        const auto choice = recompute_shape_v1_canonical_choice(
            scores, current.fanout);
        REQUIRE(choice.has_value());
        CHECK(choice->selected_fanout == 5);
    }

    SECTION("churn breaks an exact latency and risk tie")
    {
        const std::vector<ShapeV1CandidateScore> scores{
            current,
            feasible_score(3, 600, 800, 5),
            feasible_score(5, 600, 800, 4)};
        const auto choice = recompute_shape_v1_canonical_choice(
            scores, current.fanout);
        REQUIRE(choice.has_value());
        CHECK(choice->selected_fanout == 5);
    }

    SECTION("canonical fanout breaks a remaining alternative tie")
    {
        const std::vector<ShapeV1CandidateScore> scores{
            current,
            feasible_score(3, 600, 800, 4),
            feasible_score(5, 600, 800, 4)};
        const auto choice = recompute_shape_v1_canonical_choice(
            scores, current.fanout);
        REQUIRE(choice.has_value());
        CHECK(choice->selected_fanout == 3);
    }

    SECTION("current remains canonical when no alternative qualifies")
    {
        const std::vector<ShapeV1CandidateScore> scores{
            current,
            feasible_score(3, 1'000, 951, 0),
            feasible_score(5, 951, 1'000, 0)};
        const auto choice = recompute_shape_v1_canonical_choice(
            scores, current.fanout);
        REQUIRE(choice.has_value());
        CHECK(choice->selected_fanout == current.fanout);
        CHECK(choice->switch_thresholds ==
              std::vector<bool>{false, false, false});
    }
}

TEST_CASE("shape-v1 startup contract freezes candidates and hides experiment truth",
          "[shape25][shape-v1][api]")
{
    const ShapeV1Config config;
    CHECK(config.candidate_fanouts ==
          std::vector<std::uint32_t>{2, 3, 5});
    CHECK(config.fixed_pipeline_stretch == 2);
    CHECK(config.reference_tree_rule ==
          kShapeV1ReferenceTreeRule);

    auto wrong_reference_rule = shape_input();
    wrong_reference_rule.reference_tree_rule =
        "outcome-selected-reference-v1";
    CHECK(select_shape_v1(wrong_reference_rule).status ==
          ShapeV1Status::invalid_input);

    const std::string path =
        std::string(KAURI_PROJECT_SOURCE_DIR) +
        "/include/hotstuff/adaptive_v2_shape_selection.h";
    std::ifstream source(path);
    REQUIRE(source.good());
    const std::string header{
        std::istreambuf_iterator<char>(source),
        std::istreambuf_iterator<char>()};
    for (const auto *forbidden : {
             "fault_ids",
             "fault_schedule",
             "performance_label",
             "throughput",
             "arm_identity",
             "orchestrator_truth",
             "post_cutoff"})
    {
        CHECK(header.find(forbidden) == std::string::npos);
    }
}
