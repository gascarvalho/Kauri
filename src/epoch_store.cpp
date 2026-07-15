#include "hotstuff/epoch_store.h"

#include <algorithm>
#include <limits>
#include <set>
#include <stdexcept>
#include <string>
#include <utility>

namespace hotstuff
{
namespace
{

[[noreturn]] void reject(const std::string &reason)
{
    throw std::invalid_argument("epoch validation: " + reason);
}

std::vector<ReplicaID> validate_and_order_membership(
    std::vector<ReplicaID> membership)
{
    if (membership.empty())
    {
        reject("fixed membership is empty");
    }
    std::sort(membership.begin(), membership.end());
    if (std::adjacent_find(membership.begin(), membership.end()) !=
        membership.end())
    {
        reject("fixed membership contains duplicate replicas");
    }
    return membership;
}

std::size_t internal_slot_count(std::size_t member_count,
                                std::uint32_t fanout)
{
    if (member_count <= 1)
    {
        return 0;
    }
    return ((member_count - 2) / fanout) + 1;
}

void validate_tree_membership(
    const EpochTreeDefinition &tree,
    const std::set<ReplicaID> &membership)
{
    if (tree.members_breadth_first.empty())
    {
        reject("tree " + std::to_string(tree.tree_id) + " is empty");
    }
    if (tree.fanout == 0)
    {
        reject("tree " + std::to_string(tree.tree_id) +
               " has zero fanout");
    }
    std::set<ReplicaID> seen;
    for (const auto member : tree.members_breadth_first)
    {
        if (membership.count(member) == 0)
        {
            reject("tree " + std::to_string(tree.tree_id) +
                   " contains unknown replica " + std::to_string(member));
        }
        if (!seen.insert(member).second)
        {
            reject("tree " + std::to_string(tree.tree_id) +
                   " contains duplicate replica " + std::to_string(member));
        }
    }

    if (seen != membership)
    {
        reject("tree " + std::to_string(tree.tree_id) +
               " must contain every fixed member exactly once");
    }
}

void validate_eligibility(
    const EpochTreeDefinition &tree,
    const std::set<ReplicaID> &ineligible_members)
{
    const auto internal_slots = internal_slot_count(
        tree.members_breadth_first.size(), tree.fanout);
    const auto leaf_slots =
        tree.members_breadth_first.size() - internal_slots;
    if (ineligible_members.size() > leaf_slots)
    {
        reject("tree " + std::to_string(tree.tree_id) +
               " has insufficient leaf capacity for ineligible replicas");
    }

    const auto root = tree.members_breadth_first.front();
    if (ineligible_members.count(root) != 0)
    {
        reject("tree " + std::to_string(tree.tree_id) +
               " has ineligible root " + std::to_string(root));
    }

    for (std::size_t position = 0;
         position < tree.members_breadth_first.size();
         ++position)
    {
        const auto member = tree.members_breadth_first[position];
        if (ineligible_members.count(member) != 0 &&
            position < internal_slots)
        {
            reject("tree " + std::to_string(tree.tree_id) +
                   " places ineligible replica " + std::to_string(member) +
                   " in an internal position");
        }
    }
}

} // namespace

EpochStore::EpochStore(std::vector<ReplicaID> membership)
    : membership_(validate_and_order_membership(std::move(membership))),
      membership_digest_(canonical_membership_digest(membership_))
{
}

const EpochDefinition &EpochStore::stage(
    const EpochDefinitionInput &input,
    const EpochValidationContext &context)
{
    if (input.schema_version != kEpochDefinitionSchemaVersion)
    {
        reject("unsupported schema version " +
               std::to_string(input.schema_version));
    }
    if (epochs_.find(input.epoch_number) != epochs_.end())
    {
        reject("duplicate epoch " + std::to_string(input.epoch_number));
    }

    if (epochs_.empty())
    {
        if (input.epoch_number != 0)
        {
            reject("first staged epoch must be epoch 0");
        }
        if (input.previous_epoch_digest != uint256_t{})
        {
            reject("epoch 0 predecessor digest must be zero");
        }
    }
    else
    {
        const auto &predecessor = *epochs_.rbegin()->second;
        if (predecessor.epoch_number() ==
            std::numeric_limits<std::uint32_t>::max())
        {
            reject("epoch number space is exhausted");
        }
        const auto expected_epoch = predecessor.epoch_number() + 1;
        if (input.epoch_number != expected_epoch)
        {
            reject("epoch " + std::to_string(input.epoch_number) +
                   " is not successor " + std::to_string(expected_epoch));
        }
        if (input.previous_epoch_digest != predecessor.epoch_digest())
        {
            reject("predecessor digest mismatch");
        }
    }

    if (input.membership_digest != membership_digest_)
    {
        reject("membership digest mismatch");
    }
    if (context.minimum_activation_grace >
        std::numeric_limits<std::uint64_t>::max() - context.current_height)
    {
        reject("activation grace overflows height range");
    }
    const auto minimum_activation_height =
        context.current_height + context.minimum_activation_grace;
    if (input.activation_height < minimum_activation_height)
    {
        reject("activation height does not provide minimum staging grace");
    }
    if (input.trees.empty())
    {
        reject("epoch contains no trees");
    }

    // Canonicalize copies before structural validation so equivalent
    // untrusted inputs fail in a stable order without modifying caller data.
    auto normalized_input = input;
    std::stable_sort(
        normalized_input.trees.begin(), normalized_input.trees.end(),
        [](const auto &left, const auto &right) {
            return left.tree_id < right.tree_id;
        });
    const auto duplicate_tree = std::adjacent_find(
        normalized_input.trees.begin(), normalized_input.trees.end(),
        [](const auto &left, const auto &right) {
            return left.tree_id == right.tree_id;
        });
    if (duplicate_tree != normalized_input.trees.end())
    {
        reject("duplicate tree ID " +
               std::to_string(duplicate_tree->tree_id));
    }

    const std::set<ReplicaID> membership(
        membership_.begin(), membership_.end());
    auto ordered_ineligible_members = context.ineligible_members;
    std::sort(
        ordered_ineligible_members.begin(), ordered_ineligible_members.end());
    std::set<ReplicaID> ineligible_members;
    for (const auto member : ordered_ineligible_members)
    {
        if (membership.count(member) == 0)
        {
            reject("unknown ineligible replica " + std::to_string(member));
        }
        if (!ineligible_members.insert(member).second)
        {
            reject("duplicate ineligible replica " + std::to_string(member));
        }
    }

    for (const auto &tree : normalized_input.trees)
    {
        validate_tree_membership(tree, membership);
        validate_eligibility(tree, ineligible_members);
    }

    auto canonical_serialization =
        canonical_serialize_epoch(normalized_input);
    const auto computed_digest =
        DataStream(canonical_serialization).get_hash();
    if (input.epoch_digest && *input.epoch_digest != computed_digest)
    {
        reject("claimed epoch digest mismatch");
    }

    normalized_input.epoch_digest.reset();

    auto definition = std::unique_ptr<const EpochDefinition>(
        new EpochDefinition(
            std::move(normalized_input),
            std::move(canonical_serialization),
            computed_digest));
    const auto epoch_number = definition->epoch_number();
    const auto insertion = epochs_.emplace(epoch_number, std::move(definition));
    if (!insertion.second)
    {
        reject("duplicate epoch " + std::to_string(epoch_number));
    }
    return *insertion.first->second;
}

const EpochDefinition *EpochStore::find_epoch(
    std::uint32_t epoch_number) const noexcept
{
    const auto epoch = epochs_.find(epoch_number);
    return epoch == epochs_.end() ? nullptr : epoch->second.get();
}

const EpochTreeDefinition *EpochStore::find_tree(
    std::uint32_t epoch_number,
    std::uint32_t tree_id) const noexcept
{
    const auto *epoch = find_epoch(epoch_number);
    if (epoch == nullptr)
    {
        return nullptr;
    }

    const auto &trees = epoch->trees();
    const auto tree = std::lower_bound(
        trees.begin(), trees.end(), tree_id,
        [](const auto &candidate, std::uint32_t id) {
            return candidate.tree_id < id;
        });
    return tree == trees.end() || tree->tree_id != tree_id
               ? nullptr
               : &*tree;
}

std::size_t EpochStore::size() const noexcept
{
    return epochs_.size();
}

} // namespace hotstuff
