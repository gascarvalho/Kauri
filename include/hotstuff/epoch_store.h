/**
 * Validated storage for immutable adaptive epoch definitions.
 */

#ifndef HOTSTUFF_EPOCH_STORE_H_INCLUDED
#define HOTSTUFF_EPOCH_STORE_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <map>
#include <memory>
#include <vector>

#include "hotstuff/configuration.h"

namespace hotstuff
{

struct EpochValidationContext
{
    std::uint64_t current_height{0};
    std::uint64_t minimum_activation_grace{0};
    std::vector<ReplicaID> ineligible_members;
};

class EpochStore final
{
public:
    explicit EpochStore(std::vector<ReplicaID> membership);

    const EpochDefinition &stage(
        const EpochDefinitionInput &input,
        const EpochValidationContext &context);

    const EpochDefinition *find_epoch(std::uint32_t epoch_number) const noexcept;

    const EpochTreeDefinition *find_tree(
        std::uint32_t epoch_number,
        std::uint32_t tree_id) const noexcept;

    std::size_t size() const noexcept;

private:
    const std::vector<ReplicaID> membership_;
    const uint256_t membership_digest_;
    std::map<std::uint32_t, std::unique_ptr<const EpochDefinition>> epochs_;
};

} // namespace hotstuff

#endif
