#include "hotstuff/configuration.h"

#include <algorithm>
#include <charconv>
#include <limits>
#include <set>
#include <sstream>
#include <stdexcept>
#include <system_error>
#include <tuple>
#include <type_traits>
#include <utility>

namespace hotstuff
{
namespace
{

constexpr char kMembershipDomain[] = "kauri-membership-v1";
constexpr char kEpochDomainV1[] = "kauri-epoch-definition-v1";
constexpr char kEpochDomainV2[] = "kauri-epoch-definition-v2";

template <typename UInt>
void append_big_endian(bytearray_t &output, UInt value)
{
    static_assert(std::is_unsigned<UInt>::value,
                  "canonical integers must be unsigned");
    for (std::size_t shift = sizeof(UInt); shift > 0; --shift)
    {
        output.push_back(static_cast<std::uint8_t>(
            value >> ((shift - 1) * 8)));
    }
}

void append_domain(bytearray_t &output, const char *domain, std::size_t size)
{
    output.insert(output.end(), domain, domain + size);
}

void append_digest(bytearray_t &output, const uint256_t &digest)
{
    // Salticidae's Blob serialization is the project's fixed 32-byte digest
    // representation and explicitly normalizes each word to little endian.
    const bytearray_t bytes = static_cast<bytearray_t>(digest);
    if (bytes.size() != 32)
    {
        throw std::logic_error("canonical digest serialization is not 32 bytes");
    }
    output.insert(output.end(), bytes.begin(), bytes.end());
}

void append_string(bytearray_t &output, const std::string &value)
{
    if (value.size() > std::numeric_limits<std::uint32_t>::max())
    {
        throw std::length_error("canonical string exceeds uint32 length");
    }
    append_big_endian(output, static_cast<std::uint32_t>(value.size()));
    output.insert(output.end(), value.begin(), value.end());
}

std::vector<const EpochTreeDefinition *> canonical_tree_order(
    const std::vector<EpochTreeDefinition> &trees)
{
    std::vector<const EpochTreeDefinition *> ordered;
    ordered.reserve(trees.size());
    for (const auto &tree : trees)
    {
        ordered.push_back(&tree);
    }
    std::stable_sort(
        ordered.begin(), ordered.end(),
        [](const auto *left, const auto *right) {
            return left->tree_id < right->tree_id;
        });
    return ordered;
}

std::uint64_t parse_decimal(
    const std::string &token,
    const std::string &field,
    std::size_t line_number)
{
    if (token.empty())
    {
        throw std::invalid_argument(
            "legacy epoch line " + std::to_string(line_number) +
            ": missing " + field);
    }

    std::uint64_t value = 0;
    const auto result = std::from_chars(
        token.data(), token.data() + token.size(), value);
    if (result.ec != std::errc{} || result.ptr != token.data() + token.size())
    {
        throw std::invalid_argument(
            "legacy epoch line " + std::to_string(line_number) +
            ": invalid " + field + " '" + token + "'");
    }
    return value;
}

std::uint32_t parse_legacy_tree_option(
    const std::string &token,
    const std::string &prefix,
    const std::string &field,
    std::size_t line_number,
    std::uint32_t minimum_value)
{
    if (token.compare(0, prefix.size(), prefix) != 0)
    {
        throw std::invalid_argument(
            "legacy epoch line " + std::to_string(line_number) +
            ": expected " + prefix + "<value>");
    }

    const auto value = parse_decimal(
        token.substr(prefix.size()), field, line_number);
    if (value < minimum_value ||
        value > std::numeric_limits<std::uint8_t>::max())
    {
        throw std::invalid_argument(
            "legacy epoch line " + std::to_string(line_number) +
            ": " + field + " must be in [" +
            std::to_string(minimum_value) + ", 255]");
    }
    return static_cast<std::uint32_t>(value);
}

void hash_combine(std::size_t &seed, std::size_t value) noexcept
{
    constexpr auto golden_ratio =
        static_cast<std::size_t>(0x9e3779b97f4a7c15ULL);
    seed ^= value + golden_ratio + (seed << 6) + (seed >> 2);
}

} // namespace

std::optional<ByzantineQuorum> derive_byzantine_quorum(
    std::size_t replica_count) noexcept
{
    if (replica_count == 0 ||
        replica_count > std::numeric_limits<std::uint32_t>::max() ||
        (replica_count - 1) % 3 != 0)
        return std::nullopt;

    const auto replicas = static_cast<std::uint32_t>(replica_count);
    const auto faults = static_cast<std::uint32_t>((replica_count - 1) / 3);
    return ByzantineQuorum{
        replicas,
        faults,
        static_cast<std::uint32_t>(faults * 2 + 1)};
}

bool ConfigurationId::operator==(const ConfigurationId &other) const noexcept
{
    return epoch_number == other.epoch_number &&
           tree_id == other.tree_id &&
           epoch_digest == other.epoch_digest;
}

bool ConfigurationId::operator!=(const ConfigurationId &other) const noexcept
{
    return !(*this == other);
}

bool ConfigurationId::operator<(const ConfigurationId &other) const noexcept
{
    return std::tie(epoch_number, tree_id, epoch_digest) <
           std::tie(other.epoch_number, other.tree_id, other.epoch_digest);
}

bool ProposalKey::operator==(const ProposalKey &other) const noexcept
{
    return configuration == other.configuration &&
           block_hash == other.block_hash;
}

bool ProposalKey::operator!=(const ProposalKey &other) const noexcept
{
    return !(*this == other);
}

bool ProposalKey::operator<(const ProposalKey &other) const noexcept
{
    return std::tie(configuration, block_hash) <
           std::tie(other.configuration, other.block_hash);
}

bool LeaderViewId::operator==(const LeaderViewId &other) const noexcept
{
    return configuration == other.configuration &&
           view_generation == other.view_generation &&
           leader_id == other.leader_id;
}

bool LeaderViewId::operator!=(const LeaderViewId &other) const noexcept
{
    return !(*this == other);
}

bool LeaderViewId::operator<(const LeaderViewId &other) const noexcept
{
    return std::tie(configuration, view_generation, leader_id) <
           std::tie(
               other.configuration, other.view_generation, other.leader_id);
}

uint256_t canonical_membership_digest(
    const std::vector<ReplicaID> &membership)
{
    if (membership.size() > std::numeric_limits<std::uint32_t>::max())
    {
        throw std::length_error("membership exceeds uint32 length");
    }

    auto ordered = membership;
    std::sort(ordered.begin(), ordered.end());

    bytearray_t bytes;
    append_domain(bytes, kMembershipDomain, sizeof(kMembershipDomain) - 1);
    append_big_endian(bytes, static_cast<std::uint32_t>(ordered.size()));
    for (const auto member : ordered)
    {
        append_big_endian(bytes, member);
    }
    return DataStream(bytes).get_hash();
}

EpochDefinitionInput adaptive_v2_epoch_zero_input(
    const std::vector<ReplicaID> &membership,
    std::vector<EpochTreeDefinition> trees)
{
    EpochDefinitionInput input;
    input.schema_version = kEpochDefinitionSchemaVersionV2;
    input.epoch_number = 0;
    input.previous_epoch_digest = uint256_t{};
    input.membership_digest = canonical_membership_digest(membership);
    input.trees = std::move(trees);
    input.activation_height = 0;
    input.generation_seed = 0;
    input.policy_version = "adaptive-v2-bootstrap";
    input.evidence_snapshot_id = "adaptive-v2-bootstrap-epoch-zero";
    input.evidence_cutoff = 0;
    input.epoch_digest.reset();
    return input;
}

bytearray_t canonical_serialize_epoch(const EpochDefinitionInput &input)
{
    if (input.trees.size() > std::numeric_limits<std::uint32_t>::max())
    {
        throw std::length_error("epoch tree count exceeds uint32 length");
    }

    const bool schema_v1 =
        input.schema_version == kEpochDefinitionSchemaVersionV1;
    const bool schema_v2 =
        input.schema_version == kEpochDefinitionSchemaVersionV2;
    if (!schema_v1 && !schema_v2)
    {
        throw std::invalid_argument(
            "unsupported epoch definition schema " +
            std::to_string(input.schema_version));
    }

    bytearray_t bytes;
    if (schema_v1)
        append_domain(
            bytes, kEpochDomainV1, sizeof(kEpochDomainV1) - 1);
    else
        append_domain(
            bytes, kEpochDomainV2, sizeof(kEpochDomainV2) - 1);
    append_big_endian(bytes, input.schema_version);
    append_big_endian(bytes, input.epoch_number);
    append_digest(bytes, input.previous_epoch_digest);
    append_digest(bytes, input.membership_digest);
    if (schema_v1)
    {
        // Retain the historical v1 identity exactly. Adaptive-v2 separates
        // immutable definition identity from its activation schedule.
        append_big_endian(bytes, input.activation_height);
    }
    append_big_endian(bytes, input.generation_seed);
    append_string(bytes, input.policy_version);
    append_string(bytes, input.evidence_snapshot_id);
    append_big_endian(bytes, input.evidence_cutoff);

    const auto trees = canonical_tree_order(input.trees);
    append_big_endian(bytes, static_cast<std::uint32_t>(trees.size()));
    for (const auto *tree : trees)
    {
        if (tree->members_breadth_first.size() >
            std::numeric_limits<std::uint32_t>::max())
        {
            throw std::length_error("tree membership exceeds uint32 length");
        }
        if (tree->wait_exempt_leaves.size() >
            std::numeric_limits<std::uint32_t>::max())
        {
            throw std::length_error(
                "tree wait-exempt set exceeds uint32 length");
        }
        if (schema_v1 && !tree->wait_exempt_leaves.empty())
        {
            throw std::invalid_argument(
                "v1 epoch definition cannot contain wait-exempt leaves");
        }

        append_big_endian(bytes, tree->tree_id);
        append_big_endian(bytes, tree->fanout);
        append_big_endian(bytes, tree->pipeline_stretch);
        append_big_endian(
            bytes,
            static_cast<std::uint32_t>(tree->members_breadth_first.size()));
        for (const auto member : tree->members_breadth_first)
        {
            // Breadth-first order is protocol state and is intentionally not
            // sorted while canonicalizing the surrounding tree collection.
            append_big_endian(bytes, member);
        }
        if (schema_v2)
        {
            auto wait_exempt = tree->wait_exempt_leaves;
            std::sort(wait_exempt.begin(), wait_exempt.end());
            append_big_endian(
                bytes,
                static_cast<std::uint32_t>(wait_exempt.size()));
            for (const auto member : wait_exempt)
                append_big_endian(bytes, member);
        }
    }
    return bytes;
}

uint256_t compute_epoch_digest(const EpochDefinitionInput &input)
{
    return DataStream(canonical_serialize_epoch(input)).get_hash();
}

EpochDefinition::EpochDefinition(
    EpochDefinitionInput input,
    bytearray_t canonical_serialization,
    uint256_t epoch_digest)
    : schema_version_(input.schema_version),
      epoch_number_(input.epoch_number),
      previous_epoch_digest_(std::move(input.previous_epoch_digest)),
      membership_digest_(std::move(input.membership_digest)),
      trees_(std::move(input.trees)),
      activation_height_(input.activation_height),
      generation_seed_(input.generation_seed),
      policy_version_(std::move(input.policy_version)),
      evidence_snapshot_id_(std::move(input.evidence_snapshot_id)),
      evidence_cutoff_(input.evidence_cutoff),
      canonical_serialization_(std::move(canonical_serialization)),
      epoch_digest_(std::move(epoch_digest))
{
}

std::uint32_t EpochDefinition::schema_version() const noexcept
{
    return schema_version_;
}

std::uint32_t EpochDefinition::epoch_number() const noexcept
{
    return epoch_number_;
}

const uint256_t &EpochDefinition::previous_epoch_digest() const noexcept
{
    return previous_epoch_digest_;
}

const uint256_t &EpochDefinition::membership_digest() const noexcept
{
    return membership_digest_;
}

const std::vector<EpochTreeDefinition> &EpochDefinition::trees() const noexcept
{
    return trees_;
}

std::uint64_t EpochDefinition::activation_height() const noexcept
{
    return activation_height_;
}

std::uint64_t EpochDefinition::generation_seed() const noexcept
{
    return generation_seed_;
}

const std::string &EpochDefinition::policy_version() const noexcept
{
    return policy_version_;
}

const std::string &EpochDefinition::evidence_snapshot_id() const noexcept
{
    return evidence_snapshot_id_;
}

std::uint64_t EpochDefinition::evidence_cutoff() const noexcept
{
    return evidence_cutoff_;
}

const bytearray_t &EpochDefinition::canonical_serialization() const noexcept
{
    return canonical_serialization_;
}

const uint256_t &EpochDefinition::epoch_digest() const noexcept
{
    return epoch_digest_;
}

EpochDefinitionInput parse_legacy_epoch_zero(
    const std::string &configuration,
    const std::vector<ReplicaID> &membership)
{
    if (membership.empty())
    {
        throw std::invalid_argument("legacy epoch membership is empty");
    }

    const std::set<ReplicaID> expected_members(
        membership.begin(), membership.end());
    if (expected_members.size() != membership.size())
    {
        throw std::invalid_argument(
            "legacy epoch membership contains duplicate replicas");
    }

    EpochDefinitionInput input;
    input.schema_version = kEpochDefinitionSchemaVersion;
    input.epoch_number = 0;
    input.previous_epoch_digest = uint256_t{};
    input.membership_digest = canonical_membership_digest(membership);
    input.activation_height = 0;
    input.generation_seed = 0;
    input.policy_version = "legacy-static-v1";
    input.evidence_snapshot_id = "legacy-static-epoch-zero";
    input.evidence_cutoff = 0;
    input.epoch_digest.reset();

    std::istringstream lines(configuration);
    std::string line;
    std::size_t line_number = 0;
    while (std::getline(lines, line))
    {
        ++line_number;
        std::istringstream tokens(line);
        std::string fanout_token;
        std::string pipeline_token;
        if (!(tokens >> fanout_token >> pipeline_token))
        {
            throw std::invalid_argument(
                "legacy epoch line " + std::to_string(line_number) +
                ": expected fan:<n> pipe:<n> and breadth-first members");
        }
        if (input.trees.size() == std::numeric_limits<std::uint32_t>::max())
        {
            throw std::length_error("legacy epoch has too many trees");
        }

        EpochTreeDefinition tree;
        tree.tree_id = static_cast<std::uint32_t>(input.trees.size());
        tree.fanout = parse_legacy_tree_option(
            fanout_token, "fan:", "fanout", line_number, 1);
        tree.pipeline_stretch = parse_legacy_tree_option(
            pipeline_token, "pipe:", "pipeline stretch", line_number, 0);

        std::string member_token;
        std::set<ReplicaID> seen_members;
        while (tokens >> member_token)
        {
            const auto member = parse_decimal(
                member_token, "replica ID", line_number);
            if (member > std::numeric_limits<ReplicaID>::max())
            {
                throw std::invalid_argument(
                    "legacy epoch line " + std::to_string(line_number) +
                    ": replica ID is out of range");
            }

            const auto replica_id = static_cast<ReplicaID>(member);
            if (expected_members.count(replica_id) == 0)
            {
                throw std::invalid_argument(
                    "legacy epoch line " + std::to_string(line_number) +
                    ": unknown replica " + std::to_string(replica_id));
            }
            if (!seen_members.insert(replica_id).second)
            {
                throw std::invalid_argument(
                    "legacy epoch line " + std::to_string(line_number) +
                    ": duplicate replica " + std::to_string(replica_id));
            }
            tree.members_breadth_first.push_back(replica_id);
        }

        if (seen_members != expected_members)
        {
            throw std::invalid_argument(
                "legacy epoch line " + std::to_string(line_number) +
                ": tree must contain every fixed member exactly once");
        }
        input.trees.push_back(std::move(tree));
    }

    if (input.trees.empty())
    {
        throw std::invalid_argument("legacy epoch contains no trees");
    }
    return input;
}

} // namespace hotstuff

namespace std
{

size_t hash<hotstuff::ConfigurationId>::operator()(
    const hotstuff::ConfigurationId &id) const noexcept
{
    size_t seed = 0;
    hotstuff::hash_combine(seed, hash<std::uint32_t>{}(id.epoch_number));
    hotstuff::hash_combine(seed, hash<std::uint32_t>{}(id.tree_id));
    hotstuff::hash_combine(seed, hash<hotstuff::uint256_t>{}(id.epoch_digest));
    return seed;
}

size_t hash<hotstuff::ProposalKey>::operator()(
    const hotstuff::ProposalKey &key) const noexcept
{
    size_t seed = hash<hotstuff::ConfigurationId>{}(key.configuration);
    hotstuff::hash_combine(
        seed, hash<hotstuff::uint256_t>{}(key.block_hash));
    return seed;
}

size_t hash<hotstuff::LeaderViewId>::operator()(
    const hotstuff::LeaderViewId &view) const noexcept
{
    size_t seed = hash<hotstuff::ConfigurationId>{}(view.configuration);
    hotstuff::hash_combine(
        seed, hash<std::uint64_t>{}(view.view_generation));
    hotstuff::hash_combine(seed, hash<hotstuff::ReplicaID>{}(view.leader_id));
    return seed;
}

} // namespace std
