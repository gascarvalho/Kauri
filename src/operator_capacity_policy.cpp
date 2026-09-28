#include "hotstuff/operator_capacity_policy.h"

#include <algorithm>
#include <set>
#include <type_traits>

#include "detail/canonical_wire_codec.h"

namespace hotstuff
{
namespace
{

constexpr char kCapacitySnapshotDomain[] =
    "kauri-operator-capacity-snapshot-v1";
constexpr char kPolicySnapshotDomain[] =
    "kauri-operator-capacity-policy-snapshot-v1";
constexpr char kCapacitySnapshotWireDomain[] =
    "kauri-operator-capacity-snapshot-wire-v1";
struct SnapshotWireFailure { OperatorCapacitySnapshotWireError error; };
using SnapshotReader = detail::CanonicalWireReader<SnapshotWireFailure,
    OperatorCapacitySnapshotWireError>;

template <typename UInt>
void append_big_endian(bytearray_t &out, UInt value)
{
    static_assert(std::is_unsigned<UInt>::value, "unsigned canonical field");
    for (std::size_t shift = sizeof(UInt); shift > 0; --shift)
        out.push_back(static_cast<std::uint8_t>(value >> ((shift - 1) * 8)));
}

void append_digest(bytearray_t &out, const uint256_t &digest)
{
    const auto bytes = static_cast<bytearray_t>(digest);
    out.insert(out.end(), bytes.begin(), bytes.end());
}

void append_string(bytearray_t &out, const std::string &value)
{
    append_big_endian(out, static_cast<std::uint32_t>(value.size()));
    out.insert(out.end(), value.begin(), value.end());
}

bool canonical_members(std::vector<ReplicaID> members)
{
    std::sort(members.begin(), members.end());
    return !members.empty() &&
           std::adjacent_find(members.begin(), members.end()) == members.end();
}

bool canonical_labels(const std::vector<OperatorCapacityLabel> &labels)
{
    return std::is_sorted(labels.begin(), labels.end(),
        [](const auto &left, const auto &right) {
            return left.replica_id < right.replica_id;
        }) && std::adjacent_find(labels.begin(), labels.end(),
        [](const auto &left, const auto &right) {
            return left.replica_id == right.replica_id;
        }) == labels.end();
}

std::string snapshot_wire_string(SnapshotReader &reader)
{
    const auto size = reader.integer<std::uint32_t>();
    if (size > kMaximumOperatorCapacityIssuerReferenceBytes)
        throw SnapshotWireFailure{OperatorCapacitySnapshotWireError::noncanonical_encoding};
    return reader.string(size);
}

bool exact_snapshot_labels(const OperatorCapacitySnapshot &snapshot,
                           const std::vector<ReplicaID> &members)
{
    if (snapshot.schema_version != kOperatorCapacitySnapshotSchemaVersion ||
        snapshot.issuer_reference.empty() ||
        snapshot.issuer_reference.size() >
            kMaximumOperatorCapacityIssuerReferenceBytes ||
        snapshot.clock_domain !=
            OperatorCapacityClockDomain::monotonic_raw_ns ||
        snapshot.valid_from_monotonic_ns >= snapshot.valid_until_monotonic_ns ||
        snapshot.labels.size() != members.size() ||
        snapshot.canonical_digest != operator_capacity_snapshot_digest(snapshot))
        return false;

    std::vector<ReplicaID> labelled;
    labelled.reserve(snapshot.labels.size());
    for (const auto &label : snapshot.labels)
    {
        if (label.capacity != OperatorCapacityClass::fast &&
            label.capacity != OperatorCapacityClass::slow)
            return false;
        labelled.push_back(label.replica_id);
    }
    std::sort(labelled.begin(), labelled.end());
    return labelled == members;
}

bool all_responsive(const AdaptationSnapshot &snapshot,
                    const std::vector<ReplicaID> &members)
{
    const auto &ranking = snapshot.ranking();
    if (ranking.size() != members.size()) return false;
    std::set<ReplicaID> seen;
    for (std::size_t i = 0; i < ranking.size(); ++i)
    {
        const auto &entry = ranking[i];
        if (entry.rank != i || !entry.eligible ||
            entry.classification != ResponsivenessClass::responsive ||
            !std::binary_search(members.begin(), members.end(), entry.replica_id) ||
            !seen.insert(entry.replica_id).second)
            return false;
    }
    return true;
}

bool exact_baseline(const std::vector<EpochTreeDefinition> &trees,
                    const std::vector<ReplicaID> &members,
                    const OperatorCapacityPolicyConfig &config)
{
    if (trees.size() != config.tree_count) return false;
    for (std::size_t index = 0; index < trees.size(); ++index)
    {
        const auto &tree = trees[index];
        auto placed = tree.members_breadth_first;
        std::sort(placed.begin(), placed.end());
        if (tree.tree_id != index || tree.fanout != config.fanout ||
            tree.pipeline_stretch == 0 ||
            tree.pipeline_stretch > kMaximumTreePolicyPipelineStretch ||
            placed != members || !tree.wait_exempt_leaves.empty())
            return false;
    }
    return true;
}

std::string policy_snapshot_id(const AdaptationSnapshot &responsiveness,
                               const OperatorCapacityPolicyConfig &config,
                               const std::vector<EpochTreeDefinition> &trees)
{
    bytearray_t bytes(kPolicySnapshotDomain,
                      kPolicySnapshotDomain + sizeof(kPolicySnapshotDomain) - 1);
    append_string(bytes, responsiveness.snapshot_id());
    append_digest(bytes, config.capacity_snapshot.canonical_digest);
    append_big_endian(bytes, static_cast<std::uint8_t>(config.arm));
    append_string(bytes, config.policy_version);
    append_big_endian(bytes, config.decision_monotonic_ns);
    append_big_endian(bytes, config.fanout);
    append_big_endian(bytes, config.tree_count);
    append_big_endian(bytes, static_cast<std::uint32_t>(trees.size()));
    for (const auto &tree : trees)
    {
        append_big_endian(bytes, tree.tree_id);
        append_big_endian(bytes, tree.fanout);
        append_big_endian(bytes, tree.pipeline_stretch);
        for (const auto member : tree.members_breadth_first)
            append_big_endian(bytes, member);
    }
    return DataStream(bytes).get_hash().to_hex();
}

} // namespace

uint256_t operator_capacity_snapshot_digest(const OperatorCapacitySnapshot &snapshot)
{
    bytearray_t bytes(kCapacitySnapshotDomain,
                      kCapacitySnapshotDomain + sizeof(kCapacitySnapshotDomain) - 1);
    append_big_endian(bytes, snapshot.schema_version);
    append_string(bytes, snapshot.issuer_reference);
    append_big_endian(bytes, snapshot.predecessor.epoch_number);
    append_digest(bytes, snapshot.predecessor.epoch_digest);
    append_big_endian(bytes, static_cast<std::uint8_t>(snapshot.clock_domain));
    append_big_endian(bytes, snapshot.valid_from_monotonic_ns);
    append_big_endian(bytes, snapshot.valid_until_monotonic_ns);
    auto labels = snapshot.labels;
    std::sort(labels.begin(), labels.end(), [](const auto &left, const auto &right) {
        return left.replica_id < right.replica_id;
    });
    append_big_endian(bytes, static_cast<std::uint32_t>(labels.size()));
    for (const auto &label : labels)
    {
        append_big_endian(bytes, label.replica_id);
        append_big_endian(bytes, static_cast<std::uint8_t>(label.capacity));
    }
    return DataStream(bytes).get_hash();
}

bytearray_t encode_operator_capacity_snapshot(
    const OperatorCapacitySnapshot &snapshot,
    const OperatorCapacitySnapshotWireLimits &limits)
{
    if (limits.maximum_payload_bytes == 0)
        throw std::invalid_argument("operator-capacity snapshot limit is zero");
    if (snapshot.schema_version != kOperatorCapacitySnapshotSchemaVersion ||
        snapshot.issuer_reference.empty() ||
        snapshot.issuer_reference.size() > kMaximumOperatorCapacityIssuerReferenceBytes ||
        snapshot.clock_domain != OperatorCapacityClockDomain::monotonic_raw_ns ||
        snapshot.valid_from_monotonic_ns >= snapshot.valid_until_monotonic_ns ||
        snapshot.labels.empty() || snapshot.labels.size() > kMaximumTreePolicyMembers ||
        !canonical_labels(snapshot.labels) ||
        snapshot.canonical_digest != operator_capacity_snapshot_digest(snapshot))
        throw std::invalid_argument("operator-capacity snapshot is not canonical");
    bytearray_t bytes(kCapacitySnapshotWireDomain,
        kCapacitySnapshotWireDomain + sizeof(kCapacitySnapshotWireDomain) - 1);
    append_big_endian(bytes, snapshot.schema_version);
    append_string(bytes, snapshot.issuer_reference);
    append_big_endian(bytes, snapshot.predecessor.epoch_number);
    append_digest(bytes, snapshot.predecessor.epoch_digest);
    append_big_endian(bytes, static_cast<std::uint8_t>(snapshot.clock_domain));
    append_big_endian(bytes, snapshot.valid_from_monotonic_ns);
    append_big_endian(bytes, snapshot.valid_until_monotonic_ns);
    append_big_endian(bytes, static_cast<std::uint32_t>(snapshot.labels.size()));
    for (const auto &label : snapshot.labels)
    {
        append_big_endian(bytes, label.replica_id);
        append_big_endian(bytes, static_cast<std::uint8_t>(label.capacity));
    }
    if (bytes.size() > limits.maximum_payload_bytes)
        throw std::length_error("operator-capacity snapshot exceeds limit");
    return bytes;
}

OperatorCapacitySnapshotDecodeResult decode_operator_capacity_snapshot(
    const bytearray_t &payload,
    const OperatorCapacitySnapshotWireLimits &limits) noexcept
{
    try
    {
        if (limits.maximum_payload_bytes == 0)
            return {OperatorCapacitySnapshotWireError::invalid_limits, std::nullopt};
        if (payload.size() > limits.maximum_payload_bytes)
            return {OperatorCapacitySnapshotWireError::payload_too_large, std::nullopt};
        SnapshotReader reader(payload, OperatorCapacitySnapshotWireError::truncated);
        reader.domain(std::string_view(kCapacitySnapshotWireDomain,
            sizeof(kCapacitySnapshotWireDomain) - 1),
            OperatorCapacitySnapshotWireError::invalid_domain);
        OperatorCapacitySnapshot snapshot;
        snapshot.schema_version = reader.integer<std::uint32_t>();
        if (snapshot.schema_version != kOperatorCapacitySnapshotSchemaVersion)
            return {OperatorCapacitySnapshotWireError::unsupported_schema, std::nullopt};
        snapshot.issuer_reference = snapshot_wire_string(reader);
        snapshot.predecessor.epoch_number = reader.integer<std::uint32_t>();
        snapshot.predecessor.epoch_digest = reader.digest();
        snapshot.clock_domain = static_cast<OperatorCapacityClockDomain>(reader.integer<std::uint8_t>());
        snapshot.valid_from_monotonic_ns = reader.integer<std::uint64_t>();
        snapshot.valid_until_monotonic_ns = reader.integer<std::uint64_t>();
        const auto count = reader.integer<std::uint32_t>();
        if (count == 0 || count > kMaximumTreePolicyMembers)
            return {OperatorCapacitySnapshotWireError::noncanonical_encoding, std::nullopt};
        snapshot.labels.reserve(count);
        for (std::uint32_t index = 0; index < count; ++index)
        {
            const auto replica_id = reader.integer<ReplicaID>();
            const auto capacity = static_cast<OperatorCapacityClass>(reader.integer<std::uint8_t>());
            if (capacity != OperatorCapacityClass::slow && capacity != OperatorCapacityClass::fast)
                return {OperatorCapacitySnapshotWireError::noncanonical_encoding, std::nullopt};
            snapshot.labels.push_back({replica_id, capacity});
        }
        if (!reader.empty())
            return {OperatorCapacitySnapshotWireError::trailing_bytes, std::nullopt};
        snapshot.canonical_digest = operator_capacity_snapshot_digest(snapshot);
        if (snapshot.issuer_reference.empty() ||
            snapshot.clock_domain != OperatorCapacityClockDomain::monotonic_raw_ns ||
            snapshot.valid_from_monotonic_ns >= snapshot.valid_until_monotonic_ns ||
            !canonical_labels(snapshot.labels) ||
            encode_operator_capacity_snapshot(snapshot, limits) != payload)
            return {OperatorCapacitySnapshotWireError::noncanonical_encoding, std::nullopt};
        return {OperatorCapacitySnapshotWireError::none, std::move(snapshot)};
    }
    catch (const SnapshotWireFailure &failure)
    { return {failure.error, std::nullopt}; }
    catch (const std::bad_alloc &)
    { return {OperatorCapacitySnapshotWireError::allocation_failure, std::nullopt}; }
    catch (...)
    { return {OperatorCapacitySnapshotWireError::internal_failure, std::nullopt}; }
}

OperatorCapacityPlacementResult build_operator_capacity_placement(
    const std::vector<ReplicaID> &membership,
    const AdaptationSnapshot &responsiveness_snapshot,
    const OperatorCapacityPolicyConfig &config) noexcept
{
    try
    {
        if (membership.size() > kMaximumTreePolicyMembers)
            return {OperatorCapacityPolicyStatus::invalid_config, {}, {}};
        auto members = membership;
        std::sort(members.begin(), members.end());
        if (!canonical_members(members) || config.policy_version.empty() ||
            config.policy_version.size() > kMaximumTreePolicyVersionBytes ||
            config.fanout == 0 || config.fanout > kMaximumTreePolicyFanout ||
            config.tree_count == 0 ||
            config.tree_count > kMaximumTreePolicyTrees ||
            config.decision_clock_domain !=
                OperatorCapacityClockDomain::monotonic_raw_ns ||
            config.decision_monotonic_ns == 0 ||
            config.expected_responsiveness_snapshot_id.empty() ||
            config.expected_evidence_cutoff == 0)
            return {OperatorCapacityPolicyStatus::invalid_config, {}, {}};
        if (!exact_snapshot_labels(config.capacity_snapshot, members) ||
            config.capacity_snapshot.clock_domain !=
                config.decision_clock_domain ||
            config.capacity_snapshot.canonical_digest !=
                config.approved_capacity_digest ||
            config.decision_monotonic_ns < config.capacity_snapshot.valid_from_monotonic_ns ||
            config.decision_monotonic_ns > config.capacity_snapshot.valid_until_monotonic_ns)
            return {OperatorCapacityPolicyStatus::invalid_capacity_snapshot, {}, {}};
        if (config.capacity_snapshot.predecessor != responsiveness_snapshot.epoch())
            return {OperatorCapacityPolicyStatus::snapshot_epoch_mismatch, {}, {}};
        if (responsiveness_snapshot.snapshot_id() !=
                config.expected_responsiveness_snapshot_id ||
            responsiveness_snapshot.evidence_cutoff() !=
                config.expected_evidence_cutoff)
            return {OperatorCapacityPolicyStatus::responsiveness_snapshot_mismatch, {}, {}};
        if (!all_responsive(responsiveness_snapshot, members))
            return {OperatorCapacityPolicyStatus::not_all_responsive, {}, {}};
        if (!exact_baseline(config.baseline_trees, members, config))
            return {OperatorCapacityPolicyStatus::invalid_exact_copy_baseline, {}, {}};

        std::vector<EpochTreeDefinition> trees;
        if (config.arm == OperatorCapacityArm::exact_copy_sham)
        {
            trees = config.baseline_trees;
        }
        else if (config.arm == OperatorCapacityArm::fast_priority_treatment)
        {
            std::vector<ReplicaID> fast;
            std::vector<ReplicaID> slow;
            fast.reserve(members.size());
            slow.reserve(members.size());
            for (const auto capacity : {OperatorCapacityClass::fast, OperatorCapacityClass::slow})
                for (const auto member : members)
                {
                    const auto label = std::find_if(config.capacity_snapshot.labels.begin(),
                        config.capacity_snapshot.labels.end(), [member](const auto &value) {
                            return value.replica_id == member;
                    });
                    if (label != config.capacity_snapshot.labels.end() && label->capacity == capacity)
                    {
                        if (capacity == OperatorCapacityClass::fast)
                            fast.push_back(member);
                        else
                            slow.push_back(member);
                    }
                }
            if (fast.size() < config.tree_count)
                return {OperatorCapacityPolicyStatus::insufficient_fast_members, {}, {}};
            for (std::uint32_t tree = 0; tree < config.tree_count; ++tree)
            {
                EpochTreeDefinition definition = config.baseline_trees[tree];
                definition.members_breadth_first.clear();
                definition.members_breadth_first.reserve(members.size());
                for (std::size_t offset = 0; offset < fast.size(); ++offset)
                    definition.members_breadth_first.push_back(
                        fast[(static_cast<std::size_t>(tree) + offset) % fast.size()]);
                for (std::size_t offset = 0; offset < slow.size(); ++offset)
                    definition.members_breadth_first.push_back(
                        slow[(static_cast<std::size_t>(tree) + offset) % slow.size()]);
                trees.push_back(std::move(definition));
            }
        }
        else return {OperatorCapacityPolicyStatus::invalid_config, {}, {}};

        return {OperatorCapacityPolicyStatus::selected,
                policy_snapshot_id(responsiveness_snapshot, config, trees),
                std::move(trees)};
    }
    catch (...) { return {OperatorCapacityPolicyStatus::invalid_config, {}, {}}; }
}

} // namespace hotstuff
