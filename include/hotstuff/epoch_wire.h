/**
 * Canonical, bounded wire values for adaptive epoch staging.
 *
 * These values are externally serialized. Legacy deployment remains the
 * explicitly selected legacy_static mode; adaptive_v1 uses its own opcodes.
 * The legacy 0x10 and 0x11 opcodes, and the reserved 0x12 opcode, are never
 * interpreted as adaptive epoch messages.
 */

#ifndef HOTSTUFF_EPOCH_WIRE_H_INCLUDED
#define HOTSTUFF_EPOCH_WIRE_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <optional>

#include "hotstuff/configuration.h"

namespace hotstuff
{

constexpr std::uint32_t kEpochWireSchemaVersion = 1;

enum class EpochProtocolMode : std::uint8_t
{
    legacy_static = 0,
    adaptive_v1 = 1,
};

enum class EpochWireKind : std::uint8_t
{
    stage_epoch_definition = 1,
    stage_ack = 2,
    arm_activation = 3,
    activation_status = 4,
};

enum class EpochWireError : std::uint8_t
{
    none = 0,
    invalid_limits,
    payload_too_large,
    unsupported_schema,
    mode_mismatch,
    unexpected_kind,
    truncated,
    trailing_bytes,
    tree_count_exceeded,
    member_count_exceeded,
    string_length_exceeded,
    invalid_definition_digest,
};

struct EpochWireLimits
{
    std::size_t maximum_payload_bytes{1024 * 1024};
    std::uint32_t maximum_trees{64};
    std::uint32_t maximum_members_per_tree{4096};
    std::uint32_t maximum_string_bytes{4096};
};

struct EpochActivationIdentity
{
    std::uint32_t predecessor_epoch_number{0};
    uint256_t predecessor_epoch_digest;
    std::uint32_t successor_epoch_number{0};
    uint256_t successor_epoch_digest;
    std::uint64_t activation_height{0};

    bool operator==(const EpochActivationIdentity &other) const noexcept;
    bool operator!=(const EpochActivationIdentity &other) const noexcept;
};

struct StageEpochDefinition
{
    std::uint32_t wire_schema_version{kEpochWireSchemaVersion};
    EpochProtocolMode protocol_mode{EpochProtocolMode::adaptive_v1};
    EpochActivationIdentity activation;
    EpochDefinitionInput definition;
};

struct StageAck
{
    std::uint32_t wire_schema_version{kEpochWireSchemaVersion};
    EpochProtocolMode protocol_mode{EpochProtocolMode::adaptive_v1};
    ReplicaID replica_id{0};
    EpochActivationIdentity activation;
};

struct ArmActivation
{
    std::uint32_t wire_schema_version{kEpochWireSchemaVersion};
    EpochProtocolMode protocol_mode{EpochProtocolMode::adaptive_v1};
    EpochActivationIdentity activation;
};

enum class ActivationRecoveryNeed : std::uint8_t
{
    none = 0,
    exact_definition,
    exact_arm,
};

struct ActivationStatus
{
    std::uint32_t wire_schema_version{kEpochWireSchemaVersion};
    EpochProtocolMode protocol_mode{EpochProtocolMode::adaptive_v1};
    ReplicaID replica_id{0};
    EpochActivationIdentity activation;
    ActivationRecoveryNeed recovery_need{ActivationRecoveryNeed::none};
};

template <typename Value>
struct EpochWireDecodeResult
{
    EpochWireError error{EpochWireError::none};
    std::optional<Value> value;

    explicit operator bool() const noexcept
    {
        return error == EpochWireError::none && value.has_value();
    }
};

bytearray_t encode_epoch_wire(
    const StageEpochDefinition &value,
    const EpochWireLimits &limits);
bytearray_t encode_epoch_wire(
    const StageAck &value,
    const EpochWireLimits &limits);
bytearray_t encode_epoch_wire(
    const ArmActivation &value,
    const EpochWireLimits &limits);
bytearray_t encode_epoch_wire(
    const ActivationStatus &value,
    const EpochWireLimits &limits);

EpochWireDecodeResult<StageEpochDefinition> decode_stage_epoch_definition(
    const bytearray_t &payload,
    EpochProtocolMode expected_mode,
    const EpochWireLimits &limits) noexcept;
EpochWireDecodeResult<StageAck> decode_stage_ack(
    const bytearray_t &payload,
    EpochProtocolMode expected_mode,
    const EpochWireLimits &limits) noexcept;
EpochWireDecodeResult<ArmActivation> decode_arm_activation(
    const bytearray_t &payload,
    EpochProtocolMode expected_mode,
    const EpochWireLimits &limits) noexcept;
EpochWireDecodeResult<ActivationStatus> decode_activation_status(
    const bytearray_t &payload,
    EpochProtocolMode expected_mode,
    const EpochWireLimits &limits) noexcept;

std::optional<EpochWireKind> adaptive_epoch_wire_kind(
    opcode_t opcode) noexcept;

struct MsgStageEpochDefinition
{
    static const opcode_t opcode = 0x13;
    DataStream serialized;

    MsgStageEpochDefinition(
        const StageEpochDefinition &value,
        const EpochWireLimits &limits);
    explicit MsgStageEpochDefinition(DataStream &&serialized_payload);
};

struct MsgStageAck
{
    static const opcode_t opcode = 0x14;
    DataStream serialized;

    MsgStageAck(const StageAck &value, const EpochWireLimits &limits);
    explicit MsgStageAck(DataStream &&serialized_payload);
};

struct MsgArmActivation
{
    static const opcode_t opcode = 0x15;
    DataStream serialized;

    MsgArmActivation(
        const ArmActivation &value,
        const EpochWireLimits &limits);
    explicit MsgArmActivation(DataStream &&serialized_payload);
};

struct MsgActivationStatus
{
    static const opcode_t opcode = 0x16;
    DataStream serialized;

    MsgActivationStatus(
        const ActivationStatus &value,
        const EpochWireLimits &limits);
    explicit MsgActivationStatus(DataStream &&serialized_payload);
};

} // namespace hotstuff

#endif
