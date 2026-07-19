/**
 * Canonical adaptive-v2 manager-to-replica epoch-change bundles.
 */

#ifndef HOTSTUFF_EPOCH_CHANGE_BUNDLE_H_INCLUDED
#define HOTSTUFF_EPOCH_CHANGE_BUNDLE_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>

#include "hotstuff/epoch_change.h"

namespace hotstuff
{

constexpr std::uint32_t kEpochChangeBundleSchemaVersionV1 = 1;

struct EpochChangeBundleLimits
{
    std::size_t maximum_payload_bytes{2 * 1024 * 1024};
    std::size_t maximum_command_bytes{4096};
    EpochWireLimits definition_limits;
};

enum class EpochChangeBundleWireError : std::uint8_t
{
    none = 0,
    invalid_limits,
    payload_too_large,
    component_too_large,
    invalid_domain,
    unsupported_schema,
    mode_mismatch,
    truncated,
    trailing_bytes,
    invalid_command,
    invalid_definition,
    identity_mismatch,
    noncanonical_encoding,
    allocation_failure,
    internal_failure,
};

const std::string &epoch_change_bundle_domain() noexcept;

class AdaptiveV2EpochChangeBundle final
{
public:
    AdaptiveV2EpochChangeBundle(
        AuthorizedEpochChange command,
        EpochDefinitionInput definition,
        const EpochChangeBundleLimits &limits);

    std::uint32_t schema_version() const noexcept
    {
        return schema_version_;
    }

    EpochProtocolMode protocol_mode() const noexcept
    {
        return protocol_mode_;
    }

    const AuthorizedEpochChange &command() const noexcept
    {
        return command_;
    }

    const EpochDefinitionInput &definition() const noexcept
    {
        return definition_;
    }

    const bytearray_t &canonical_bytes() const noexcept
    {
        return canonical_bytes_;
    }

private:
    std::uint32_t schema_version_{kEpochChangeBundleSchemaVersionV1};
    EpochProtocolMode protocol_mode_{EpochProtocolMode::adaptive_v2};
    AuthorizedEpochChange command_;
    EpochDefinitionInput definition_;
    bytearray_t canonical_bytes_;
};

struct EpochChangeBundleDecodeResult
{
    EpochChangeBundleWireError error{EpochChangeBundleWireError::none};
    EpochChangeWireError command_error{EpochChangeWireError::none};
    EpochWireError definition_error{EpochWireError::none};
    std::optional<AdaptiveV2EpochChangeBundle> value;

    explicit operator bool() const noexcept
    {
        return error == EpochChangeBundleWireError::none && value.has_value();
    }
};

EpochChangeBundleDecodeResult decode_adaptive_v2_epoch_change_bundle(
    const bytearray_t &payload,
    const EpochChangeBundleLimits &limits) noexcept;

struct MsgAdaptiveV2EpochChangeBundle
{
    static const opcode_t opcode = 0x19;
    DataStream serialized;

    explicit MsgAdaptiveV2EpochChangeBundle(
        const AdaptiveV2EpochChangeBundle &value);
    explicit MsgAdaptiveV2EpochChangeBundle(
        DataStream &&serialized_payload);
};

static_assert(
    MsgAdaptiveV2EpochChangeBundle::opcode !=
        MsgStageEpochDefinition::opcode &&
    MsgAdaptiveV2EpochChangeBundle::opcode != MsgStageAck::opcode &&
    MsgAdaptiveV2EpochChangeBundle::opcode != MsgArmActivation::opcode &&
    MsgAdaptiveV2EpochChangeBundle::opcode != MsgActivationStatus::opcode &&
    MsgAdaptiveV2EpochChangeBundle::opcode !=
        MsgEpochDefinitionRequest::opcode &&
    MsgAdaptiveV2EpochChangeBundle::opcode !=
        MsgEpochDefinitionReply::opcode,
    "adaptive epoch message opcodes must remain distinct");

} // namespace hotstuff

#endif
