#include "hotstuff/epoch_wire.h"

#include <algorithm>
#include <limits>
#include <set>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>

namespace hotstuff
{
namespace
{

struct WireFailure
{
    EpochWireError error;
};

[[noreturn]] void fail(EpochWireError error)
{
    throw WireFailure{error};
}

bool valid_limits(const EpochWireLimits &limits) noexcept
{
    return limits.maximum_payload_bytes != 0 &&
           limits.maximum_trees != 0 &&
           limits.maximum_members_per_tree != 0 &&
           limits.maximum_string_bytes != 0 &&
           limits.maximum_wait_exempt_leaves_per_tree != 0;
}

void require_encode_limits(const EpochWireLimits &limits)
{
    if (!valid_limits(limits))
    {
        throw std::invalid_argument("epoch wire limits must be nonzero");
    }
}

class Writer final
{
public:
    explicit Writer(std::size_t maximum_size) : maximum_size_(maximum_size) {}

    template <typename UInt>
    void integer(UInt value)
    {
        static_assert(std::is_unsigned<UInt>::value,
                      "epoch wire integers must be unsigned");
        ensure(sizeof(UInt));
        for (std::size_t shift = sizeof(UInt); shift > 0; --shift)
        {
            bytes_.push_back(static_cast<std::uint8_t>(
                value >> ((shift - 1) * 8)));
        }
    }

    void digest(const uint256_t &value)
    {
        const bytearray_t bytes = static_cast<bytearray_t>(value);
        if (bytes.size() != 32)
        {
            throw std::logic_error("epoch wire digest is not 32 bytes");
        }
        append(bytes.data(), bytes.size());
    }

    void string(
        const std::string &value,
        std::uint32_t maximum_string_bytes)
    {
        if (value.size() > maximum_string_bytes ||
            value.size() > std::numeric_limits<std::uint32_t>::max())
        {
            throw std::length_error("epoch wire string exceeds limit");
        }
        integer(static_cast<std::uint32_t>(value.size()));
        append(
            reinterpret_cast<const std::uint8_t *>(value.data()),
            value.size());
    }

    bytearray_t finish() &&
    {
        return std::move(bytes_);
    }

private:
    void ensure(std::size_t additional)
    {
        if (additional > maximum_size_ - bytes_.size())
        {
            throw std::length_error("epoch wire payload exceeds limit");
        }
    }

    void append(const std::uint8_t *data, std::size_t size)
    {
        ensure(size);
        bytes_.insert(bytes_.end(), data, data + size);
    }

    const std::size_t maximum_size_;
    bytearray_t bytes_;
};

class Reader final
{
public:
    explicit Reader(const bytearray_t &bytes) : bytes_(bytes) {}

    template <typename UInt>
    UInt integer()
    {
        static_assert(std::is_unsigned<UInt>::value,
                      "epoch wire integers must be unsigned");
        require(sizeof(UInt));
        UInt value = 0;
        for (std::size_t index = 0; index < sizeof(UInt); ++index)
        {
            value = static_cast<UInt>(
                (value << 8) | bytes_[offset_ + index]);
        }
        offset_ += sizeof(UInt);
        return value;
    }

    uint256_t digest()
    {
        constexpr std::size_t digest_size = 32;
        require(digest_size);
        const uint256_t value(bytes_.data() + offset_);
        offset_ += digest_size;
        return value;
    }

    std::string string(std::uint32_t maximum_string_bytes)
    {
        const auto size = integer<std::uint32_t>();
        if (size > maximum_string_bytes)
        {
            fail(EpochWireError::string_length_exceeded);
        }
        require(size);
        const auto *const begin = reinterpret_cast<const char *>(
            bytes_.data() + offset_);
        std::string value(begin, begin + size);
        offset_ += size;
        return value;
    }

    std::size_t remaining() const noexcept
    {
        return bytes_.size() - offset_;
    }

    bool empty() const noexcept
    {
        return offset_ == bytes_.size();
    }

    void require(std::size_t size) const
    {
        if (size > remaining())
        {
            fail(EpochWireError::truncated);
        }
    }

private:
    const bytearray_t &bytes_;
    std::size_t offset_{0};
};

void append_header(
    Writer &writer,
    std::uint32_t schema,
    EpochProtocolMode mode,
    EpochWireKind kind)
{
    writer.integer(schema);
    writer.integer(static_cast<std::uint8_t>(mode));
    writer.integer(static_cast<std::uint8_t>(kind));
}

std::optional<std::uint32_t> wire_schema_for_mode(
    EpochProtocolMode mode) noexcept
{
    switch (mode)
    {
    case EpochProtocolMode::adaptive_v1:
        return kEpochWireSchemaVersionV1;
    case EpochProtocolMode::adaptive_v2:
        return kEpochWireSchemaVersionV2;
    case EpochProtocolMode::legacy_static:
        return std::nullopt;
    }
    return std::nullopt;
}

std::optional<std::uint32_t> definition_schema_for_mode(
    EpochProtocolMode mode) noexcept
{
    switch (mode)
    {
    case EpochProtocolMode::adaptive_v1:
        return kEpochDefinitionSchemaVersionV1;
    case EpochProtocolMode::adaptive_v2:
        return kEpochDefinitionSchemaVersionV2;
    case EpochProtocolMode::legacy_static:
        return std::nullopt;
    }
    return std::nullopt;
}

bool mode_supports_kind(
    EpochProtocolMode mode,
    EpochWireKind kind) noexcept
{
    if (mode == EpochProtocolMode::adaptive_v1)
        return true;
    if (mode == EpochProtocolMode::adaptive_v2)
    {
        // Adaptive-v2 activation is consensus ordered. The v1 Arm and its
        // recovery status are deliberately unavailable in this mode.
        return kind == EpochWireKind::stage_epoch_definition ||
               kind == EpochWireKind::stage_ack;
    }
    return false;
}

void require_encode_header_contract(
    std::uint32_t schema,
    EpochProtocolMode mode,
    EpochWireKind kind)
{
    const auto expected_schema = wire_schema_for_mode(mode);
    if (!expected_schema.has_value())
    {
        throw std::invalid_argument(
            "epoch wire encoder requires an adaptive protocol mode");
    }
    if (schema != *expected_schema)
    {
        throw std::invalid_argument(
            "epoch wire schema does not match protocol mode");
    }
    if (!mode_supports_kind(mode, kind))
    {
        throw std::invalid_argument(
            "epoch wire kind is unsupported for protocol mode");
    }
}

void read_header(
    Reader &reader,
    EpochProtocolMode expected_mode,
    EpochWireKind expected_kind)
{
    const auto schema = reader.integer<std::uint32_t>();
    const auto mode = static_cast<EpochProtocolMode>(
        reader.integer<std::uint8_t>());
    const auto expected_schema = wire_schema_for_mode(expected_mode);
    if (!expected_schema.has_value() || mode != expected_mode)
    {
        fail(EpochWireError::mode_mismatch);
    }
    if (schema != *expected_schema)
    {
        fail(EpochWireError::unsupported_schema);
    }
    const auto kind = static_cast<EpochWireKind>(
        reader.integer<std::uint8_t>());
    if (kind != expected_kind)
    {
        fail(EpochWireError::unexpected_kind);
    }
    if (!mode_supports_kind(mode, kind))
    {
        fail(EpochWireError::unsupported_kind_for_mode);
    }
}

void append_identity(
    Writer &writer,
    const EpochActivationIdentity &identity)
{
    writer.integer(identity.predecessor_epoch_number);
    writer.digest(identity.predecessor_epoch_digest);
    writer.integer(identity.successor_epoch_number);
    writer.digest(identity.successor_epoch_digest);
    writer.integer(identity.activation_height);
}

EpochActivationIdentity read_identity(Reader &reader)
{
    EpochActivationIdentity identity;
    identity.predecessor_epoch_number = reader.integer<std::uint32_t>();
    identity.predecessor_epoch_digest = reader.digest();
    identity.successor_epoch_number = reader.integer<std::uint32_t>();
    identity.successor_epoch_digest = reader.digest();
    identity.activation_height = reader.integer<std::uint64_t>();
    return identity;
}

bool identity_matches_definition(
    const EpochActivationIdentity &identity,
    const EpochDefinitionInput &definition,
    EpochProtocolMode mode)
{
    const auto expected_schema = definition_schema_for_mode(mode);
    if (!expected_schema.has_value() ||
        definition.schema_version != *expected_schema ||
        definition.epoch_number != identity.successor_epoch_number ||
        definition.previous_epoch_digest !=
            identity.predecessor_epoch_digest ||
        definition.activation_height != identity.activation_height)
    {
        return false;
    }

    const auto computed_digest = compute_epoch_digest(definition);
    if (computed_digest != identity.successor_epoch_digest)
    {
        return false;
    }
    return !definition.epoch_digest ||
           *definition.epoch_digest == computed_digest;
}

EpochDefinitionInput normalized_definition(
    const StageEpochDefinition &value,
    const EpochWireLimits &limits)
{
    if (!identity_matches_definition(
            value.activation, value.definition, value.protocol_mode))
    {
        throw std::invalid_argument(
            "epoch activation identity does not match definition");
    }
    if (value.definition.trees.size() > limits.maximum_trees)
    {
        throw std::length_error("epoch wire tree count exceeds limit");
    }
    if (value.definition.policy_version.size() >
            limits.maximum_string_bytes ||
        value.definition.evidence_snapshot_id.size() >
            limits.maximum_string_bytes)
    {
        throw std::length_error("epoch wire string exceeds limit");
    }

    auto definition = value.definition;
    std::stable_sort(
        definition.trees.begin(), definition.trees.end(),
        [](const auto &left, const auto &right) {
            return left.tree_id < right.tree_id;
        });
    const auto duplicate_tree = std::adjacent_find(
        definition.trees.begin(), definition.trees.end(),
        [](const auto &left, const auto &right) {
            return left.tree_id == right.tree_id;
        });
    if (duplicate_tree != definition.trees.end())
    {
        throw std::invalid_argument(
            "epoch wire definition contains duplicate tree IDs");
    }
    for (auto &tree : definition.trees)
    {
        if (tree.members_breadth_first.size() >
            limits.maximum_members_per_tree)
        {
            throw std::length_error(
                "epoch wire member count exceeds limit");
        }
        const std::set<ReplicaID> unique_members(
            tree.members_breadth_first.begin(),
            tree.members_breadth_first.end());
        if (unique_members.size() != tree.members_breadth_first.size())
        {
            throw std::invalid_argument(
                "epoch wire definition contains duplicate members");
        }
        if (tree.wait_exempt_leaves.size() >
            limits.maximum_wait_exempt_leaves_per_tree)
        {
            throw std::length_error(
                "epoch wire wait-exempt count exceeds limit");
        }
        if (value.protocol_mode == EpochProtocolMode::adaptive_v1 &&
            !tree.wait_exempt_leaves.empty())
        {
            throw std::invalid_argument(
                "adaptive-v1 definition contains wait-exempt leaves");
        }
        std::sort(
            tree.wait_exempt_leaves.begin(),
            tree.wait_exempt_leaves.end());
        if (std::adjacent_find(
                tree.wait_exempt_leaves.begin(),
                tree.wait_exempt_leaves.end()) !=
            tree.wait_exempt_leaves.end())
        {
            throw std::invalid_argument(
                "epoch wire definition contains duplicate wait-exempt replicas");
        }
    }
    definition.epoch_digest = value.activation.successor_epoch_digest;
    return definition;
}

void append_definition(
    Writer &writer,
    const EpochDefinitionInput &definition,
    EpochProtocolMode mode,
    const EpochWireLimits &limits)
{
    writer.integer(definition.schema_version);
    writer.integer(definition.epoch_number);
    writer.digest(definition.previous_epoch_digest);
    writer.digest(definition.membership_digest);
    if (mode != EpochProtocolMode::adaptive_v2)
        writer.integer(definition.activation_height);
    writer.integer(definition.generation_seed);
    writer.string(definition.policy_version, limits.maximum_string_bytes);
    writer.string(
        definition.evidence_snapshot_id,
        limits.maximum_string_bytes);
    writer.integer(definition.evidence_cutoff);
    writer.integer(static_cast<std::uint32_t>(definition.trees.size()));
    for (const auto &tree : definition.trees)
    {
        writer.integer(tree.tree_id);
        writer.integer(tree.fanout);
        writer.integer(tree.pipeline_stretch);
        writer.integer(static_cast<std::uint32_t>(
            tree.members_breadth_first.size()));
        for (const auto member : tree.members_breadth_first)
        {
            writer.integer(member);
        }
        if (mode == EpochProtocolMode::adaptive_v2)
        {
            writer.integer(static_cast<std::uint32_t>(
                tree.wait_exempt_leaves.size()));
            for (const auto member : tree.wait_exempt_leaves)
                writer.integer(member);
        }
    }
}

EpochDefinitionInput read_definition(
    Reader &reader,
    EpochProtocolMode mode,
    std::uint64_t activation_height,
    const EpochWireLimits &limits)
{
    EpochDefinitionInput definition;
    definition.schema_version = reader.integer<std::uint32_t>();
    const auto expected_schema = definition_schema_for_mode(mode);
    if (!expected_schema.has_value() ||
        definition.schema_version != *expected_schema)
    {
        fail(EpochWireError::unsupported_schema);
    }
    definition.epoch_number = reader.integer<std::uint32_t>();
    definition.previous_epoch_digest = reader.digest();
    definition.membership_digest = reader.digest();
    definition.activation_height =
        mode == EpochProtocolMode::adaptive_v1
            ? reader.integer<std::uint64_t>()
            : activation_height;
    definition.generation_seed = reader.integer<std::uint64_t>();
    definition.policy_version = reader.string(limits.maximum_string_bytes);
    definition.evidence_snapshot_id =
        reader.string(limits.maximum_string_bytes);
    definition.evidence_cutoff = reader.integer<std::uint64_t>();

    const auto tree_count = reader.integer<std::uint32_t>();
    if (tree_count > limits.maximum_trees)
    {
        fail(EpochWireError::tree_count_exceeded);
    }
    constexpr std::size_t minimum_tree_bytes =
        sizeof(std::uint32_t) * 4;
    if (tree_count > reader.remaining() / minimum_tree_bytes)
    {
        fail(EpochWireError::truncated);
    }
    definition.trees.reserve(tree_count);

    std::optional<std::uint32_t> previous_tree_id;
    for (std::uint32_t tree_index = 0;
         tree_index < tree_count;
         ++tree_index)
    {
        EpochTreeDefinition tree;
        tree.tree_id = reader.integer<std::uint32_t>();
        tree.fanout = reader.integer<std::uint32_t>();
        tree.pipeline_stretch = reader.integer<std::uint32_t>();
        if (previous_tree_id && tree.tree_id <= *previous_tree_id)
        {
            fail(EpochWireError::invalid_definition_digest);
        }
        previous_tree_id = tree.tree_id;

        const auto member_count = reader.integer<std::uint32_t>();
        if (member_count > limits.maximum_members_per_tree)
        {
            fail(EpochWireError::member_count_exceeded);
        }
        if (member_count > reader.remaining() / sizeof(ReplicaID))
        {
            fail(EpochWireError::truncated);
        }
        tree.members_breadth_first.reserve(member_count);
        for (std::uint32_t member_index = 0;
             member_index < member_count;
             ++member_index)
        {
            tree.members_breadth_first.push_back(
                reader.integer<ReplicaID>());
        }
        if (mode == EpochProtocolMode::adaptive_v2)
        {
            const auto wait_exempt_count = reader.integer<std::uint32_t>();
            if (wait_exempt_count >
                limits.maximum_wait_exempt_leaves_per_tree)
            {
                fail(EpochWireError::wait_exempt_count_exceeded);
            }
            if (wait_exempt_count > tree.members_breadth_first.size() ||
                wait_exempt_count >
                    reader.remaining() / sizeof(ReplicaID))
            {
                fail(EpochWireError::truncated);
            }
            tree.wait_exempt_leaves.reserve(wait_exempt_count);
            std::optional<ReplicaID> previous_wait_exempt;
            for (std::uint32_t wait_index = 0;
                 wait_index < wait_exempt_count;
                 ++wait_index)
            {
                const auto member = reader.integer<ReplicaID>();
                if (previous_wait_exempt &&
                    member <= *previous_wait_exempt)
                {
                    fail(EpochWireError::invalid_definition_digest);
                }
                previous_wait_exempt = member;
                tree.wait_exempt_leaves.push_back(member);
            }
        }
        definition.trees.push_back(std::move(tree));
    }
    return definition;
}

template <typename Value, typename Decode>
EpochWireDecodeResult<Value> decode(
    const bytearray_t &payload,
    const EpochWireLimits &limits,
    Decode &&decode_value) noexcept
{
    if (!valid_limits(limits))
    {
        return {EpochWireError::invalid_limits, std::nullopt};
    }
    if (payload.size() > limits.maximum_payload_bytes)
    {
        return {EpochWireError::payload_too_large, std::nullopt};
    }

    try
    {
        Reader reader(payload);
        auto value = decode_value(reader);
        if (!reader.empty())
        {
            return {EpochWireError::trailing_bytes, std::nullopt};
        }
        return {EpochWireError::none, std::move(value)};
    }
    catch (const WireFailure &failure)
    {
        return {failure.error, std::nullopt};
    }
    catch (const std::bad_alloc &)
    {
        return {EpochWireError::payload_too_large, std::nullopt};
    }
    catch (...)
    {
        return {EpochWireError::invalid_definition_digest, std::nullopt};
    }
}

} // namespace

bool EpochActivationIdentity::operator==(
    const EpochActivationIdentity &other) const noexcept
{
    return predecessor_epoch_number == other.predecessor_epoch_number &&
           predecessor_epoch_digest == other.predecessor_epoch_digest &&
           successor_epoch_number == other.successor_epoch_number &&
           successor_epoch_digest == other.successor_epoch_digest &&
           activation_height == other.activation_height;
}

bool EpochActivationIdentity::operator!=(
    const EpochActivationIdentity &other) const noexcept
{
    return !(*this == other);
}

bytearray_t encode_epoch_wire(
    const StageEpochDefinition &value,
    const EpochWireLimits &limits)
{
    require_encode_limits(limits);
    require_encode_header_contract(
        value.wire_schema_version,
        value.protocol_mode,
        EpochWireKind::stage_epoch_definition);
    auto definition = normalized_definition(value, limits);
    Writer writer(limits.maximum_payload_bytes);
    append_header(
        writer,
        value.wire_schema_version,
        value.protocol_mode,
        EpochWireKind::stage_epoch_definition);
    append_identity(writer, value.activation);
    append_definition(writer, definition, value.protocol_mode, limits);
    return std::move(writer).finish();
}

bytearray_t encode_epoch_wire(
    const StageAck &value,
    const EpochWireLimits &limits)
{
    require_encode_limits(limits);
    require_encode_header_contract(
        value.wire_schema_version,
        value.protocol_mode,
        EpochWireKind::stage_ack);
    Writer writer(limits.maximum_payload_bytes);
    append_header(
        writer,
        value.wire_schema_version,
        value.protocol_mode,
        EpochWireKind::stage_ack);
    writer.integer(value.replica_id);
    append_identity(writer, value.activation);
    return std::move(writer).finish();
}

bytearray_t encode_epoch_wire(
    const ArmActivation &value,
    const EpochWireLimits &limits)
{
    require_encode_limits(limits);
    require_encode_header_contract(
        value.wire_schema_version,
        value.protocol_mode,
        EpochWireKind::arm_activation);
    Writer writer(limits.maximum_payload_bytes);
    append_header(
        writer,
        value.wire_schema_version,
        value.protocol_mode,
        EpochWireKind::arm_activation);
    append_identity(writer, value.activation);
    return std::move(writer).finish();
}

bytearray_t encode_epoch_wire(
    const ActivationStatus &value,
    const EpochWireLimits &limits)
{
    require_encode_limits(limits);
    require_encode_header_contract(
        value.wire_schema_version,
        value.protocol_mode,
        EpochWireKind::activation_status);
    if (value.recovery_need > ActivationRecoveryNeed::exact_arm)
    {
        throw std::invalid_argument("invalid activation recovery need");
    }
    Writer writer(limits.maximum_payload_bytes);
    append_header(
        writer,
        value.wire_schema_version,
        value.protocol_mode,
        EpochWireKind::activation_status);
    writer.integer(value.replica_id);
    append_identity(writer, value.activation);
    writer.integer(static_cast<std::uint8_t>(value.recovery_need));
    return std::move(writer).finish();
}

EpochWireDecodeResult<StageEpochDefinition> decode_stage_epoch_definition(
    const bytearray_t &payload,
    EpochProtocolMode expected_mode,
    const EpochWireLimits &limits) noexcept
{
    return decode<StageEpochDefinition>(
        payload, limits,
        [&](Reader &reader) {
            read_header(
                reader,
                expected_mode,
                EpochWireKind::stage_epoch_definition);
            auto activation = read_identity(reader);
            auto definition = read_definition(
                reader,
                expected_mode,
                activation.activation_height,
                limits);
            definition.epoch_digest = activation.successor_epoch_digest;
            if (!identity_matches_definition(
                    activation, definition, expected_mode))
            {
                fail(EpochWireError::invalid_definition_digest);
            }
            return StageEpochDefinition{
                *wire_schema_for_mode(expected_mode),
                expected_mode,
                std::move(activation),
                std::move(definition)};
        });
}

EpochWireDecodeResult<StageAck> decode_stage_ack(
    const bytearray_t &payload,
    EpochProtocolMode expected_mode,
    const EpochWireLimits &limits) noexcept
{
    return decode<StageAck>(
        payload, limits,
        [&](Reader &reader) {
            read_header(reader, expected_mode, EpochWireKind::stage_ack);
            const auto replica = reader.integer<ReplicaID>();
            return StageAck{
                *wire_schema_for_mode(expected_mode),
                expected_mode,
                replica,
                read_identity(reader)};
        });
}

EpochWireDecodeResult<ArmActivation> decode_arm_activation(
    const bytearray_t &payload,
    EpochProtocolMode expected_mode,
    const EpochWireLimits &limits) noexcept
{
    return decode<ArmActivation>(
        payload, limits,
        [&](Reader &reader) {
            read_header(reader, expected_mode, EpochWireKind::arm_activation);
            return ArmActivation{
                *wire_schema_for_mode(expected_mode),
                expected_mode,
                read_identity(reader)};
        });
}

EpochWireDecodeResult<ActivationStatus> decode_activation_status(
    const bytearray_t &payload,
    EpochProtocolMode expected_mode,
    const EpochWireLimits &limits) noexcept
{
    return decode<ActivationStatus>(
        payload, limits,
        [&](Reader &reader) {
            read_header(
                reader,
                expected_mode,
                EpochWireKind::activation_status);
            const auto replica = reader.integer<ReplicaID>();
            auto activation = read_identity(reader);
            const auto recovery_need = static_cast<ActivationRecoveryNeed>(
                reader.integer<std::uint8_t>());
            if (recovery_need > ActivationRecoveryNeed::exact_arm)
            {
                fail(EpochWireError::invalid_definition_digest);
            }
            return ActivationStatus{
                *wire_schema_for_mode(expected_mode),
                expected_mode,
                replica,
                std::move(activation),
                recovery_need};
        });
}

std::optional<EpochWireKind> adaptive_epoch_wire_kind(
    opcode_t opcode) noexcept
{
    switch (opcode)
    {
    case MsgStageEpochDefinition::opcode:
        return EpochWireKind::stage_epoch_definition;
    case MsgStageAck::opcode:
        return EpochWireKind::stage_ack;
    case MsgArmActivation::opcode:
        return EpochWireKind::arm_activation;
    case MsgActivationStatus::opcode:
        return EpochWireKind::activation_status;
    default:
        // In particular, legacy 0x10/0x11 and reserved 0x12 stay outside the
        // adaptive namespace.
        return std::nullopt;
    }
}

const opcode_t MsgStageEpochDefinition::opcode;
const opcode_t MsgStageAck::opcode;
const opcode_t MsgArmActivation::opcode;
const opcode_t MsgActivationStatus::opcode;

MsgStageEpochDefinition::MsgStageEpochDefinition(
    const StageEpochDefinition &value,
    const EpochWireLimits &limits)
    : serialized(encode_epoch_wire(value, limits))
{
}

MsgStageEpochDefinition::MsgStageEpochDefinition(
    DataStream &&serialized_payload)
    : serialized(std::move(serialized_payload))
{
}

MsgStageAck::MsgStageAck(
    const StageAck &value,
    const EpochWireLimits &limits)
    : serialized(encode_epoch_wire(value, limits))
{
}

MsgStageAck::MsgStageAck(DataStream &&serialized_payload)
    : serialized(std::move(serialized_payload))
{
}

MsgArmActivation::MsgArmActivation(
    const ArmActivation &value,
    const EpochWireLimits &limits)
    : serialized(encode_epoch_wire(value, limits))
{
}

MsgArmActivation::MsgArmActivation(DataStream &&serialized_payload)
    : serialized(std::move(serialized_payload))
{
}

MsgActivationStatus::MsgActivationStatus(
    const ActivationStatus &value,
    const EpochWireLimits &limits)
    : serialized(encode_epoch_wire(value, limits))
{
}

MsgActivationStatus::MsgActivationStatus(DataStream &&serialized_payload)
    : serialized(std::move(serialized_payload))
{
}

} // namespace hotstuff
