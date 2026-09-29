#include "hotstuff/operator_capacity_authorization.h"

#include <algorithm>
#include <type_traits>

#include "detail/canonical_wire_codec.h"

namespace hotstuff
{
namespace
{
constexpr char kAuthorizationDomain[] = "kauri-operator-capacity-authorization-v1";
constexpr std::size_t kMaximumWireStringBytes = 128;
struct WireFailure { OperatorCapacityAuthorizationWireError error; };
using Reader = detail::CanonicalWireReader<WireFailure, OperatorCapacityAuthorizationWireError>;

template <typename UInt> void append_unsigned(bytearray_t &out, UInt value)
{
    static_assert(std::is_unsigned<UInt>::value, "unsigned canonical field");
    for (std::size_t shift = sizeof(UInt); shift > 0; --shift)
        out.push_back(static_cast<std::uint8_t>(value >> ((shift - 1) * 8)));
}
void append_digest(bytearray_t &out, const uint256_t &value)
{
    const auto bytes = static_cast<bytearray_t>(value);
    out.insert(out.end(), bytes.begin(), bytes.end());
}
void append_string(bytearray_t &out, const std::string &value)
{
    append_unsigned(out, static_cast<std::uint32_t>(value.size()));
    out.insert(out.end(), value.begin(), value.end());
}
void append_tree(bytearray_t &out, const EpochTreeDefinition &tree)
{
    append_unsigned(out, tree.tree_id); append_unsigned(out, tree.fanout);
    append_unsigned(out, tree.pipeline_stretch);
    append_unsigned(out, static_cast<std::uint32_t>(tree.members_breadth_first.size()));
    for (const auto id : tree.members_breadth_first) append_unsigned(out, id);
    append_unsigned(out, static_cast<std::uint32_t>(tree.wait_exempt_leaves.size()));
    for (const auto id : tree.wait_exempt_leaves) append_unsigned(out, id);
}
void append_snapshot(bytearray_t &out, const OperatorCapacitySnapshot &snapshot)
{
    append_unsigned(out, snapshot.schema_version); append_string(out, snapshot.issuer_reference);
    append_unsigned(out, snapshot.predecessor.epoch_number);
    append_digest(out, snapshot.predecessor.epoch_digest);
    append_unsigned(out, static_cast<std::uint8_t>(snapshot.clock_domain));
    append_unsigned(out, snapshot.valid_from_monotonic_ns); append_unsigned(out, snapshot.valid_until_monotonic_ns);
    auto labels = snapshot.labels;
    std::sort(labels.begin(), labels.end(), [](const auto &a, const auto &b) { return a.replica_id < b.replica_id; });
    append_unsigned(out, static_cast<std::uint32_t>(labels.size()));
    for (const auto &label : labels) { append_unsigned(out, label.replica_id); append_unsigned(out, static_cast<std::uint8_t>(label.capacity)); }
    append_digest(out, snapshot.canonical_digest);
}
bool canonical_membership(const std::vector<ReplicaID> &members)
{
    return !members.empty() && std::is_sorted(members.begin(), members.end()) &&
           std::adjacent_find(members.begin(), members.end()) == members.end();
}
OperatorCapacityAuthorizationDecodeResult reject(OperatorCapacityAuthorizationWireError error) noexcept
{ return {error, std::nullopt}; }
std::string read_string(Reader &reader)
{
    const auto size = reader.integer<std::uint32_t>();
    if (size > kMaximumWireStringBytes) throw WireFailure{OperatorCapacityAuthorizationWireError::noncanonical_encoding};
    return reader.string(size);
}
OperatorCapacityAuthorization decode_impl(const bytearray_t &payload)
{
    Reader reader(payload, OperatorCapacityAuthorizationWireError::truncated);
    reader.domain(std::string_view(kAuthorizationDomain, sizeof(kAuthorizationDomain) - 1), OperatorCapacityAuthorizationWireError::invalid_domain);
    OperatorCapacityAuthorization result;
    result.schema_version = reader.integer<std::uint32_t>();
    if (result.schema_version != kOperatorCapacityAuthorizationSchemaVersion) throw WireFailure{OperatorCapacityAuthorizationWireError::unsupported_schema};
    result.issuer_id = reader.integer<std::uint32_t>();
    const auto member_count = reader.integer<std::uint32_t>();
    if (member_count == 0 || member_count > kMaximumTreePolicyMembers) throw WireFailure{OperatorCapacityAuthorizationWireError::noncanonical_encoding};
    result.membership.reserve(member_count);
    for (std::uint32_t i = 0; i < member_count; ++i) result.membership.push_back(reader.integer<ReplicaID>());
    auto &policy = result.policy;
    policy.arm = static_cast<OperatorCapacityArm>(reader.integer<std::uint8_t>());
    policy.policy_version = read_string(reader);
    policy.decision_clock_domain = static_cast<OperatorCapacityClockDomain>(reader.integer<std::uint8_t>());
    policy.decision_monotonic_ns = reader.integer<std::uint64_t>();
    policy.approved_capacity_digest = reader.digest(); policy.expected_responsiveness_snapshot_id = read_string(reader);
    policy.expected_evidence_cutoff = reader.integer<std::uint64_t>(); policy.fanout = reader.integer<std::uint32_t>(); policy.tree_count = reader.integer<std::uint32_t>();
    auto &snapshot = policy.capacity_snapshot;
    snapshot.schema_version = reader.integer<std::uint32_t>(); snapshot.issuer_reference = read_string(reader);
    snapshot.predecessor.epoch_number = reader.integer<std::uint32_t>(); snapshot.predecessor.epoch_digest = reader.digest();
    snapshot.clock_domain = static_cast<OperatorCapacityClockDomain>(reader.integer<std::uint8_t>());
    snapshot.valid_from_monotonic_ns = reader.integer<std::uint64_t>(); snapshot.valid_until_monotonic_ns = reader.integer<std::uint64_t>();
    const auto label_count = reader.integer<std::uint32_t>();
    if (label_count > kMaximumTreePolicyMembers) throw WireFailure{OperatorCapacityAuthorizationWireError::noncanonical_encoding};
    snapshot.labels.reserve(label_count);
    for (std::uint32_t i = 0; i < label_count; ++i) snapshot.labels.push_back({reader.integer<ReplicaID>(), static_cast<OperatorCapacityClass>(reader.integer<std::uint8_t>())});
    snapshot.canonical_digest = reader.digest();
    const auto tree_count = reader.integer<std::uint32_t>();
    if (tree_count > kMaximumTreePolicyTrees) throw WireFailure{OperatorCapacityAuthorizationWireError::noncanonical_encoding};
    policy.baseline_trees.reserve(tree_count);
    for (std::uint32_t i = 0; i < tree_count; ++i) {
        EpochTreeDefinition tree; tree.tree_id = reader.integer<std::uint32_t>(); tree.fanout = reader.integer<std::uint32_t>(); tree.pipeline_stretch = reader.integer<std::uint32_t>();
        const auto placed = reader.integer<std::uint32_t>(); if (placed > kMaximumTreePolicyMembers) throw WireFailure{OperatorCapacityAuthorizationWireError::noncanonical_encoding};
        tree.members_breadth_first.reserve(placed); for (std::uint32_t j = 0; j < placed; ++j) tree.members_breadth_first.push_back(reader.integer<ReplicaID>());
        const auto exempt = reader.integer<std::uint32_t>(); if (exempt > kMaximumTreePolicyMembers) throw WireFailure{OperatorCapacityAuthorizationWireError::noncanonical_encoding};
        tree.wait_exempt_leaves.reserve(exempt); for (std::uint32_t j = 0; j < exempt; ++j) tree.wait_exempt_leaves.push_back(reader.integer<ReplicaID>());
        policy.baseline_trees.push_back(std::move(tree));
    }
    const auto signature_bytes = reader.bytes(kOperatorCapacityAuthorizationSignatureBytes);
    if (!reader.empty()) throw WireFailure{OperatorCapacityAuthorizationWireError::trailing_bytes};
    DataStream signature_stream(signature_bytes);
    try { result.signature.unserialize(signature_stream); }
    catch (...) { throw WireFailure{OperatorCapacityAuthorizationWireError::malformed_signature}; }
    if (signature_stream.size() != 0) throw WireFailure{OperatorCapacityAuthorizationWireError::malformed_signature};
    return result;
}
} // namespace

bytearray_t canonical_operator_capacity_authorization_bytes(const OperatorCapacityAuthorization &authorization)
{
    bytearray_t bytes(kAuthorizationDomain, kAuthorizationDomain + sizeof(kAuthorizationDomain) - 1);
    append_unsigned(bytes, authorization.schema_version); append_unsigned(bytes, authorization.issuer_id);
    append_unsigned(bytes, static_cast<std::uint32_t>(authorization.membership.size()));
    for (const auto id : authorization.membership) append_unsigned(bytes, id);
    const auto &policy = authorization.policy;
    append_unsigned(bytes, static_cast<std::uint8_t>(policy.arm)); append_string(bytes, policy.policy_version);
    append_unsigned(bytes, static_cast<std::uint8_t>(policy.decision_clock_domain)); append_unsigned(bytes, policy.decision_monotonic_ns);
    append_digest(bytes, policy.approved_capacity_digest); append_string(bytes, policy.expected_responsiveness_snapshot_id);
    append_unsigned(bytes, policy.expected_evidence_cutoff); append_unsigned(bytes, policy.fanout); append_unsigned(bytes, policy.tree_count);
    append_snapshot(bytes, policy.capacity_snapshot);
    append_unsigned(bytes, static_cast<std::uint32_t>(policy.baseline_trees.size()));
    for (const auto &tree : policy.baseline_trees) append_tree(bytes, tree);
    return bytes;
}
uint256_t operator_capacity_authorization_digest(const OperatorCapacityAuthorization &authorization)
{ return DataStream(canonical_operator_capacity_authorization_bytes(authorization)).get_hash(); }
OperatorCapacityAuthorization authorize_operator_capacity(const std::vector<ReplicaID> &membership, const OperatorCapacityPolicyConfig &policy, std::uint32_t issuer_id, const PrivKeySecp256k1 &key)
{
    OperatorCapacityAuthorization authorization;
    authorization.issuer_id = issuer_id; authorization.membership = membership; authorization.policy = policy;
    authorization.signature.sign(operator_capacity_authorization_digest(authorization), key);
    return authorization;
}
bytearray_t encode_operator_capacity_authorization(const OperatorCapacityAuthorization &authorization, const OperatorCapacityAuthorizationWireLimits &limits)
{
    if (limits.maximum_payload_bytes == 0) throw std::invalid_argument("operator-capacity authorization limit is zero");
    auto bytes = canonical_operator_capacity_authorization_bytes(authorization);
    DataStream stream; authorization.signature.serialize(stream);
    const auto signature = static_cast<bytearray_t>(std::move(stream));
    if (signature.size() != kOperatorCapacityAuthorizationSignatureBytes ||
        signature.size() > limits.maximum_payload_bytes ||
        bytes.size() > limits.maximum_payload_bytes - signature.size())
        throw std::length_error("operator-capacity authorization exceeds limit");
    bytes.insert(bytes.end(), signature.begin(), signature.end()); return bytes;
}
OperatorCapacityAuthorizationDecodeResult decode_operator_capacity_authorization(const bytearray_t &payload, const OperatorCapacityAuthorizationWireLimits &limits) noexcept
{
    try {
        if (limits.maximum_payload_bytes == 0) return reject(OperatorCapacityAuthorizationWireError::invalid_limits);
        if (payload.size() > limits.maximum_payload_bytes) return reject(OperatorCapacityAuthorizationWireError::payload_too_large);
        auto value = decode_impl(payload);
        if (!canonical_membership(value.membership) || encode_operator_capacity_authorization(value, limits) != payload) return reject(OperatorCapacityAuthorizationWireError::noncanonical_encoding);
        return {OperatorCapacityAuthorizationWireError::none, std::move(value)};
    } catch (const WireFailure &failure) { return reject(failure.error); }
      catch (const std::bad_alloc &) { return reject(OperatorCapacityAuthorizationWireError::allocation_failure); }
      catch (...) { return reject(OperatorCapacityAuthorizationWireError::internal_failure); }
}
std::optional<VerifiedOperatorCapacityAuthorization> verify_operator_capacity_authorization(const OperatorCapacityAuthorization &authorization, const OperatorCapacityIssuer &issuer, const std::vector<ReplicaID> &expected_membership) noexcept
{
    try {
        const auto &policy = authorization.policy;
        const auto &label_reference =
            issuer.approved_label_issuer_reference.empty()
                ? issuer.issuer_reference
                : issuer.approved_label_issuer_reference;
        if (authorization.schema_version != kOperatorCapacityAuthorizationSchemaVersion || authorization.issuer_id == 0 || issuer.issuer_id == 0 || authorization.issuer_id != issuer.issuer_id || issuer.issuer_reference.empty() || label_reference.empty() || authorization.membership != expected_membership || !canonical_membership(authorization.membership) || policy.capacity_snapshot.issuer_reference != label_reference || policy.capacity_snapshot.canonical_digest != issuer.approved_capacity_digest || policy.approved_capacity_digest != issuer.approved_capacity_digest || policy.capacity_snapshot.canonical_digest != operator_capacity_snapshot_digest(policy.capacity_snapshot) || policy.capacity_snapshot.clock_domain != OperatorCapacityClockDomain::monotonic_raw_ns || policy.decision_clock_domain != OperatorCapacityClockDomain::monotonic_raw_ns || policy.decision_monotonic_ns < policy.capacity_snapshot.valid_from_monotonic_ns || policy.decision_monotonic_ns > policy.capacity_snapshot.valid_until_monotonic_ns || (policy.arm != OperatorCapacityArm::exact_copy_sham && policy.arm != OperatorCapacityArm::fast_priority_treatment)) return std::nullopt;
        auto signature = authorization.signature;
        if (!signature.verify(operator_capacity_authorization_digest(authorization), issuer.public_key)) return std::nullopt;
        return VerifiedOperatorCapacityAuthorization(authorization);
    } catch (...) { return std::nullopt; }
}
} // namespace hotstuff
