#include "hotstuff/tree_policy.h"

#include <algorithm>
#include <map>
#include <set>
#include <stdexcept>
#include <utility>

namespace hotstuff
{
namespace
{

struct ValidatedInput
{
    std::vector<ReplicaID> members;
    std::map<ReplicaID, const ReplicaAdaptationResult *> scores;
    std::vector<const ReplicaAdaptationResult *> eligible;
    std::set<ReplicaID> policy_constrained;
    std::size_t first_leaf{0};
};

struct RootPlan
{
    std::vector<ReplicaID> roots;
    std::vector<RootDecision> decisions;
};

struct PlacementWork
{
    std::vector<EpochTreeDefinition> trees;
    TreePlacementExplanation explanation;
};

struct InternalCandidate
{
    ReplicaID replica_id{0};
    std::uint32_t rank{0};
    std::size_t remaining{0};
    std::optional<std::size_t> root_tree;
};

[[noreturn]] void reject(const char *message)
{
    throw std::invalid_argument(message);
}

std::size_t first_leaf_index(
    std::size_t member_count,
    std::uint32_t fanout) noexcept
{
    return member_count == 1
               ? 0
               : ((member_count - 2) / fanout) + 1;
}

ValidatedInput validate_input(
    const TreePlacementInput &input,
    const AdaptationSnapshot &snapshot)
{
    if (input.membership.empty() ||
        input.membership.size() > kMaximumTreePolicyMembers)
    {
        reject("tree policy membership is outside fixed bounds");
    }
    if (input.shape.fanout == 0 ||
        input.shape.fanout > kMaximumTreePolicyFanout ||
        input.shape.tree_count == 0 ||
        input.shape.tree_count > kMaximumTreePolicyTrees ||
        input.shape.pipeline_stretch >
            kMaximumTreePolicyPipelineStretch)
    {
        reject("tree policy shape is outside fixed bounds");
    }
    if (input.policy_version.empty() ||
        input.policy_version.size() > kMaximumTreePolicyVersionBytes)
    {
        reject("tree policy version is outside fixed bounds");
    }

    ValidatedInput validated;
    validated.members = input.membership;
    std::sort(validated.members.begin(), validated.members.end());
    if (std::adjacent_find(
            validated.members.begin(), validated.members.end()) !=
        validated.members.end())
    {
        reject("tree policy membership must be unique");
    }

    const auto &ranking = snapshot.ranking();
    if (ranking.size() != validated.members.size())
        reject("tree policy ranking must cover membership exactly");

    validated.eligible.reserve(ranking.size());
    for (std::size_t index = 0; index < ranking.size(); ++index)
    {
        const auto &score = ranking[index];
        if (score.rank != static_cast<std::uint32_t>(index))
            reject("tree policy ranking positions are inconsistent");
        if (!std::binary_search(
                validated.members.begin(),
                validated.members.end(),
                score.replica_id))
        {
            reject("tree policy ranking contains a foreign member");
        }
        if (!validated.scores.emplace(score.replica_id, &score).second)
            reject("tree policy ranking members must be unique");
        if (score.eligible)
            validated.eligible.push_back(&score);
    }
    if (validated.scores.size() != validated.members.size())
        reject("tree policy ranking is incomplete");
    if (validated.eligible.size() < input.shape.tree_count)
        reject("tree policy has insufficient eligible roots");

    validated.first_leaf = first_leaf_index(
        validated.members.size(), input.shape.fanout);
    const auto leaf_capacity =
        validated.members.size() - validated.first_leaf;
    const auto constrained_count =
        validated.members.size() - validated.eligible.size();
    if (constrained_count > leaf_capacity)
        reject("tree policy has insufficient leaf capacity");

    return validated;
}

ValidatedInput apply_explicit_constraints(
    ValidatedInput validated,
    const TreePlacementInput &input,
    const std::vector<ReplicaID> &constrained_leaves)
{
    if (constrained_leaves.empty())
        return validated;
    if (constrained_leaves.size() > validated.members.size())
        reject("tree policy constrained leaves exceed membership");

    for (const auto replica : constrained_leaves)
    {
        if (validated.scores.count(replica) == 0 ||
            !validated.policy_constrained.insert(replica).second)
        {
            reject("tree policy constrained leaves must be unique members");
        }
    }

    std::vector<const ReplicaAdaptationResult *> influential;
    influential.reserve(validated.eligible.size());
    for (const auto *score : validated.eligible)
    {
        if (validated.policy_constrained.count(score->replica_id) == 0)
            influential.push_back(score);
    }
    validated.eligible = std::move(influential);

    if (validated.eligible.size() < input.shape.tree_count)
        reject("tree policy constraints leave insufficient eligible roots");
    const auto leaf_capacity =
        validated.members.size() - validated.first_leaf;
    const auto constrained_count =
        validated.members.size() - validated.eligible.size();
    if (constrained_count > leaf_capacity)
        reject("tree policy constraints exceed leaf capacity");
    return validated;
}

ValidatedInput apply_performance_constraints(
    ValidatedInput validated,
    const TreePlacementInput &input,
    const PerformanceOptimizationPolicy &policy)
{
    return apply_explicit_constraints(
        std::move(validated), input, policy.constrained_leaves);
}

ValidatedInput apply_containment_constraints(
    ValidatedInput validated,
    const TreePlacementInput &input,
    const FaultContainmentPolicy &policy)
{
    return apply_explicit_constraints(
        std::move(validated), input, policy.constrained_leaves);
}

std::vector<std::optional<ReplicaID>> canonical_baselines(
    const TreePlacementInput &input,
    const FaultContainmentPolicy &policy)
{
    if (policy.baseline_roots.size() != input.shape.tree_count)
        reject("baseline roots must cover every tree");

    std::vector<std::optional<ReplicaID>> baselines(
        input.shape.tree_count);
    for (const auto &baseline : policy.baseline_roots)
    {
        if (baseline.tree_id >= input.shape.tree_count ||
            baselines[baseline.tree_id].has_value())
        {
            reject("baseline tree identifiers must be canonical");
        }
        baselines[baseline.tree_id] = baseline.replica_id;
    }
    for (const auto &baseline : baselines)
    {
        if (!baseline.has_value())
            reject("baseline roots must cover every tree");
    }
    return baselines;
}

const ReplicaAdaptationResult *find_score(
    const ValidatedInput &validated,
    ReplicaID replica) noexcept
{
    const auto found = validated.scores.find(replica);
    return found == validated.scores.end() ? nullptr : found->second;
}

RootPlan containment_roots(
    const TreePlacementInput &input,
    const ValidatedInput &validated,
    const FaultContainmentPolicy &policy)
{
    const auto baselines = canonical_baselines(input, policy);
    std::vector<bool> preserve(input.shape.tree_count, false);
    std::set<ReplicaID> reserved;
    for (std::size_t tree = 0; tree < baselines.size(); ++tree)
    {
        const auto replica = *baselines[tree];
        const auto *score = find_score(validated, replica);
        if (score != nullptr && score->eligible &&
            validated.policy_constrained.count(replica) == 0 &&
            reserved.insert(replica).second)
        {
            preserve[tree] = true;
        }
    }

    RootPlan plan;
    plan.roots.resize(input.shape.tree_count);
    plan.decisions.reserve(input.shape.tree_count);
    std::set<ReplicaID> chosen = reserved;
    for (std::size_t tree = 0; tree < baselines.size(); ++tree)
    {
        const auto requested = *baselines[tree];
        const auto *requested_score = find_score(validated, requested);
        ReplicaID selected = requested;
        RootSelectionReason reason =
            RootSelectionReason::preserved_eligible_baseline;

        if (!preserve[tree])
        {
            if (requested_score == nullptr)
            {
                reason = RootSelectionReason::fallback_missing_baseline;
            }
            else if (!requested_score->eligible ||
                     validated.policy_constrained.count(requested) != 0)
            {
                reason =
                    RootSelectionReason::fallback_ineligible_baseline;
            }
            else
            {
                reason =
                    RootSelectionReason::fallback_duplicate_baseline;
            }

            const auto replacement = std::find_if(
                validated.eligible.begin(),
                validated.eligible.end(),
                [&chosen](const auto *score) {
                    return chosen.count(score->replica_id) == 0;
                });
            if (replacement == validated.eligible.end())
                reject("tree policy has insufficient eligible roots");
            selected = (*replacement)->replica_id;
            chosen.insert(selected);
        }

        const auto *selected_score = find_score(validated, selected);
        if (selected_score == nullptr || !selected_score->eligible ||
            validated.policy_constrained.count(selected) != 0)
        {
            reject("tree policy selected an invalid root");
        }
        plan.roots[tree] = selected;
        plan.decisions.push_back(
            {static_cast<std::uint32_t>(tree),
             baselines[tree],
             selected,
             selected_score->rank,
             reason});
    }
    return plan;
}

RootPlan optimization_roots(
    const TreePlacementInput &input,
    const ValidatedInput &validated)
{
    RootPlan plan;
    plan.roots.reserve(input.shape.tree_count);
    plan.decisions.reserve(input.shape.tree_count);
    for (std::size_t tree = 0; tree < input.shape.tree_count; ++tree)
    {
        const auto *score = validated.eligible[tree];
        plan.roots.push_back(score->replica_id);
        plan.decisions.push_back(
            {static_cast<std::uint32_t>(tree),
             std::nullopt,
             score->replica_id,
             score->rank,
             RootSelectionReason::highest_ranked_eligible});
    }
    return plan;
}

std::size_t future_capacity(
    const InternalCandidate &candidate,
    std::size_t current_tree,
    std::size_t tree_count) noexcept
{
    auto capacity = tree_count - current_tree - 1;
    if (candidate.root_tree.has_value() &&
        *candidate.root_tree > current_tree)
    {
        --capacity;
    }
    return capacity;
}

std::vector<std::vector<ReplicaID>> assign_internal_members(
    const TreePlacementInput &input,
    const ValidatedInput &validated,
    const std::vector<ReplicaID> &roots)
{
    std::vector<std::vector<ReplicaID>> assignments(
        input.shape.tree_count);
    const auto internal_per_tree =
        validated.first_leaf == 0 ? 0 : validated.first_leaf - 1;
    if (internal_per_tree == 0)
        return assignments;

    std::map<ReplicaID, std::size_t> root_trees;
    for (std::size_t tree = 0; tree < roots.size(); ++tree)
        root_trees.emplace(roots[tree], tree);

    const auto total_slots =
        internal_per_tree * input.shape.tree_count;
    const auto base_quota = total_slots / validated.eligible.size();
    auto extras = total_slots % validated.eligible.size();
    std::vector<InternalCandidate> candidates;
    candidates.reserve(validated.eligible.size());
    for (const auto *score : validated.eligible)
    {
        const auto root = root_trees.find(score->replica_id);
        const auto root_tree =
            root == root_trees.end()
                ? std::optional<std::size_t>{}
                : std::optional<std::size_t>{root->second};
        const auto capacity = input.shape.tree_count -
                              (root_tree.has_value() ? 1 : 0);
        if (base_quota > capacity)
            reject("tree policy internal capacity is insufficient");
        candidates.push_back(
            {score->replica_id, score->rank, base_quota, root_tree});
    }
    for (auto &candidate : candidates)
    {
        if (extras == 0)
            break;
        const auto capacity = input.shape.tree_count -
                              (candidate.root_tree.has_value() ? 1 : 0);
        if (candidate.remaining < capacity)
        {
            ++candidate.remaining;
            --extras;
        }
    }
    if (extras != 0)
        reject("tree policy internal capacity is insufficient");

    for (std::size_t tree = 0;
         tree < input.shape.tree_count;
         ++tree)
    {
        std::vector<bool> selected(candidates.size(), false);
        std::size_t selected_count = 0;
        for (std::size_t index = 0; index < candidates.size(); ++index)
        {
            auto &candidate = candidates[index];
            if (candidate.replica_id == roots[tree] ||
                candidate.remaining == 0)
            {
                continue;
            }
            if (candidate.remaining >
                future_capacity(candidate, tree, input.shape.tree_count))
            {
                selected[index] = true;
                ++selected_count;
            }
        }
        if (selected_count > internal_per_tree)
            reject("tree policy internal assignments are infeasible");

        std::vector<std::size_t> available;
        available.reserve(candidates.size());
        for (std::size_t index = 0; index < candidates.size(); ++index)
        {
            const auto &candidate = candidates[index];
            if (!selected[index] &&
                candidate.replica_id != roots[tree] &&
                candidate.remaining != 0)
            {
                available.push_back(index);
            }
        }
        std::sort(
            available.begin(),
            available.end(),
            [&candidates](std::size_t left, std::size_t right) {
                const auto &left_candidate = candidates[left];
                const auto &right_candidate = candidates[right];
                if (left_candidate.remaining != right_candidate.remaining)
                {
                    return left_candidate.remaining >
                           right_candidate.remaining;
                }
                return left_candidate.rank < right_candidate.rank;
            });
        const auto needed = internal_per_tree - selected_count;
        if (available.size() < needed)
            reject("tree policy internal assignments are infeasible");
        for (std::size_t index = 0; index < needed; ++index)
        {
            selected[available[index]] = true;
            ++selected_count;
        }

        assignments[tree].reserve(internal_per_tree);
        for (std::size_t index = 0; index < candidates.size(); ++index)
        {
            if (!selected[index])
                continue;
            assignments[tree].push_back(candidates[index].replica_id);
            --candidates[index].remaining;
        }

        for (const auto &candidate : candidates)
        {
            if (candidate.remaining >
                future_capacity(candidate, tree, input.shape.tree_count))
            {
                reject("tree policy internal assignments are infeasible");
            }
        }
    }
    for (const auto &candidate : candidates)
    {
        if (candidate.remaining != 0)
            reject("tree policy internal assignments are incomplete");
    }
    return assignments;
}

std::uint64_t splitmix64(std::uint64_t &state) noexcept
{
    auto value = (state += 0x9E3779B97F4A7C15ULL);
    value = (value ^ (value >> 30)) * 0xBF58476D1CE4E5B9ULL;
    value = (value ^ (value >> 27)) * 0x94D049BB133111EBULL;
    return value ^ (value >> 31);
}

void fisher_yates_leaves(
    std::vector<ReplicaID> &leaves,
    std::uint64_t seed,
    std::size_t tree_id) noexcept
{
    auto state = seed ^
                 (0xD1B54A32D192ED03ULL *
                  static_cast<std::uint64_t>(tree_id + 1));
    for (std::size_t remaining = leaves.size(); remaining > 1; --remaining)
    {
        const auto index = static_cast<std::size_t>(
            splitmix64(state) % remaining);
        std::swap(leaves[remaining - 1], leaves[index]);
    }
}

PlacementWork build_work(
    const TreePlacementInput &input,
    const AdaptationSnapshot &snapshot,
    const ValidatedInput &validated,
    TreePolicyKind kind,
    RootPlan root_plan)
{
    const auto internal = assign_internal_members(
        input, validated, root_plan.roots);

    PlacementWork work;
    work.trees.reserve(input.shape.tree_count);
    work.explanation.schema_version = kTreePolicySchemaVersion;
    work.explanation.policy_kind = kind;
    work.explanation.policy_version = input.policy_version;
    work.explanation.generation_seed = input.generation_seed;
    work.explanation.evidence_snapshot_id = snapshot.snapshot_id();
    work.explanation.evidence_cutoff = snapshot.evidence_cutoff();
    work.explanation.root_decisions = std::move(root_plan.decisions);
    work.explanation.replica_roles.reserve(
        validated.members.size() * input.shape.tree_count);

    for (std::size_t tree = 0;
         tree < input.shape.tree_count;
         ++tree)
    {
        EpochTreeDefinition definition;
        definition.tree_id = static_cast<std::uint32_t>(tree);
        definition.fanout = input.shape.fanout;
        definition.pipeline_stretch = input.shape.pipeline_stretch;
        definition.members_breadth_first.reserve(validated.members.size());

        std::set<ReplicaID> placed;
        definition.members_breadth_first.push_back(root_plan.roots[tree]);
        placed.insert(root_plan.roots[tree]);
        for (const auto replica : internal[tree])
        {
            if (!placed.insert(replica).second)
                reject("tree policy repeated an internal member");
            definition.members_breadth_first.push_back(replica);
        }

        std::vector<ReplicaID> leaves;
        leaves.reserve(
            validated.members.size() -
            definition.members_breadth_first.size());
        for (const auto replica : validated.members)
        {
            if (placed.count(replica) == 0)
                leaves.push_back(replica);
        }
        fisher_yates_leaves(leaves, input.generation_seed, tree);
        definition.members_breadth_first.insert(
            definition.members_breadth_first.end(),
            leaves.begin(),
            leaves.end());
        if (definition.members_breadth_first.size() !=
            validated.members.size())
        {
            reject("tree policy did not place every member");
        }

        for (std::size_t position = 0;
             position < definition.members_breadth_first.size();
             ++position)
        {
            const auto replica =
                definition.members_breadth_first[position];
            const auto *score = validated.scores.at(replica);
            TreeReplicaRole role = TreeReplicaRole::leaf;
            ReplicaPlacementReason reason =
                validated.policy_constrained.count(replica) != 0
                    ? ReplicaPlacementReason::policy_constrained_leaf
                    : score->eligible
                          ? ReplicaPlacementReason::seeded_eligible_leaf
                          : ReplicaPlacementReason::
                                constrained_ineligible_leaf;
            if (position == 0)
            {
                role = TreeReplicaRole::root;
                reason = ReplicaPlacementReason::selected_root;
            }
            else if (position < validated.first_leaf)
            {
                role = TreeReplicaRole::internal;
                reason =
                    ReplicaPlacementReason::balanced_eligible_internal;
            }
            work.explanation.replica_roles.push_back(
                {static_cast<std::uint32_t>(tree),
                 replica,
                 score->rank,
                 score->classification,
                 score->eligible,
                 static_cast<std::uint32_t>(position),
                 role,
                 reason});
        }
        work.trees.push_back(std::move(definition));
    }
    return work;
}

} // namespace

TreePlacementResult build_tree_placement(
    const TreePlacementInput &input,
    const AdaptationSnapshot &snapshot,
    const FaultContainmentPolicy &policy)
{
    const auto validated = apply_containment_constraints(
        validate_input(input, snapshot), input, policy);
    auto work = build_work(
        input,
        snapshot,
        validated,
        TreePolicyKind::fault_containment,
        containment_roots(input, validated, policy));
    TreePlacementResult result;
    result.trees_ = std::move(work.trees);
    result.explanation_ = std::move(work.explanation);
    return result;
}

TreePlacementResult build_tree_placement(
    const TreePlacementInput &input,
    const AdaptationSnapshot &snapshot,
    const PerformanceOptimizationPolicy &policy)
{
    const auto validated = apply_performance_constraints(
        validate_input(input, snapshot), input, policy);
    auto work = build_work(
        input,
        snapshot,
        validated,
        TreePolicyKind::performance_optimization,
        optimization_roots(input, validated));
    TreePlacementResult result;
    result.trees_ = std::move(work.trees);
    result.explanation_ = std::move(work.explanation);
    return result;
}

} // namespace hotstuff
