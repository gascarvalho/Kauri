#include "hotstuff/adaptive_v2_epoch_factory.h"

#include "hotstuff/epoch_activation.h"

#include <algorithm>
#include <limits>
#include <optional>
#include <set>
#include <stdexcept>
#include <utility>
#include <vector>

namespace hotstuff
{
namespace
{

AdaptiveV2EpochFactoryResult rejected(
    AdaptiveV2EpochFactoryStatus status) noexcept
{
    return {status, nullptr};
}

bool supported_current_schema(const EpochDefinition &current) noexcept
{
    return current.schema_version() == kEpochDefinitionSchemaVersionV1 ||
           current.schema_version() == kEpochDefinitionSchemaVersionV2;
}

bool sort_unique(std::vector<ReplicaID> &replicas)
{
    std::sort(replicas.begin(), replicas.end());
    return std::adjacent_find(replicas.begin(), replicas.end()) ==
           replicas.end();
}

std::optional<std::vector<ReplicaID>> current_membership(
    const EpochDefinition &current)
{
    if (!supported_current_schema(current) || current.trees().empty() ||
        current.canonical_serialization().empty() ||
        DataStream(current.canonical_serialization()).get_hash() !=
            current.epoch_digest())
    {
        return std::nullopt;
    }

    auto canonical = current.trees().front().members_breadth_first;
    if (canonical.empty() || !sort_unique(canonical) ||
        canonical_membership_digest(canonical) !=
            current.membership_digest())
    {
        return std::nullopt;
    }

    for (const auto &tree : current.trees())
    {
        auto members = tree.members_breadth_first;
        if (!sort_unique(members) || members != canonical)
            return std::nullopt;
    }
    return canonical;
}

bool exact_selection_metadata(
    const AdaptiveV2SelectionResult &selection,
    const ByzantineQuorum &quorum) noexcept
{
    const auto &metadata = selection.metadata;
    return metadata.replica_count == quorum.replica_count &&
           metadata.fault_threshold == quorum.fault_threshold &&
           metadata.quorum == quorum.quorum &&
           metadata.required_nonresponsive != 0 &&
           metadata.required_nonresponsive <= quorum.fault_threshold &&
           metadata.required_qualifying_reporters ==
               quorum.fault_threshold + 1U &&
           metadata.minimum_timeouts_per_reporter != 0 &&
           metadata.minimum_score_drop != 0 &&
           metadata.baseline_cutoff < metadata.evidence_cutoff;
}

const ReplicaAdaptationResult *snapshot_entry(
    const AdaptationSnapshot &snapshot,
    ReplicaID replica_id) noexcept
{
    const auto found = std::find_if(
        snapshot.ranking().begin(),
        snapshot.ranking().end(),
        [replica_id](const auto &entry) {
            return entry.replica_id == replica_id;
        });
    return found == snapshot.ranking().end() ? nullptr : &*found;
}

bool valid_guarded_candidates(
    const AdaptiveV2SelectionResult &selection,
    const std::set<ReplicaID> &membership,
    const ByzantineQuorum &quorum)
{
    if (selection.eligible_candidates.size() <
        selection.metadata.required_nonresponsive)
        return false;

    std::set<ReplicaID> candidate_ids;
    for (const auto &candidate : selection.eligible_candidates)
    {
        if (membership.count(candidate.replica_id) == 0 ||
            !candidate_ids.insert(candidate.replica_id).second ||
            candidate.snapshot_classification !=
                ResponsivenessClass::nonresponsive ||
            !candidate.snapshot_nonresponsive ||
            !candidate.score_drop_satisfied ||
            !candidate.reporter_guard_satisfied ||
            !candidate.guarded_eligible ||
            candidate.qualifying_reporters.size() <
                selection.metadata.required_qualifying_reporters ||
            candidate.guard_drawdown >
                -static_cast<std::int64_t>(
                    selection.metadata.minimum_score_drop) ||
            candidate.guard_drawdown > 0 ||
            (candidate.guard_drawdown < 0 &&
             static_cast<std::uint64_t>(
                 -(candidate.guard_drawdown + 1)) +
                     1U >
                 candidate.total_uncompensated_timeouts) ||
            static_cast<std::int64_t>(candidate.current_score) -
                    static_cast<std::int64_t>(candidate.baseline_score) !=
                candidate.baseline_score_delta)
        {
            return false;
        }

        auto reporters = candidate.qualifying_reporters;
        if (!sort_unique(reporters))
            return false;
        for (const auto reporter : reporters)
        {
            if (membership.count(reporter) == 0)
                return false;
        }
    }

    for (std::size_t index = 0;
         index < selection.metadata.required_nonresponsive;
         ++index)
    {
        if (selection.eligible_candidates[index].replica_id !=
            selection.selected_replicas[index])
        {
            return false;
        }
    }
    return true;
}

std::size_t first_leaf_index(
    std::size_t member_count,
    std::uint32_t fanout) noexcept;

enum class ConsensusConstraintStatus : std::uint8_t
{
    empty = 1,
    exact,
    invalid,
};

struct ConsensusConstraints
{
    ConsensusConstraintStatus status{
        ConsensusConstraintStatus::invalid};
    std::vector<ReplicaID> replicas;
};

ConsensusConstraints current_consensus_constraints(
    const EpochDefinition &current,
    const std::set<ReplicaID> &membership,
    const ByzantineQuorum &quorum)
{
    if (current.trees().empty())
        return {};

    const auto canonical =
        current.trees().front().wait_exempt_leaves;
    if (canonical.empty())
    {
        for (const auto &tree : current.trees())
        {
            if (!tree.wait_exempt_leaves.empty())
                return {};
        }
        return {ConsensusConstraintStatus::empty, {}};
    }

    if (canonical.empty() ||
        canonical.size() > quorum.fault_threshold ||
        !std::is_sorted(canonical.begin(), canonical.end()) ||
        std::adjacent_find(canonical.begin(), canonical.end()) !=
            canonical.end())
    {
        return {};
    }
    for (const auto replica_id : canonical)
    {
        if (membership.count(replica_id) == 0)
            return {};
    }

    for (const auto &tree : current.trees())
    {
        if (tree.fanout == 0 ||
            tree.wait_exempt_leaves != canonical)
        {
            return {};
        }
        const auto leaf_start = first_leaf_index(
            tree.members_breadth_first.size(), tree.fanout);
        for (const auto replica_id : canonical)
        {
            const auto found = std::find(
                tree.members_breadth_first.begin(),
                tree.members_breadth_first.end(),
                replica_id);
            if (found == tree.members_breadth_first.end() ||
                static_cast<std::size_t>(std::distance(
                    tree.members_breadth_first.begin(), found)) <
                    leaf_start)
            {
                return {};
            }
        }
    }
    return {ConsensusConstraintStatus::exact, canonical};
}

AdaptiveV2EpochFactoryStatus validate_selection(
    const EpochDefinition &current,
    const AdaptiveV2SelectionResult &selection,
    const AdaptiveV2TransitionPolicy &transition_policy,
    const std::vector<ReplicaID> &membership,
    const ByzantineQuorum &quorum,
    std::vector<ReplicaID> &canonical_wait_exempt,
    std::vector<ReplicaID> &snapshot_roots)
{
    if (selection.status != AdaptiveV2SelectionStatus::selected ||
        selection.snapshot == nullptr ||
        !exact_selection_metadata(selection, quorum) ||
        selection.selected_replicas.size() !=
            selection.metadata.required_nonresponsive)
    {
        return AdaptiveV2EpochFactoryStatus::invalid_selection;
    }

    const auto &snapshot = *selection.snapshot;
    if (snapshot.schema_version() != kAdaptationSchemaVersion ||
        snapshot.epoch().epoch_number != current.epoch_number() ||
        snapshot.epoch().epoch_digest != current.epoch_digest() ||
        snapshot.evidence_cutoff() !=
            selection.metadata.evidence_cutoff)
    {
        return AdaptiveV2EpochFactoryStatus::epoch_mismatch;
    }

    canonical_wait_exempt = selection.selected_replicas;
    if (!sort_unique(canonical_wait_exempt))
        return AdaptiveV2EpochFactoryStatus::invalid_selection;

    const std::set<ReplicaID> member_set(
        membership.begin(), membership.end());
    const auto current_constraints = current_consensus_constraints(
        current, member_set, quorum);
    if (current_constraints.status ==
        ConsensusConstraintStatus::invalid)
    {
        return AdaptiveV2EpochFactoryStatus::invalid_selection;
    }

    switch (selection.constraint_basis)
    {
    case AdaptiveV2SelectionConstraintBasis::guarded_evidence:
        if (!valid_guarded_candidates(selection, member_set, quorum) ||
            (transition_policy.intent ==
                 TreePolicyKind::performance_optimization &&
             current_constraints.status !=
                 ConsensusConstraintStatus::empty))
        {
            return AdaptiveV2EpochFactoryStatus::invalid_selection;
        }
        break;
    case AdaptiveV2SelectionConstraintBasis::
        inherited_consensus_wait_exempt:
    {
        if (!selection.eligible_candidates.empty() ||
            current_constraints.status !=
                ConsensusConstraintStatus::exact)
        {
            return AdaptiveV2EpochFactoryStatus::invalid_selection;
        }
        if (current_constraints.replicas != canonical_wait_exempt ||
            selection.selected_replicas != canonical_wait_exempt)
        {
            return AdaptiveV2EpochFactoryStatus::invalid_selection;
        }
        break;
    }
    default:
        return AdaptiveV2EpochFactoryStatus::invalid_selection;
    }

    const std::set<ReplicaID> selected(
        canonical_wait_exempt.begin(), canonical_wait_exempt.end());
    const auto &ranking = snapshot.ranking();
    if (ranking.size() != membership.size())
        return AdaptiveV2EpochFactoryStatus::invalid_selection;

    std::set<ReplicaID> ranked;
    snapshot_roots.clear();
    snapshot_roots.reserve(quorum.quorum);
    for (std::size_t index = 0; index < ranking.size(); ++index)
    {
        const auto &entry = ranking[index];
        if (entry.rank != static_cast<std::uint32_t>(index) ||
            member_set.count(entry.replica_id) == 0 ||
            !ranked.insert(entry.replica_id).second ||
            (entry.classification != ResponsivenessClass::responsive &&
             entry.classification !=
                 ResponsivenessClass::insufficient_evidence &&
             entry.classification !=
                 ResponsivenessClass::nonresponsive) ||
            entry.eligible !=
                (entry.classification ==
                 ResponsivenessClass::responsive))
        {
            return AdaptiveV2EpochFactoryStatus::invalid_selection;
        }

        if (selected.count(entry.replica_id) != 0)
        {
            if (selection.constraint_basis ==
                    AdaptiveV2SelectionConstraintBasis::
                        guarded_evidence &&
                (entry.classification !=
                     ResponsivenessClass::nonresponsive ||
                 entry.eligible))
            {
                return AdaptiveV2EpochFactoryStatus::invalid_selection;
            }
            continue;
        }

        if (entry.classification != ResponsivenessClass::responsive ||
            !entry.eligible)
        {
            return AdaptiveV2EpochFactoryStatus::root_mismatch;
        }
        if (snapshot_roots.size() < quorum.quorum)
            snapshot_roots.push_back(entry.replica_id);
    }

    if (selection.constraint_basis ==
        AdaptiveV2SelectionConstraintBasis::guarded_evidence)
    {
        for (const auto selected_replica : canonical_wait_exempt)
        {
            const auto *entry = snapshot_entry(
                snapshot, selected_replica);
            if (entry == nullptr ||
                entry->classification !=
                    ResponsivenessClass::nonresponsive ||
                entry->eligible)
            {
                return AdaptiveV2EpochFactoryStatus::invalid_selection;
            }
        }
    }

    if (snapshot_roots.size() != quorum.quorum ||
        selection.eligible_roots != snapshot_roots)
    {
        return AdaptiveV2EpochFactoryStatus::root_mismatch;
    }
    return AdaptiveV2EpochFactoryStatus::success;
}

bool bundle_counts_fit(
    const TreePlacementInput &placement,
    std::size_t member_count,
    std::size_t wait_exempt_count,
    const AdaptationSnapshot &snapshot,
    const EpochChangeBundleLimits &limits) noexcept
{
    const auto &definition_limits = limits.definition_limits;
    return limits.maximum_payload_bytes != 0 &&
           limits.maximum_command_bytes != 0 &&
           definition_limits.maximum_payload_bytes != 0 &&
           definition_limits.maximum_trees != 0 &&
           definition_limits.maximum_members_per_tree != 0 &&
           definition_limits.maximum_string_bytes != 0 &&
           definition_limits.maximum_wait_exempt_leaves_per_tree != 0 &&
           placement.shape.tree_count <=
               definition_limits.maximum_trees &&
           member_count <=
               definition_limits.maximum_members_per_tree &&
           wait_exempt_count <=
               definition_limits.maximum_wait_exempt_leaves_per_tree &&
           placement.policy_version.size() <=
               definition_limits.maximum_string_bytes &&
           snapshot.snapshot_id().size() <=
               definition_limits.maximum_string_bytes;
}

std::size_t first_leaf_index(
    std::size_t member_count,
    std::uint32_t fanout) noexcept
{
    return member_count == 1
               ? 0
               : ((member_count - 2) / fanout) + 1;
}

std::optional<std::vector<ReplicaID>> exact_membership_complement(
    const std::vector<ReplicaID> &membership,
    const std::vector<ReplicaID> &roots)
{
    const std::set<ReplicaID> members(
        membership.begin(), membership.end());
    if (members.size() != membership.size())
        return std::nullopt;

    std::set<ReplicaID> root_set;
    for (const auto root : roots)
    {
        if (members.count(root) == 0 ||
            !root_set.insert(root).second)
        {
            return std::nullopt;
        }
    }

    std::vector<ReplicaID> complement;
    complement.reserve(membership.size() - root_set.size());
    for (const auto member : membership)
    {
        if (root_set.count(member) == 0)
            complement.push_back(member);
    }
    if (root_set.size() + complement.size() != membership.size())
        return std::nullopt;
    return complement;
}

bool exact_optimized_placement(
    const TreePlacementResult &placement,
    const TreePlacementInput &input,
    const AdaptationSnapshot &snapshot,
    const std::vector<ReplicaID> &membership,
    const std::vector<ReplicaID> &roots,
    const std::vector<ReplicaID> &constrained_leaves)
{
    const auto expected_constrained = exact_membership_complement(
        membership, roots);
    if (!expected_constrained.has_value() ||
        *expected_constrained != constrained_leaves)
    {
        return false;
    }
    const std::set<ReplicaID> constrained(
        constrained_leaves.begin(), constrained_leaves.end());

    const auto &explanation = placement.explanation();
    if (explanation.policy_kind !=
            TreePolicyKind::performance_optimization ||
        explanation.policy_version != input.policy_version ||
        explanation.generation_seed != input.generation_seed ||
        explanation.evidence_snapshot_id != snapshot.snapshot_id() ||
        explanation.evidence_cutoff != snapshot.evidence_cutoff() ||
        explanation.root_decisions.size() != roots.size() ||
        placement.trees().size() != roots.size())
    {
        return false;
    }

    for (std::size_t index = 0; index < roots.size(); ++index)
    {
        const auto &root = explanation.root_decisions[index];
        const auto &tree = placement.trees()[index];
        if (root.tree_id != index ||
            root.chosen_root != roots[index] ||
            root.requested_baseline_root.has_value() ||
            root.reason != RootSelectionReason::highest_ranked_eligible ||
            tree.tree_id != index ||
            tree.members_breadth_first.empty() ||
            tree.members_breadth_first.front() != roots[index] ||
            constrained.count(roots[index]) != 0 ||
            !tree.wait_exempt_leaves.empty())
        {
            return false;
        }

        auto canonical_tree_members = tree.members_breadth_first;
        if (!sort_unique(canonical_tree_members) ||
            canonical_tree_members != membership)
        {
            return false;
        }

        const auto leaf_start = first_leaf_index(
            tree.members_breadth_first.size(), tree.fanout);
        for (const auto constrained_leaf : constrained_leaves)
        {
            const auto position = std::find(
                tree.members_breadth_first.begin(),
                tree.members_breadth_first.end(),
                constrained_leaf);
            if (position == tree.members_breadth_first.end() ||
                static_cast<std::size_t>(std::distance(
                    tree.members_breadth_first.begin(), position)) <
                    leaf_start)
            {
                return false;
            }
        }
    }
    return true;
}

std::optional<std::vector<ReplicaID>> canonical_baseline_roots(
    const AdaptiveV2TransitionPolicy &policy,
    std::uint32_t tree_count)
{
    if (policy.containment_baseline_roots.size() != tree_count)
        return std::nullopt;

    std::vector<std::optional<ReplicaID>> by_tree(tree_count);
    for (const auto &baseline : policy.containment_baseline_roots)
    {
        if (baseline.tree_id >= tree_count ||
            by_tree[baseline.tree_id].has_value())
        {
            return std::nullopt;
        }
        by_tree[baseline.tree_id] = baseline.replica_id;
    }

    std::vector<ReplicaID> roots;
    roots.reserve(tree_count);
    for (const auto &baseline : by_tree)
    {
        if (!baseline.has_value())
            return std::nullopt;
        roots.push_back(*baseline);
    }
    return roots;
}

bool exact_containment_placement(
    const TreePlacementResult &placement,
    const AdaptiveV2TransitionPolicy &policy,
    const TreePlacementInput &input,
    const AdaptationSnapshot &snapshot,
    const std::vector<ReplicaID> &membership,
    const std::vector<ReplicaID> &selected)
{
    const auto baselines = canonical_baseline_roots(
        policy, input.shape.tree_count);
    if (!baselines.has_value())
        return false;

    const auto &explanation = placement.explanation();
    if (explanation.policy_kind != TreePolicyKind::fault_containment ||
        explanation.policy_version != input.policy_version ||
        explanation.generation_seed != input.generation_seed ||
        explanation.evidence_snapshot_id != snapshot.snapshot_id() ||
        explanation.evidence_cutoff != snapshot.evidence_cutoff() ||
        explanation.root_decisions.size() != baselines->size() ||
        placement.trees().size() != baselines->size())
    {
        return false;
    }

    const std::set<ReplicaID> member_set(
        membership.begin(), membership.end());
    std::vector<ReplicaID> eligible_roots;
    eligible_roots.reserve(snapshot.ranking().size());
    for (const auto &entry : snapshot.ranking())
    {
        if (entry.eligible)
            eligible_roots.push_back(entry.replica_id);
    }
    const std::set<ReplicaID> eligible(
        eligible_roots.begin(), eligible_roots.end());
    std::vector<bool> preserve(baselines->size(), false);
    std::set<ReplicaID> reserved;
    for (std::size_t tree = 0; tree < baselines->size(); ++tree)
    {
        const auto requested = (*baselines)[tree];
        if (eligible.count(requested) != 0 &&
            reserved.insert(requested).second)
        {
            preserve[tree] = true;
        }
    }

    std::set<ReplicaID> chosen = reserved;
    for (std::size_t index = 0; index < baselines->size(); ++index)
    {
        const auto requested = (*baselines)[index];
        auto expected = requested;
        auto reason = RootSelectionReason::preserved_eligible_baseline;
        if (!preserve[index])
        {
            if (member_set.count(requested) == 0)
            {
                reason = RootSelectionReason::fallback_missing_baseline;
            }
            else if (eligible.count(requested) == 0)
            {
                reason = RootSelectionReason::fallback_ineligible_baseline;
            }
            else
            {
                reason = RootSelectionReason::fallback_duplicate_baseline;
            }

            const auto replacement = std::find_if(
                eligible_roots.begin(),
                eligible_roots.end(),
                [&chosen](ReplicaID candidate) {
                    return chosen.count(candidate) == 0;
                });
            if (replacement == eligible_roots.end())
                return false;
            expected = *replacement;
            chosen.insert(expected);
        }

        const auto &decision = explanation.root_decisions[index];
        const auto &tree = placement.trees()[index];
        const auto *score = snapshot_entry(snapshot, expected);
        if (score == nullptr || !score->eligible ||
            decision.tree_id != index ||
            decision.requested_baseline_root != requested ||
            decision.chosen_root != expected ||
            decision.chosen_rank != score->rank ||
            decision.reason != reason || tree.tree_id != index ||
            tree.members_breadth_first.empty() ||
            tree.members_breadth_first.front() != expected ||
            !tree.wait_exempt_leaves.empty())
        {
            return false;
        }

        auto canonical_tree_members = tree.members_breadth_first;
        if (!sort_unique(canonical_tree_members) ||
            canonical_tree_members != membership)
        {
            return false;
        }

        const auto leaf_start = first_leaf_index(
            tree.members_breadth_first.size(), tree.fanout);
        for (const auto selected_replica : selected)
        {
            const auto position = std::find(
                tree.members_breadth_first.begin(),
                tree.members_breadth_first.end(),
                selected_replica);
            if (position == tree.members_breadth_first.end() ||
                static_cast<std::size_t>(std::distance(
                    tree.members_breadth_first.begin(), position)) <
                    leaf_start)
            {
                return false;
            }
        }
    }
    return true;
}

AdaptiveV2EpochFactoryResult build_validated(
    const EpochDefinition &current,
    const AdaptiveV2SelectionResult &selection,
    const AdaptiveV2TransitionPolicy &transition_policy,
    const TreePlacementInput &placement_input,
    std::uint64_t activation_delay_blocks,
    EpochChangeIssuerId issuer_id,
    const PrivKeySecp256k1 &issuer_private_key,
    const EpochChangeBundleLimits &bundle_limits)
{
    const auto current_members = current_membership(current);
    if (!current_members.has_value())
    {
        return rejected(
            AdaptiveV2EpochFactoryStatus::invalid_current_epoch);
    }
    const auto successor_epoch = checked_successor_epoch(
        current.epoch_number());
    if (!successor_epoch.has_value())
    {
        return rejected(
            AdaptiveV2EpochFactoryStatus::epoch_number_exhausted);
    }

    auto requested_members = placement_input.membership;
    if (!sort_unique(requested_members) ||
        requested_members != *current_members ||
        canonical_membership_digest(requested_members) !=
            current.membership_digest())
    {
        return rejected(
            AdaptiveV2EpochFactoryStatus::membership_mismatch);
    }

    const auto quorum = derive_byzantine_quorum(
        current_members->size());
    if (!quorum.has_value() || quorum->fault_threshold == 0)
    {
        return rejected(
            AdaptiveV2EpochFactoryStatus::invalid_current_epoch);
    }

    std::vector<ReplicaID> selected;
    std::vector<ReplicaID> roots;
    const auto selection_status = validate_selection(
        current,
        selection,
        transition_policy,
        *current_members,
        *quorum,
        selected,
        roots);
    if (selection_status != AdaptiveV2EpochFactoryStatus::success)
        return rejected(selection_status);

    std::vector<ReplicaID> optimization_constrained_leaves;
    if (transition_policy.intent ==
        TreePolicyKind::performance_optimization)
    {
        const auto complement = exact_membership_complement(
            *current_members, roots);
        if (!complement.has_value())
        {
            return rejected(
                AdaptiveV2EpochFactoryStatus::root_mismatch);
        }
        optimization_constrained_leaves = *complement;
    }

    if (placement_input.shape.tree_count != quorum->quorum)
    {
        return rejected(
            AdaptiveV2EpochFactoryStatus::tree_count_mismatch);
    }
    if (activation_delay_blocks == 0)
    {
        return rejected(
            AdaptiveV2EpochFactoryStatus::invalid_activation_delay);
    }

    if (current_members->size() > kMaximumTreePolicyMembers ||
        placement_input.shape.tree_count > kMaximumTreePolicyTrees ||
        placement_input.shape.fanout > kMaximumTreePolicyFanout ||
        placement_input.shape.pipeline_stretch >
            kMaximumTreePolicyPipelineStretch ||
        placement_input.policy_version.size() >
            kMaximumTreePolicyVersionBytes ||
        !bundle_counts_fit(
            placement_input,
            current_members->size(),
            selected.size(),
            *selection.snapshot,
            bundle_limits))
    {
        return rejected(
            AdaptiveV2EpochFactoryStatus::capacity_exceeded);
    }
    if (placement_input.shape.fanout == 0 ||
        placement_input.policy_version.empty())
    {
        return rejected(
            AdaptiveV2EpochFactoryStatus::placement_failed);
    }

    const auto leaf_start = first_leaf_index(
        current_members->size(), placement_input.shape.fanout);
    const auto constrained_leaf_count =
        transition_policy.intent ==
                TreePolicyKind::performance_optimization
            ? optimization_constrained_leaves.size()
            : selected.size();
    if (constrained_leaf_count >
        current_members->size() - leaf_start)
    {
        return rejected(
            AdaptiveV2EpochFactoryStatus::insufficient_leaf_capacity);
    }

    std::optional<TreePlacementResult> placement;
    try
    {
        switch (transition_policy.intent)
        {
        case TreePolicyKind::fault_containment:
            placement.emplace(build_tree_placement(
                placement_input,
                *selection.snapshot,
                FaultContainmentPolicy{
                    transition_policy.containment_baseline_roots}));
            break;
        case TreePolicyKind::performance_optimization:
            if (!transition_policy.containment_baseline_roots.empty())
            {
                return rejected(
                    AdaptiveV2EpochFactoryStatus::placement_failed);
            }
            placement.emplace(build_tree_placement(
                placement_input,
                *selection.snapshot,
                PerformanceOptimizationPolicy{
                    optimization_constrained_leaves}));
            break;
        default:
            return rejected(
                AdaptiveV2EpochFactoryStatus::placement_failed);
        }
    }
    catch (const std::length_error &)
    {
        return rejected(
            AdaptiveV2EpochFactoryStatus::capacity_exceeded);
    }
    catch (const std::bad_alloc &)
    {
        return rejected(
            AdaptiveV2EpochFactoryStatus::capacity_exceeded);
    }
    catch (...)
    {
        return rejected(
            AdaptiveV2EpochFactoryStatus::placement_failed);
    }

    const auto exact_placement =
        transition_policy.intent == TreePolicyKind::fault_containment
            ? exact_containment_placement(
                  *placement,
                  transition_policy,
                  placement_input,
                  *selection.snapshot,
                  *current_members,
                  selected)
            : exact_optimized_placement(
                  *placement,
                  placement_input,
                  *selection.snapshot,
                  *current_members,
                  roots,
                  optimization_constrained_leaves);
    if (!exact_placement)
    {
        return rejected(
            AdaptiveV2EpochFactoryStatus::placement_failed);
    }

    EpochDefinitionInput successor;
    successor.schema_version = kEpochDefinitionSchemaVersionV2;
    successor.epoch_number = *successor_epoch;
    successor.previous_epoch_digest = current.epoch_digest();
    successor.membership_digest = current.membership_digest();
    successor.trees = placement->trees();
    for (auto &tree : successor.trees)
        tree.wait_exempt_leaves = selected;
    successor.activation_height = 0;
    successor.generation_seed = placement_input.generation_seed;
    successor.policy_version = placement_input.policy_version;
    successor.evidence_snapshot_id =
        selection.snapshot->snapshot_id();
    successor.evidence_cutoff =
        selection.snapshot->evidence_cutoff();
    successor.epoch_digest.reset();

    const auto successor_digest = compute_epoch_digest(successor);
    successor.epoch_digest = successor_digest;

    std::optional<AuthorizedEpochChange> command;
    try
    {
        command.emplace(authorize_epoch_change(
            EpochChangePayload{
                successor.epoch_number,
                successor.previous_epoch_digest,
                successor_digest,
                activation_delay_blocks},
            issuer_id,
            issuer_private_key));
    }
    catch (...)
    {
        return rejected(
            AdaptiveV2EpochFactoryStatus::authorization_failed);
    }

    try
    {
        auto bundle =
            std::make_unique<const AdaptiveV2EpochChangeBundle>(
                std::move(*command),
                std::move(successor),
                bundle_limits);
        return {
            AdaptiveV2EpochFactoryStatus::success,
            std::move(bundle)};
    }
    catch (const std::length_error &)
    {
        return rejected(
            AdaptiveV2EpochFactoryStatus::capacity_exceeded);
    }
    catch (const std::bad_alloc &)
    {
        return rejected(
            AdaptiveV2EpochFactoryStatus::capacity_exceeded);
    }
    catch (...)
    {
        return rejected(
            AdaptiveV2EpochFactoryStatus::bundle_failed);
    }
}

} // namespace

AdaptiveV2EpochFactoryResult build_adaptive_v2_successor_bundle(
    const EpochDefinition &current_epoch,
    const AdaptiveV2SelectionResult &selection,
    const AdaptiveV2TransitionPolicy &transition_policy,
    const TreePlacementInput &placement_input,
    std::uint64_t activation_delay_blocks,
    EpochChangeIssuerId issuer_id,
    const PrivKeySecp256k1 &issuer_private_key,
    const EpochChangeBundleLimits &bundle_limits) noexcept
{
    try
    {
        return build_validated(
            current_epoch,
            selection,
            transition_policy,
            placement_input,
            activation_delay_blocks,
            issuer_id,
            issuer_private_key,
            bundle_limits);
    }
    catch (const std::length_error &)
    {
        return rejected(
            AdaptiveV2EpochFactoryStatus::capacity_exceeded);
    }
    catch (const std::bad_alloc &)
    {
        return rejected(
            AdaptiveV2EpochFactoryStatus::capacity_exceeded);
    }
    catch (...)
    {
        return rejected(
            AdaptiveV2EpochFactoryStatus::internal_failure);
    }
}

AdaptiveV2EpochFactoryResult build_adaptive_v2_successor_bundle(
    const EpochDefinition &current_epoch,
    const AdaptiveV2SelectionResult &selection,
    const TreePlacementInput &placement_input,
    std::uint64_t activation_delay_blocks,
    EpochChangeIssuerId issuer_id,
    const PrivKeySecp256k1 &issuer_private_key,
    const EpochChangeBundleLimits &bundle_limits) noexcept
{
    return build_adaptive_v2_successor_bundle(
        current_epoch,
        selection,
        AdaptiveV2TransitionPolicy{},
        placement_input,
        activation_delay_blocks,
        issuer_id,
        issuer_private_key,
        bundle_limits);
}

} // namespace hotstuff
