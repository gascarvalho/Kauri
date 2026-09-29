#include "hotstuff/operator_capacity_label_envelope.h"

#include <algorithm>
#include <stdexcept>
#include <type_traits>

#include "detail/canonical_wire_codec.h"

namespace hotstuff
{
namespace
{
constexpr char kTopologyDomain[] = "kauri-operator-capacity-e0-topology-v1";
constexpr char kEnvelopeDomain[] = "kauri-operator-capacity-label-envelope-v1";
constexpr std::size_t kMaximumWireStringBytes =
    kMaximumOperatorCapacityIssuerReferenceBytes;

struct WireFailure { OperatorCapacityLabelEnvelopeWireError error; };
using Reader = detail::CanonicalWireReader<WireFailure,
    OperatorCapacityLabelEnvelopeWireError>;

template <typename UInt>
void append_unsigned(bytearray_t &out, UInt value)
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
    append_unsigned(out, tree.tree_id);
    append_unsigned(out, tree.fanout);
    append_unsigned(out, tree.pipeline_stretch);
    append_unsigned(out,
        static_cast<std::uint32_t>(tree.members_breadth_first.size()));
    for (const auto id : tree.members_breadth_first) append_unsigned(out, id);
    append_unsigned(out,
        static_cast<std::uint32_t>(tree.wait_exempt_leaves.size()));
    for (const auto id : tree.wait_exempt_leaves) append_unsigned(out, id);
}

void append_snapshot(bytearray_t &out, const OperatorCapacitySnapshot &snapshot)
{
    append_unsigned(out, snapshot.schema_version);
    append_string(out, snapshot.issuer_reference);
    append_unsigned(out, snapshot.predecessor.epoch_number);
    append_digest(out, snapshot.predecessor.epoch_digest);
    append_unsigned(out, static_cast<std::uint8_t>(snapshot.clock_domain));
    append_unsigned(out, snapshot.valid_from_monotonic_ns);
    append_unsigned(out, snapshot.valid_until_monotonic_ns);
    append_unsigned(out, static_cast<std::uint32_t>(snapshot.labels.size()));
    for (const auto &label : snapshot.labels)
    {
        append_unsigned(out, label.replica_id);
        append_unsigned(out, static_cast<std::uint8_t>(label.capacity));
    }
    append_digest(out, snapshot.canonical_digest);
}

bool canonical_membership(const std::vector<ReplicaID> &members)
{
    return !members.empty() &&
        std::is_sorted(members.begin(), members.end()) &&
        std::adjacent_find(members.begin(), members.end()) == members.end();
}

bool canonical_labels(const std::vector<OperatorCapacityLabel> &labels)
{
    return !labels.empty() &&
        std::is_sorted(labels.begin(), labels.end(),
            [](const auto &left, const auto &right) {
                return left.replica_id < right.replica_id;
            }) &&
        std::adjacent_find(labels.begin(), labels.end(),
            [](const auto &left, const auto &right) {
                return left.replica_id == right.replica_id;
            }) == labels.end();
}

bool valid_arm(OperatorCapacityArm arm)
{
    return arm == OperatorCapacityArm::fast_priority_treatment ||
        arm == OperatorCapacityArm::exact_copy_sham;
}

bool valid_snapshot(const OperatorCapacitySnapshot &snapshot,
                    const std::vector<ReplicaID> &membership,
                    const AdaptationEpochId &epoch0,
                    const std::string &issuer_reference)
{
    if (snapshot.schema_version != kOperatorCapacitySnapshotSchemaVersion ||
        snapshot.issuer_reference != issuer_reference ||
        snapshot.predecessor != epoch0 ||
        snapshot.clock_domain != OperatorCapacityClockDomain::monotonic_raw_ns ||
        snapshot.valid_from_monotonic_ns >= snapshot.valid_until_monotonic_ns ||
        snapshot.labels.size() != membership.size() ||
        !canonical_labels(snapshot.labels) ||
        snapshot.canonical_digest != operator_capacity_snapshot_digest(snapshot))
        return false;
    for (std::size_t index = 0; index < membership.size(); ++index)
    {
        const auto &label = snapshot.labels[index];
        if (label.replica_id != membership[index] ||
            (label.capacity != OperatorCapacityClass::slow &&
             label.capacity != OperatorCapacityClass::fast))
            return false;
    }
    return true;
}

std::string read_string(Reader &reader)
{
    const auto size = reader.integer<std::uint32_t>();
    if (size == 0 || size > kMaximumWireStringBytes)
        throw WireFailure{OperatorCapacityLabelEnvelopeWireError::noncanonical_encoding};
    return reader.string(size);
}

OperatorCapacityLabelEnvelopeDecodeResult reject(
    OperatorCapacityLabelEnvelopeWireError error) noexcept
{ return {error, std::nullopt}; }

OperatorCapacityLabelEnvelope decode_impl(const bytearray_t &payload)
{
    Reader reader(payload, OperatorCapacityLabelEnvelopeWireError::truncated);
    reader.domain(std::string_view(kEnvelopeDomain, sizeof(kEnvelopeDomain) - 1),
        OperatorCapacityLabelEnvelopeWireError::invalid_domain);
    OperatorCapacityLabelEnvelope envelope;
    envelope.schema_version = reader.integer<std::uint32_t>();
    if (envelope.schema_version != kOperatorCapacityLabelEnvelopeSchemaVersion)
        throw WireFailure{OperatorCapacityLabelEnvelopeWireError::unsupported_schema};
    envelope.issuer_id = reader.integer<std::uint32_t>();
    envelope.issuer_reference = read_string(reader);
    const auto member_count = reader.integer<std::uint32_t>();
    if (member_count == 0 || member_count > kMaximumTreePolicyMembers)
        throw WireFailure{OperatorCapacityLabelEnvelopeWireError::noncanonical_encoding};
    envelope.membership.reserve(member_count);
    for (std::uint32_t index = 0; index < member_count; ++index)
        envelope.membership.push_back(reader.integer<ReplicaID>());
    envelope.epoch0.epoch_number = reader.integer<std::uint32_t>();
    envelope.epoch0.epoch_digest = reader.digest();
    envelope.baseline_topology_digest = reader.digest();
    envelope.arm = static_cast<OperatorCapacityArm>(reader.integer<std::uint8_t>());
    auto &snapshot = envelope.capacity_snapshot;
    snapshot.schema_version = reader.integer<std::uint32_t>();
    snapshot.issuer_reference = read_string(reader);
    snapshot.predecessor.epoch_number = reader.integer<std::uint32_t>();
    snapshot.predecessor.epoch_digest = reader.digest();
    snapshot.clock_domain = static_cast<OperatorCapacityClockDomain>(reader.integer<std::uint8_t>());
    snapshot.valid_from_monotonic_ns = reader.integer<std::uint64_t>();
    snapshot.valid_until_monotonic_ns = reader.integer<std::uint64_t>();
    const auto label_count = reader.integer<std::uint32_t>();
    if (label_count == 0 || label_count > kMaximumTreePolicyMembers)
        throw WireFailure{OperatorCapacityLabelEnvelopeWireError::noncanonical_encoding};
    snapshot.labels.reserve(label_count);
    for (std::uint32_t index = 0; index < label_count; ++index)
        snapshot.labels.push_back({reader.integer<ReplicaID>(),
            static_cast<OperatorCapacityClass>(reader.integer<std::uint8_t>())});
    snapshot.canonical_digest = reader.digest();
    const auto signature_bytes = reader.bytes(kOperatorCapacityLabelEnvelopeSignatureBytes);
    if (!reader.empty())
        throw WireFailure{OperatorCapacityLabelEnvelopeWireError::trailing_bytes};
    DataStream signature_stream(signature_bytes);
    try { envelope.signature.unserialize(signature_stream); }
    catch (...) { throw WireFailure{OperatorCapacityLabelEnvelopeWireError::malformed_signature}; }
    if (signature_stream.size() != 0)
        throw WireFailure{OperatorCapacityLabelEnvelopeWireError::malformed_signature};
    return envelope;
}
} // namespace

uint256_t operator_capacity_baseline_topology_digest(
    const AdaptationEpochId &epoch0,
    const std::vector<ReplicaID> &membership,
    const std::vector<EpochTreeDefinition> &trees)
{
    bytearray_t bytes(kTopologyDomain,
        kTopologyDomain + sizeof(kTopologyDomain) - 1);
    append_unsigned(bytes, epoch0.epoch_number);
    append_digest(bytes, epoch0.epoch_digest);
    append_unsigned(bytes, static_cast<std::uint32_t>(membership.size()));
    for (const auto member : membership) append_unsigned(bytes, member);
    append_unsigned(bytes, static_cast<std::uint32_t>(trees.size()));
    for (const auto &tree : trees) append_tree(bytes, tree);
    return DataStream(bytes).get_hash();
}

bytearray_t canonical_operator_capacity_label_envelope_bytes(
    const OperatorCapacityLabelEnvelope &envelope)
{
    bytearray_t bytes(kEnvelopeDomain,
        kEnvelopeDomain + sizeof(kEnvelopeDomain) - 1);
    append_unsigned(bytes, envelope.schema_version);
    append_unsigned(bytes, envelope.issuer_id);
    append_string(bytes, envelope.issuer_reference);
    append_unsigned(bytes, static_cast<std::uint32_t>(envelope.membership.size()));
    for (const auto member : envelope.membership) append_unsigned(bytes, member);
    append_unsigned(bytes, envelope.epoch0.epoch_number);
    append_digest(bytes, envelope.epoch0.epoch_digest);
    append_digest(bytes, envelope.baseline_topology_digest);
    append_unsigned(bytes, static_cast<std::uint8_t>(envelope.arm));
    append_snapshot(bytes, envelope.capacity_snapshot);
    return bytes;
}

uint256_t operator_capacity_label_envelope_digest(
    const OperatorCapacityLabelEnvelope &envelope)
{ return DataStream(canonical_operator_capacity_label_envelope_bytes(envelope)).get_hash(); }

OperatorCapacityLabelEnvelope sign_operator_capacity_label_envelope(
    const std::vector<ReplicaID> &membership,
    const AdaptationEpochId &epoch0,
    const uint256_t &baseline_topology_digest,
    OperatorCapacityArm arm,
    const OperatorCapacitySnapshot &capacity_snapshot,
    std::uint32_t issuer_id,
    const PrivKeySecp256k1 &private_key)
{
    OperatorCapacityLabelEnvelope envelope;
    envelope.issuer_id = issuer_id;
    envelope.issuer_reference = capacity_snapshot.issuer_reference;
    envelope.membership = membership;
    envelope.epoch0 = epoch0;
    envelope.baseline_topology_digest = baseline_topology_digest;
    envelope.arm = arm;
    envelope.capacity_snapshot = capacity_snapshot;
    envelope.signature.sign(operator_capacity_label_envelope_digest(envelope), private_key);
    return envelope;
}

bytearray_t encode_operator_capacity_label_envelope(
    const OperatorCapacityLabelEnvelope &envelope,
    const OperatorCapacityLabelEnvelopeWireLimits &limits)
{
    if (limits.maximum_payload_bytes == 0)
        throw std::invalid_argument("operator-capacity label envelope limit is zero");
    const auto bytes = canonical_operator_capacity_label_envelope_bytes(envelope);
    DataStream stream;
    envelope.signature.serialize(stream);
    const auto signature = static_cast<bytearray_t>(std::move(stream));
    if (signature.size() != kOperatorCapacityLabelEnvelopeSignatureBytes ||
        signature.size() > limits.maximum_payload_bytes ||
        bytes.size() > limits.maximum_payload_bytes - signature.size())
        throw std::length_error("operator-capacity label envelope exceeds limit");
    bytearray_t payload = bytes;
    payload.insert(payload.end(), signature.begin(), signature.end());
    return payload;
}

OperatorCapacityLabelEnvelopeDecodeResult decode_operator_capacity_label_envelope(
    const bytearray_t &payload,
    const OperatorCapacityLabelEnvelopeWireLimits &limits) noexcept
{
    try
    {
        if (limits.maximum_payload_bytes == 0)
            return reject(OperatorCapacityLabelEnvelopeWireError::invalid_limits);
        if (payload.size() > limits.maximum_payload_bytes)
            return reject(OperatorCapacityLabelEnvelopeWireError::payload_too_large);
        auto value = decode_impl(payload);
        if (!canonical_membership(value.membership) ||
            !valid_arm(value.arm) ||
            !valid_snapshot(value.capacity_snapshot, value.membership,
                value.epoch0, value.issuer_reference) ||
            encode_operator_capacity_label_envelope(value, limits) != payload)
            return reject(OperatorCapacityLabelEnvelopeWireError::noncanonical_encoding);
        return {OperatorCapacityLabelEnvelopeWireError::none, std::move(value)};
    }
    catch (const WireFailure &failure) { return reject(failure.error); }
    catch (const std::bad_alloc &) { return reject(OperatorCapacityLabelEnvelopeWireError::allocation_failure); }
    catch (...) { return reject(OperatorCapacityLabelEnvelopeWireError::internal_failure); }
}

std::optional<VerifiedOperatorCapacityLabelEnvelope>
verify_operator_capacity_label_envelope(
    const OperatorCapacityLabelEnvelope &envelope,
    const OperatorCapacityLabelEnvelopeIssuer &issuer,
    const std::vector<ReplicaID> &expected_membership,
    const AdaptationEpochId &expected_epoch0,
    const uint256_t &expected_baseline_topology_digest,
    std::uint64_t now_monotonic_raw_ns) noexcept
{
    try
    {
        if (envelope.schema_version != kOperatorCapacityLabelEnvelopeSchemaVersion ||
            envelope.issuer_id == 0 || issuer.issuer_id == 0 ||
            envelope.issuer_id != issuer.issuer_id ||
            issuer.issuer_reference.empty() ||
            envelope.issuer_reference != issuer.issuer_reference ||
            envelope.membership != expected_membership ||
            !canonical_membership(envelope.membership) ||
            expected_epoch0.epoch_number != 0 ||
            envelope.epoch0 != expected_epoch0 ||
            envelope.baseline_topology_digest != expected_baseline_topology_digest ||
            !valid_arm(envelope.arm) ||
            !valid_snapshot(envelope.capacity_snapshot, envelope.membership,
                envelope.epoch0, envelope.issuer_reference) ||
            envelope.capacity_snapshot.canonical_digest != issuer.approved_capacity_digest ||
            now_monotonic_raw_ns == 0 ||
            now_monotonic_raw_ns < envelope.capacity_snapshot.valid_from_monotonic_ns ||
            now_monotonic_raw_ns > envelope.capacity_snapshot.valid_until_monotonic_ns)
            return std::nullopt;
        auto signature = envelope.signature;
        if (!signature.verify(operator_capacity_label_envelope_digest(envelope),
                issuer.public_key))
            return std::nullopt;
        return VerifiedOperatorCapacityLabelEnvelope(envelope);
    }
    catch (...) { return std::nullopt; }
}

} // namespace hotstuff
