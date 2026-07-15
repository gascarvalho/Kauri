/**
 * Exact, versioned identities for adaptive Kauri configurations.
 */

#ifndef HOTSTUFF_CONFIGURATION_H_INCLUDED
#define HOTSTUFF_CONFIGURATION_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <functional>
#include <optional>
#include <string>
#include <vector>

#include "hotstuff/type.h"

namespace hotstuff
{

constexpr std::uint32_t kEpochDefinitionSchemaVersion = 1;

struct ConfigurationId
{
    std::uint32_t epoch_number{0};
    std::uint32_t tree_id{0};
    uint256_t epoch_digest;

    bool operator==(const ConfigurationId &other) const noexcept;
    bool operator!=(const ConfigurationId &other) const noexcept;
    bool operator<(const ConfigurationId &other) const noexcept;
};

struct ProposalKey
{
    ConfigurationId configuration;
    uint256_t block_hash;

    bool operator==(const ProposalKey &other) const noexcept;
    bool operator!=(const ProposalKey &other) const noexcept;
    bool operator<(const ProposalKey &other) const noexcept;
};

struct LeaderViewId
{
    ConfigurationId configuration;
    std::uint64_t view_generation{0};
    ReplicaID leader_id{0};

    bool operator==(const LeaderViewId &other) const noexcept;
    bool operator!=(const LeaderViewId &other) const noexcept;
    bool operator<(const LeaderViewId &other) const noexcept;
};

struct EpochTreeDefinition
{
    std::uint32_t tree_id{0};
    std::uint32_t fanout{0};
    std::uint32_t pipeline_stretch{0};
    std::vector<ReplicaID> members_breadth_first;
};

struct EpochDefinitionInput
{
    std::uint32_t schema_version{kEpochDefinitionSchemaVersion};
    std::uint32_t epoch_number{0};
    uint256_t previous_epoch_digest;
    uint256_t membership_digest;
    std::vector<EpochTreeDefinition> trees;
    std::uint64_t activation_height{0};
    std::uint64_t generation_seed{0};
    std::string policy_version;
    std::string evidence_snapshot_id;
    std::uint64_t evidence_cutoff{0};
    std::optional<uint256_t> epoch_digest;
};

uint256_t canonical_membership_digest(
    const std::vector<ReplicaID> &membership);

bytearray_t canonical_serialize_epoch(const EpochDefinitionInput &input);

uint256_t compute_epoch_digest(const EpochDefinitionInput &input);

class EpochStore;

/**
 * An externally immutable, validated epoch definition.
 */
class EpochDefinition final
{
public:
    std::uint32_t schema_version() const noexcept;
    std::uint32_t epoch_number() const noexcept;
    const uint256_t &previous_epoch_digest() const noexcept;
    const uint256_t &membership_digest() const noexcept;
    const std::vector<EpochTreeDefinition> &trees() const noexcept;
    std::uint64_t activation_height() const noexcept;
    std::uint64_t generation_seed() const noexcept;
    const std::string &policy_version() const noexcept;
    const std::string &evidence_snapshot_id() const noexcept;
    std::uint64_t evidence_cutoff() const noexcept;
    const bytearray_t &canonical_serialization() const noexcept;
    const uint256_t &epoch_digest() const noexcept;

private:
    friend class EpochStore;

    EpochDefinition(EpochDefinitionInput input,
                    bytearray_t canonical_serialization,
                    uint256_t epoch_digest);

    const std::uint32_t schema_version_;
    const std::uint32_t epoch_number_;
    const uint256_t previous_epoch_digest_;
    const uint256_t membership_digest_;
    const std::vector<EpochTreeDefinition> trees_;
    const std::uint64_t activation_height_;
    const std::uint64_t generation_seed_;
    const std::string policy_version_;
    const std::string evidence_snapshot_id_;
    const std::uint64_t evidence_cutoff_;
    const bytearray_t canonical_serialization_;
    const uint256_t epoch_digest_;
};

/**
 * Adapt the existing fan:/pipe: breadth-first text grammar to epoch zero.
 */
EpochDefinitionInput parse_legacy_epoch_zero(
    const std::string &configuration,
    const std::vector<ReplicaID> &membership);

} // namespace hotstuff

namespace std
{

template <>
struct hash<hotstuff::ConfigurationId>
{
    size_t operator()(const hotstuff::ConfigurationId &id) const noexcept;
};

template <>
struct hash<hotstuff::ProposalKey>
{
    size_t operator()(const hotstuff::ProposalKey &key) const noexcept;
};

template <>
struct hash<hotstuff::LeaderViewId>
{
    size_t operator()(const hotstuff::LeaderViewId &view) const noexcept;
};

} // namespace std

#endif
