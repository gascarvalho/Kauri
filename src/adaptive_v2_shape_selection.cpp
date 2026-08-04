#include "hotstuff/adaptive_v2_shape_selection.h"
#include "hotstuff/tree_policy.h"

#include <algorithm>
#include <limits>
#include <map>
#include <set>
#include <stdexcept>
#include <tuple>
#include <type_traits>

namespace hotstuff
{
namespace
{

constexpr char kTopologyDigestDomain[] = "kauri-shape-v1-topology";
constexpr char kEvidenceDigestDomain[] = "kauri-shape-v1-evidence";
constexpr char kDecisionDigestDomain[] = "kauri-shape-v1-decision";
constexpr std::uint32_t kMaximumLiveFanout = 255;

template <typename UInt>
void append_big_endian(bytearray_t &output, UInt value)
{
    static_assert(
        std::is_unsigned<UInt>::value,
        "canonical integers must be unsigned");
    for (std::size_t shift = sizeof(UInt); shift > 0; --shift)
    {
        output.push_back(static_cast<std::uint8_t>(
            value >> ((shift - 1) * 8)));
    }
}

void append_digest(bytearray_t &output, const uint256_t &value)
{
    const auto bytes = static_cast<bytearray_t>(value);
    if (bytes.size() != 32)
        throw std::logic_error("shape-v1 digest is not 32 bytes");
    output.insert(output.end(), bytes.begin(), bytes.end());
}

void append_string(bytearray_t &output, const std::string &value)
{
    if (value.size() > std::numeric_limits<std::uint32_t>::max())
        throw std::length_error("shape-v1 string exceeds uint32 length");
    append_big_endian(output, static_cast<std::uint32_t>(value.size()));
    output.insert(output.end(), value.begin(), value.end());
}

template <std::size_t Size>
void append_domain(bytearray_t &output, const char (&value)[Size])
{
    output.insert(output.end(), value, value + Size - 1);
}

bool valid_classification(ResponsivenessClass value) noexcept
{
    switch (value)
    {
        case ResponsivenessClass::responsive:
        case ResponsivenessClass::insufficient_evidence:
        case ResponsivenessClass::nonresponsive:
            return true;
    }
    return false;
}

bool valid_rejection(ShapeV1CandidateRejection value) noexcept
{
    switch (value)
    {
        case ShapeV1CandidateRejection::none:
        case ShapeV1CandidateRejection::fanout_out_of_range:
        case ShapeV1CandidateRejection::wait_exempt_would_influence:
        case ShapeV1CandidateRejection::missing_influential_evidence:
        case ShapeV1CandidateRejection::score_overflow:
            return true;
    }
    return false;
}

std::size_t first_leaf_index(
    std::size_t member_count,
    std::uint32_t fanout) noexcept
{
    return member_count == 1
               ? 0
               : ((member_count - 2) / fanout) + 1;
}

std::uint32_t uniform_tree_depth(
    std::size_t member_count,
    std::uint32_t fanout) noexcept
{
    if (member_count <= 1)
        return 0;
    std::uint32_t depth = 0;
    auto position = member_count - 1;
    while (position != 0)
    {
        position = (position - 1) / fanout;
        ++depth;
    }
    return depth;
}

std::vector<const EpochTreeDefinition *> ordered_trees(
    const std::vector<EpochTreeDefinition> &trees)
{
    std::vector<const EpochTreeDefinition *> ordered;
    ordered.reserve(trees.size());
    for (const auto &tree : trees)
        ordered.push_back(&tree);
    std::sort(
        ordered.begin(), ordered.end(),
        [](const auto *left, const auto *right) {
            return left->tree_id < right->tree_id;
        });
    return ordered;
}

std::vector<ShapeV1ReplicaEvidence> ordered_evidence(
    const std::vector<ShapeV1ReplicaEvidence> &evidence)
{
    auto ordered = evidence;
    std::sort(
        ordered.begin(), ordered.end(),
        [](const auto &left, const auto &right) {
            return left.replica_id < right.replica_id;
        });
    return ordered;
}

uint256_t topology_digest(
    const ShapeV1Input &input,
    const std::vector<const EpochTreeDefinition *> &trees)
{
    bytearray_t bytes;
    append_domain(bytes, kTopologyDigestDomain);
    append_big_endian(bytes, input.epoch_number);
    append_digest(bytes, input.epoch_digest);
    append_big_endian(bytes, input.tree_count);
    append_big_endian(bytes, input.fixed_pipeline_stretch);
    append_big_endian(bytes, static_cast<std::uint32_t>(trees.size()));
    for (const auto *tree : trees)
    {
        append_big_endian(bytes, tree->tree_id);
        append_big_endian(bytes, tree->fanout);
        append_big_endian(bytes, tree->pipeline_stretch);
        append_big_endian(
            bytes,
            static_cast<std::uint32_t>(
                tree->members_breadth_first.size()));
        for (const auto member : tree->members_breadth_first)
            append_big_endian(bytes, member);
        append_big_endian(
            bytes,
            static_cast<std::uint32_t>(
                tree->wait_exempt_leaves.size()));
        for (const auto member : tree->wait_exempt_leaves)
            append_big_endian(bytes, member);
    }
    return DataStream(bytes).get_hash();
}

uint256_t evidence_digest(
    const ShapeV1Input &input,
    const std::vector<ShapeV1ReplicaEvidence> &evidence)
{
    bytearray_t bytes;
    append_domain(bytes, kEvidenceDigestDomain);
    append_big_endian(bytes, input.epoch_number);
    append_digest(bytes, input.epoch_digest);
    append_big_endian(bytes, input.evidence_cutoff);
    append_big_endian(bytes, static_cast<std::uint32_t>(evidence.size()));
    for (const auto &entry : evidence)
    {
        append_big_endian(bytes, entry.replica_id);
        append_big_endian(
            bytes, static_cast<std::uint8_t>(entry.classification));
        append_big_endian(bytes, entry.attempt_count);
        append_big_endian(bytes, entry.timeout_rate_ppm);
        append_big_endian(
            bytes,
            static_cast<std::uint8_t>(
                entry.latency_percentile_us.has_value()));
        if (entry.latency_percentile_us.has_value())
            append_big_endian(bytes, *entry.latency_percentile_us);
    }
    return DataStream(bytes).get_hash();
}

bool add_checked(
    std::uint64_t left,
    std::uint64_t right,
    std::uint64_t &result) noexcept
{
    if (right > std::numeric_limits<std::uint64_t>::max() - left)
        return false;
    result = left + right;
    return true;
}

bool multiply_checked(
    std::uint64_t left,
    std::uint64_t right,
    std::uint64_t &result) noexcept
{
    if (left != 0 &&
        right > std::numeric_limits<std::uint64_t>::max() / left)
    {
        return false;
    }
    result = left * right;
    return true;
}

ShapeV1CandidateRejection score_tree(
    std::uint32_t fanout,
    const EpochTreeDefinition &tree,
    const std::map<ReplicaID, ShapeV1ReplicaEvidence> &evidence,
    ShapeV1CandidateScore &score)
{
    const auto member_count = tree.members_breadth_first.size();
    const auto candidate_leaf_start =
        first_leaf_index(member_count, fanout);
    const std::set<ReplicaID> wait_exempt(
        tree.wait_exempt_leaves.begin(),
        tree.wait_exempt_leaves.end());
    for (const auto replica : wait_exempt)
    {
        const auto position = std::find(
            tree.members_breadth_first.begin(),
            tree.members_breadth_first.end(), replica);
        if (position == tree.members_breadth_first.end() ||
            static_cast<std::size_t>(std::distance(
                tree.members_breadth_first.begin(), position)) <
                candidate_leaf_start)
        {
            return ShapeV1CandidateRejection::
                wait_exempt_would_influence;
        }
    }

    std::vector<std::uint64_t> subtree_size(member_count, 0);
    std::vector<std::uint64_t> path_latency(member_count, 0);
    for (std::size_t position = 0; position < member_count; ++position)
    {
        const auto replica = tree.members_breadth_first[position];
        if (wait_exempt.count(replica) != 0)
            continue;
        const auto found = evidence.find(replica);
        if (found == evidence.end() ||
            found->second.classification ==
                ResponsivenessClass::insufficient_evidence ||
            found->second.attempt_count == 0 ||
            !found->second.latency_percentile_us.has_value())
        {
            return ShapeV1CandidateRejection::
                missing_influential_evidence;
        }
        subtree_size[position] = 1;
        if (position == 0)
            continue;
        const auto parent = (position - 1) / fanout;
        if (!add_checked(
                path_latency[parent],
                *found->second.latency_percentile_us,
                path_latency[position]))
        {
            return ShapeV1CandidateRejection::score_overflow;
        }
        score.latency = std::max(score.latency, path_latency[position]);
    }

    for (std::size_t position = member_count; position-- > 1;)
    {
        const auto parent = (position - 1) / fanout;
        if (!add_checked(
                subtree_size[parent],
                subtree_size[position],
                subtree_size[parent]))
        {
            return ShapeV1CandidateRejection::score_overflow;
        }
    }
    for (std::size_t position = 0; position < member_count; ++position)
    {
        const auto replica = tree.members_breadth_first[position];
        if (wait_exempt.count(replica) != 0)
            continue;
        std::uint64_t exposure = 0;
        if (!multiply_checked(
                evidence.at(replica).timeout_rate_ppm,
                subtree_size[position],
                exposure))
        {
            return ShapeV1CandidateRejection::score_overflow;
        }
        score.risk = std::max(score.risk, exposure);
    }

    for (std::size_t position = 1; position < member_count; ++position)
    {
        const auto current_parent = (position - 1) / tree.fanout;
        const auto candidate_parent = (position - 1) / fanout;
        if (tree.members_breadth_first[current_parent] ==
            tree.members_breadth_first[candidate_parent])
        {
            continue;
        }
        if (score.churn == std::numeric_limits<std::uint64_t>::max())
            return ShapeV1CandidateRejection::score_overflow;
        ++score.churn;
    }
    return ShapeV1CandidateRejection::none;
}

ShapeV1CandidateScore score_candidate(
    std::uint32_t fanout,
    const std::vector<const EpochTreeDefinition *> &trees,
    std::uint32_t tree_count,
    const std::map<ReplicaID, ShapeV1ReplicaEvidence> &evidence)
{
    ShapeV1CandidateScore score;
    score.fanout = fanout;
    if (fanout == 0 || fanout > kMaximumLiveFanout)
    {
        score.rejection =
            ShapeV1CandidateRejection::fanout_out_of_range;
        return score;
    }
    score.depth = uniform_tree_depth(
        trees.front()->members_breadth_first.size(), fanout);
    for (std::size_t index = 0; index < tree_count; ++index)
    {
        const auto rejection = score_tree(
            fanout, *trees[index], evidence, score);
        if (rejection != ShapeV1CandidateRejection::none)
        {
            ShapeV1CandidateScore rejected;
            rejected.fanout = fanout;
            rejected.rejection = rejection;
            rejected.depth = score.depth;
            return rejected;
        }
    }
    return score;
}

bool improves_five_percent(
    std::uint64_t candidate,
    std::uint64_t current) noexcept
{
    if (current == 0 || candidate >= current)
        return false;
    const auto required =
        current / 20 + static_cast<std::uint64_t>(current % 20 != 0);
    return candidate <= current - required;
}

bool switch_threshold(
    const ShapeV1CandidateScore &candidate,
    const ShapeV1CandidateScore &current) noexcept
{
    return (improves_five_percent(
                candidate.latency, current.latency) &&
            candidate.risk <= current.risk) ||
           (improves_five_percent(candidate.risk, current.risk) &&
            candidate.latency <= current.latency);
}

bool better_switch_candidate(
    const ShapeV1CandidateScore &left,
    const ShapeV1CandidateScore &right,
    std::uint32_t current_fanout) noexcept
{
    return std::make_tuple(
               left.latency,
               left.risk,
               left.churn,
               left.fanout != current_fanout,
               left.fanout) <
           std::make_tuple(
               right.latency,
               right.risk,
               right.churn,
               right.fanout != current_fanout,
               right.fanout);
}

bool valid_input_header(
    const ShapeV1Input &input,
    const std::vector<const EpochTreeDefinition *> &trees,
    const std::vector<std::uint32_t> &candidates) noexcept
{
    return input.schema_version == kShapeV1SchemaVersion &&
        input.selector_version == kShapeV1SelectorVersion &&
        input.tie_rule == kShapeV1TieRule &&
        input.reference_tree_rule == kShapeV1ReferenceTreeRule &&
        input.epoch_digest != uint256_t{} && input.evidence_cutoff != 0 &&
        !trees.empty() && !candidates.empty() && input.tree_count != 0 &&
        input.tree_count <= 255 && trees.size() <= 255 &&
        input.tree_count <= trees.size() &&
        input.fixed_pipeline_stretch != 0 &&
        input.fixed_pipeline_stretch <=
            kMaximumTreePolicyPipelineStretch;
}

bool valid_membership(const std::vector<ReplicaID> &membership) noexcept
{
    return !membership.empty() &&
        membership.size() <= kMaximumAdaptationMembers &&
        std::adjacent_find(membership.begin(), membership.end()) ==
            membership.end();
}

bool valid_tree(
    const EpochTreeDefinition &tree,
    const std::vector<ReplicaID> &membership,
    std::uint32_t current_fanout,
    std::uint32_t pipeline_stretch,
    std::set<std::uint32_t> &tree_ids)
{
    auto tree_members = tree.members_breadth_first;
    std::sort(tree_members.begin(), tree_members.end());
    if (!tree_ids.insert(tree.tree_id).second ||
        tree_members != membership || tree.fanout == 0 ||
        tree.fanout > kMaximumLiveFanout ||
        tree.fanout != current_fanout ||
        tree.pipeline_stretch != pipeline_stretch ||
        !std::is_sorted(
            tree.wait_exempt_leaves.begin(),
            tree.wait_exempt_leaves.end()) ||
        std::adjacent_find(
            tree.wait_exempt_leaves.begin(),
            tree.wait_exempt_leaves.end()) !=
            tree.wait_exempt_leaves.end())
    {
        return false;
    }

    const auto leaf_start = first_leaf_index(
        tree.members_breadth_first.size(), tree.fanout);
    for (const auto replica : tree.wait_exempt_leaves)
    {
        const auto found = std::find(
            tree.members_breadth_first.begin(),
            tree.members_breadth_first.end(), replica);
        if (!std::binary_search(
                membership.begin(), membership.end(), replica) ||
            found == tree.members_breadth_first.end() ||
            static_cast<std::size_t>(std::distance(
                tree.members_breadth_first.begin(), found)) < leaf_start)
        {
            return false;
        }
    }
    return true;
}

bool index_evidence(
    const std::vector<ShapeV1ReplicaEvidence> &evidence,
    const std::vector<ReplicaID> &membership,
    std::map<ReplicaID, ShapeV1ReplicaEvidence> &indexed)
{
    for (const auto &entry : evidence)
    {
        if (!std::binary_search(
                membership.begin(), membership.end(), entry.replica_id) ||
            !valid_classification(entry.classification) ||
            entry.timeout_rate_ppm > kRatePpmScale ||
            (entry.latency_percentile_us.has_value() &&
             *entry.latency_percentile_us == 0) ||
            !indexed.emplace(entry.replica_id, entry).second)
        {
            return false;
        }
    }
    return true;
}

ShapeDecisionRecord initial_record(const ShapeV1Input &input)
{
    ShapeDecisionRecord record;
    record.schema_version = input.schema_version;
    record.selector_version = input.selector_version;
    record.tie_rule = input.tie_rule;
    record.epoch_number = input.epoch_number;
    record.epoch_digest = input.epoch_digest;
    record.evidence_cutoff = input.evidence_cutoff;
    record.predecessor_tree_count = static_cast<std::uint32_t>(
        input.current_trees.size());
    record.tree_count = input.tree_count;
    record.fixed_pipeline_stretch = input.fixed_pipeline_stretch;
    record.deterministic_seed = input.deterministic_seed;
    record.reference_tree_rule = input.reference_tree_rule;
    return record;
}

} // namespace

std::optional<ShapeV1CanonicalChoice>
recompute_shape_v1_canonical_choice(
    const std::vector<ShapeV1CandidateScore> &candidates,
    std::uint32_t current_fanout) noexcept
{
    try
    {
        const ShapeV1CandidateScore *current = nullptr;
        for (const auto &candidate : candidates)
        {
            if (candidate.fanout != current_fanout ||
                candidate.rejection != ShapeV1CandidateRejection::none)
            {
                continue;
            }
            if (current != nullptr)
                return std::nullopt;
            current = &candidate;
        }
        if (current == nullptr)
            return std::nullopt;

        ShapeV1CanonicalChoice choice;
        choice.switch_thresholds.assign(candidates.size(), false);
        const ShapeV1CandidateScore *best = nullptr;
        for (std::size_t index = 0; index < candidates.size(); ++index)
        {
            const auto &candidate = candidates[index];
            if (candidate.rejection != ShapeV1CandidateRejection::none ||
                candidate.fanout == current_fanout)
            {
                continue;
            }
            const bool qualifies = switch_threshold(candidate, *current);
            choice.switch_thresholds[index] = qualifies;
            if (qualifies &&
                (best == nullptr || better_switch_candidate(
                    candidate, *best, current_fanout)))
            {
                best = &candidate;
            }
        }
        choice.selected_fanout =
            best == nullptr ? current_fanout : best->fanout;
        return choice;
    }
    catch (...)
    {
        return std::nullopt;
    }
}

bytearray_t canonical_serialize_shape_decision_record(
    const ShapeDecisionRecord &record)
{
    bytearray_t bytes;
    append_domain(bytes, kDecisionDigestDomain);
    append_big_endian(bytes, record.schema_version);
    append_big_endian(bytes, static_cast<std::uint8_t>(record.status));
    append_string(bytes, record.selector_version);
    append_string(bytes, record.tie_rule);
    append_big_endian(bytes, record.epoch_number);
    append_digest(bytes, record.epoch_digest);
    append_digest(bytes, record.current_topology_digest);
    append_big_endian(bytes, record.evidence_cutoff);
    append_digest(bytes, record.evidence_digest);
    append_big_endian(bytes, record.predecessor_tree_count);
    append_big_endian(bytes, record.tree_count);
    append_big_endian(bytes, record.fixed_pipeline_stretch);
    append_big_endian(bytes, record.deterministic_seed);
    append_big_endian(bytes, record.current_fanout);
    append_big_endian(bytes, record.selected_fanout);
    append_big_endian(bytes, record.applied_fanout);
    append_string(bytes, record.reference_tree_rule);
    append_big_endian(
        bytes, static_cast<std::uint32_t>(record.candidates.size()));
    for (const auto &candidate : record.candidates)
    {
        append_big_endian(bytes, candidate.fanout);
        append_big_endian(
            bytes, static_cast<std::uint8_t>(candidate.rejection));
        append_big_endian(bytes, candidate.depth);
        append_big_endian(bytes, candidate.risk);
        append_big_endian(bytes, candidate.latency);
        append_big_endian(bytes, candidate.churn);
        append_big_endian(
            bytes,
            static_cast<std::uint8_t>(
                candidate.switch_threshold_satisfied));
    }
    return bytes;
}

uint256_t compute_shape_decision_digest(
    const ShapeDecisionRecord &record)
{
    return DataStream(
        canonical_serialize_shape_decision_record(record)).get_hash();
}

bool valid_shape_decision_record(
    const ShapeDecisionRecord &record) noexcept
{
    try
    {
        if (record.schema_version != kShapeV1SchemaVersion ||
            record.status != ShapeV1Status::selected ||
            record.selector_version != kShapeV1SelectorVersion ||
            record.tie_rule != kShapeV1TieRule ||
            record.epoch_digest == uint256_t{} ||
            record.current_topology_digest == uint256_t{} ||
            record.evidence_cutoff == 0 ||
            record.evidence_digest == uint256_t{} ||
            record.predecessor_tree_count == 0 ||
            record.predecessor_tree_count > 255 ||
            record.tree_count == 0 || record.tree_count > 255 ||
            record.tree_count > record.predecessor_tree_count ||
            record.fixed_pipeline_stretch == 0 ||
            record.current_fanout == 0 ||
            record.current_fanout > kMaximumLiveFanout ||
            record.selected_fanout == 0 ||
            record.selected_fanout > kMaximumLiveFanout ||
            record.reference_tree_rule !=
                kShapeV1ReferenceTreeRule ||
            (record.applied_fanout != record.current_fanout &&
             record.applied_fanout != record.selected_fanout) ||
            record.candidates.empty())
        {
            return false;
        }

        bool has_current = false;
        std::uint32_t previous = 0;
        for (std::size_t index = 0;
             index < record.candidates.size(); ++index)
        {
            const auto &candidate = record.candidates[index];
            if ((index != 0 && candidate.fanout <= previous) ||
                !valid_rejection(candidate.rejection) ||
                (candidate.rejection ==
                     ShapeV1CandidateRejection::fanout_out_of_range &&
                 candidate.depth != 0) ||
                (candidate.rejection != ShapeV1CandidateRejection::none &&
                 (candidate.risk != 0 || candidate.latency != 0 ||
                  candidate.churn != 0 ||
                  candidate.switch_threshold_satisfied)))
            {
                return false;
            }
            previous = candidate.fanout;
            const bool feasible = candidate.rejection ==
                ShapeV1CandidateRejection::none;
            has_current = has_current ||
                (candidate.fanout == record.current_fanout && feasible);
        }
        if (!has_current)
            return false;
        const auto canonical = recompute_shape_v1_canonical_choice(
            record.candidates, record.current_fanout);
        if (!canonical.has_value() ||
            canonical->selected_fanout != record.selected_fanout ||
            canonical->switch_thresholds.size() !=
                record.candidates.size())
        {
            return false;
        }
        for (std::size_t index = 0;
             index < record.candidates.size(); ++index)
        {
            if (record.candidates[index].switch_threshold_satisfied !=
                canonical->switch_thresholds[index])
            {
                return false;
            }
        }
        return record.decision_digest ==
            compute_shape_decision_digest(record);
    }
    catch (...)
    {
        return false;
    }
}

ShapeDecisionRecord select_shape_v1(const ShapeV1Input &input) noexcept
{
    auto record = initial_record(input);
    const auto invalid = [&record]() {
        record.status = ShapeV1Status::invalid_input;
        record.decision_digest = {};
        return record;
    };

    try
    {
        auto trees = ordered_trees(input.current_trees);
        auto evidence = ordered_evidence(input.evidence);
        auto candidates = input.candidate_fanouts;
        std::sort(candidates.begin(), candidates.end());
        candidates.erase(
            std::unique(candidates.begin(), candidates.end()),
            candidates.end());
        if (!valid_input_header(input, trees, candidates))
            return invalid();

        auto membership = trees.front()->members_breadth_first;
        std::sort(membership.begin(), membership.end());
        const auto quorum = derive_byzantine_quorum(membership.size());
        if (!valid_membership(membership) || !quorum.has_value() ||
            quorum->quorum != input.tree_count)
        {
            return invalid();
        }

        record.current_fanout = trees.front()->fanout;
        std::set<std::uint32_t> tree_ids;
        for (const auto *tree : trees)
        {
            if (!valid_tree(
                    *tree,
                    membership,
                    record.current_fanout,
                    input.fixed_pipeline_stretch,
                    tree_ids))
            {
                return invalid();
            }
        }
        if (!std::binary_search(
                candidates.begin(), candidates.end(),
                record.current_fanout))
        {
            return invalid();
        }

        std::map<ReplicaID, ShapeV1ReplicaEvidence> indexed_evidence;
        if (!index_evidence(evidence, membership, indexed_evidence))
            return invalid();

        record.current_topology_digest = topology_digest(input, trees);
        record.evidence_digest = evidence_digest(input, evidence);
        for (const auto fanout : candidates)
        {
            // ordered_trees() makes this the audited lowest-ID Q prefix.
            record.candidates.push_back(score_candidate(
                fanout,
                trees,
                input.tree_count,
                indexed_evidence));
        }
        const auto canonical = recompute_shape_v1_canonical_choice(
            record.candidates, record.current_fanout);
        if (!canonical.has_value())
        {
            record.status = ShapeV1Status::no_feasible_candidate;
            record.decision_digest = compute_shape_decision_digest(record);
            return record;
        }
        for (std::size_t index = 0;
             index < record.candidates.size(); ++index)
        {
            record.candidates[index].switch_threshold_satisfied =
                canonical->switch_thresholds[index];
        }
        record.selected_fanout = canonical->selected_fanout;
        record.applied_fanout = record.selected_fanout;
        record.status = ShapeV1Status::selected;
        record.decision_digest = compute_shape_decision_digest(record);
        return valid_shape_decision_record(record) ? record : invalid();
    }
    catch (...)
    {
        return invalid();
    }
}

bool finalize_shape_v1_application(
    ShapeDecisionRecord &record,
    bool apply_selected) noexcept
{
    if (!valid_shape_decision_record(record))
        return false;
    try
    {
        record.applied_fanout = apply_selected
                                    ? record.selected_fanout
                                    : record.current_fanout;
        record.decision_digest = compute_shape_decision_digest(record);
        return valid_shape_decision_record(record);
    }
    catch (...)
    {
        return false;
    }
}

} // namespace hotstuff
