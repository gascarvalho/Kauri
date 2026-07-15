#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <fstream>
#include <iterator>
#include <limits>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include "catch.hpp"
#include "hotstuff/client.h"
#include "hotstuff/epoch_store.h"
#include "hotstuff/evidence.h"

/*
 * D11 phase-1 contract
 * -------------------
 * This target specifies pure, externally serialized epoch staging and
 * activation state. It deliberately does not claim a HotStuff message
 * handler, network authentication, timer integration, or proposal draining.
 *
 * The adaptive wire format is a new versioned namespace. Legacy static
 * deployment remains explicitly selected and opcodes 0x10, 0x11, and 0x12
 * are never parsed as adaptive epoch messages.
 */
#if __has_include("hotstuff/epoch_activation.h") && \
    __has_include("hotstuff/epoch_wire.h")
#include "hotstuff/epoch_activation.h"
#include "hotstuff/epoch_wire.h"
#define KAURI_HAS_D11_EPOCH_ACTIVATION_API 1
#else
#define KAURI_HAS_D11_EPOCH_ACTIVATION_API 0

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

enum class StageAckDisposition : std::uint8_t
{
    accepted = 1,
    duplicate,
    unsupported_schema,
    wrong_mode,
    reporter_mismatch,
    foreign_replica,
    wrong_activation,
};

struct StageAckRecordResult
{
    const StageAckDisposition disposition;
    const std::size_t accepted_acknowledgements;
    const std::size_t required_acknowledgements;
    const bool ready;
};

class EpochAckTracker final
{
public:
    EpochAckTracker(
        EpochActivationIdentity staged_activation,
        std::vector<ReplicaID> fixed_membership,
        std::uint32_t tolerated_faults);
    ~EpochAckTracker();

    EpochAckTracker(const EpochAckTracker &) = delete;
    EpochAckTracker &operator=(const EpochAckTracker &) = delete;
    EpochAckTracker(EpochAckTracker &&) = delete;
    EpochAckTracker &operator=(EpochAckTracker &&) = delete;

    StageAckRecordResult record(
        const AuthenticatedReporter &authenticated_reporter,
        const StageAck &acknowledgement);
    ArmActivation build_arm() const;

    std::size_t acknowledgement_count() const noexcept;
    std::size_t required_acknowledgements() const noexcept;
    bool ready() const noexcept;

private:
    struct State;
    std::unique_ptr<State> state_;
};

enum class ReplicaStageDisposition : std::uint8_t
{
    staged = 1,
    duplicate,
    unsupported_schema,
    wrong_mode,
    wrong_successor,
    wrong_predecessor,
    divergent_definition,
};

struct ReplicaStageResult
{
    const ReplicaStageDisposition disposition;
    const std::optional<StageAck> acknowledgement;
};

enum class ReplicaArmDisposition : std::uint8_t
{
    armed = 1,
    duplicate,
    unsupported_schema,
    wrong_mode,
    missing_definition,
    wrong_activation,
};

enum class ActivationBlockReason : std::uint8_t
{
    none = 0,
    missing_definition,
    missing_arm,
    predecessor_digest_mismatch,
    missed_activation_height,
};

enum class ActivationTransition : std::uint8_t
{
    waiting = 1,
    blocked,
    activated,
    already_active,
};

struct EpochActivationEffect
{
    const EpochDefinition *const definition;
    const ConfigurationId configuration;
    const std::uint32_t rotation_ordinal;
    const std::uint64_t generation;
};

struct EpochActivationResult
{
    const ActivationTransition transition;
    const ActivationBlockReason blocked_reason;
    const std::optional<EpochActivationEffect> effect;
};

std::optional<std::uint32_t> checked_successor_epoch(
    std::uint32_t current_epoch) noexcept;
std::optional<std::uint64_t> checked_activation_generation(
    std::uint32_t epoch_number,
    std::uint64_t rotation_ordinal) noexcept;

class ReplicaEpochActivation final
{
public:
    ReplicaEpochActivation(
        EpochStore &store,
        const EpochDefinition &active_epoch,
        ReplicaID local_replica,
        std::uint32_t active_tree_id = 0,
        std::uint32_t rotation_ordinal = 0);
    ~ReplicaEpochActivation();

    ReplicaEpochActivation(const ReplicaEpochActivation &) = delete;
    ReplicaEpochActivation &operator=(const ReplicaEpochActivation &) = delete;
    ReplicaEpochActivation(ReplicaEpochActivation &&) = delete;
    ReplicaEpochActivation &operator=(ReplicaEpochActivation &&) = delete;

    ReplicaStageResult stage(
        const StageEpochDefinition &message,
        const EpochValidationContext &validation_context);
    ReplicaArmDisposition arm(const ArmActivation &message);
    bool restore_expectation(const ActivationStatus &status);

    EpochActivationResult on_predecessor_commit(
        std::uint64_t height,
        const uint256_t &digest);
    EpochActivationResult replay_blocked_commit();
    EpochActivationEffect rotate_to_tree(std::uint32_t tree_id);

    EpochActivationEffect active_effect() const;
    std::optional<ActivationStatus> active_status() const;
    std::optional<ActivationStatus> recovery_status() const;
    ActivationBlockReason blocked_reason() const noexcept;
    bool admits_new_proposals() const noexcept;
    bool may_drain_exact_context(
        const ConfigurationId &configuration) const noexcept;

private:
    struct State;
    std::unique_ptr<State> state_;
};

} // namespace hotstuff
#endif

namespace
{

using hotstuff::ActivationBlockReason;
using hotstuff::ActivationRecoveryNeed;
using hotstuff::ActivationStatus;
using hotstuff::ActivationTransition;
using hotstuff::ArmActivation;
using hotstuff::AuthenticatedReporter;
using hotstuff::ConfigurationId;
using hotstuff::EpochAckTracker;
using hotstuff::EpochActivationEffect;
using hotstuff::EpochActivationIdentity;
using hotstuff::EpochActivationResult;
using hotstuff::EpochDefinition;
using hotstuff::EpochDefinitionInput;
using hotstuff::EpochProtocolMode;
using hotstuff::EpochStore;
using hotstuff::EpochTreeDefinition;
using hotstuff::EpochValidationContext;
using hotstuff::EpochWireError;
using hotstuff::EpochWireKind;
using hotstuff::EpochWireLimits;
using hotstuff::MsgActivationStatus;
using hotstuff::MsgArmActivation;
using hotstuff::MsgStageAck;
using hotstuff::MsgStageEpochDefinition;
using hotstuff::ReplicaArmDisposition;
using hotstuff::ReplicaEpochActivation;
using hotstuff::ReplicaID;
using hotstuff::ReplicaStageDisposition;
using hotstuff::StageAck;
using hotstuff::StageAckDisposition;
using hotstuff::StageEpochDefinition;
using hotstuff::bytearray_t;
using hotstuff::opcode_t;
using hotstuff::uint256_t;

constexpr std::uint64_t kActivationHeight = 20;

const opcode_t *const stage_opcode = &MsgStageEpochDefinition::opcode;
const opcode_t *const ack_opcode = &MsgStageAck::opcode;
const opcode_t *const arm_opcode = &MsgArmActivation::opcode;
const opcode_t *const status_opcode = &MsgActivationStatus::opcode;
const opcode_t *const legacy_epoch_opcode = &hotstuff::MsgDeployEpoch::opcode;
const opcode_t *const legacy_reputation_opcode =
    &hotstuff::MsgDeployEpochReputation::opcode;

uint256_t fixture_digest(const std::string &label)
{
    return hotstuff::DataStream(label).get_hash();
}

std::vector<ReplicaID> membership7()
{
    return {0, 1, 2, 3, 4, 5, 6};
}

EpochTreeDefinition tree(
    std::uint32_t tree_id,
    std::vector<ReplicaID> members)
{
    return {tree_id, 2, 2, std::move(members)};
}

EpochDefinitionInput epoch_zero_input()
{
    EpochDefinitionInput input;
    input.schema_version = hotstuff::kEpochDefinitionSchemaVersion;
    input.epoch_number = 0;
    input.previous_epoch_digest = {};
    input.membership_digest =
        hotstuff::canonical_membership_digest(membership7());
    input.trees = {
        tree(0, {0, 1, 2, 3, 4, 5, 6}),
        tree(1, {1, 0, 2, 3, 4, 5, 6}),
    };
    input.activation_height = 0;
    input.generation_seed = 0xD110;
    input.policy_version = "d11-baseline-v1";
    input.evidence_snapshot_id = "d11-baseline";
    input.evidence_cutoff = 0;
    input.epoch_digest.reset();
    return input;
}

EpochValidationContext epoch_zero_context()
{
    return {0, 0, {}};
}

EpochDefinitionInput successor_input(const EpochDefinition &predecessor)
{
    auto input = epoch_zero_input();
    input.epoch_number = predecessor.epoch_number() + 1;
    input.previous_epoch_digest = predecessor.epoch_digest();
    input.trees = {
        tree(1, {2, 0, 1, 3, 4, 5, 6}),
        tree(0, {3, 0, 1, 2, 4, 5, 6}),
    };
    input.activation_height = kActivationHeight;
    input.generation_seed = 0xD111;
    input.policy_version = "d11-adaptive-v1";
    input.evidence_snapshot_id = "d11-window-1";
    input.evidence_cutoff = 50;
    input.epoch_digest.reset();
    return input;
}

EpochValidationContext successor_context()
{
    return {10, 5, {}};
}

StageEpochDefinition stage_message(
    const EpochDefinition &predecessor,
    EpochDefinitionInput input)
{
    const auto digest = hotstuff::compute_epoch_digest(input);
    input.epoch_digest = digest;
    return {
        hotstuff::kEpochWireSchemaVersion,
        EpochProtocolMode::adaptive_v1,
        EpochActivationIdentity{
            predecessor.epoch_number(),
            predecessor.epoch_digest(),
            input.epoch_number,
            digest,
            input.activation_height},
        std::move(input)};
}

StageAck acknowledgement(
    ReplicaID replica,
    const EpochActivationIdentity &activation)
{
    return {
        hotstuff::kEpochWireSchemaVersion,
        EpochProtocolMode::adaptive_v1,
        replica,
        activation};
}

ArmActivation arm_for(const EpochActivationIdentity &activation)
{
    return {
        hotstuff::kEpochWireSchemaVersion,
        EpochProtocolMode::adaptive_v1,
        activation};
}

ActivationStatus status_for(
    ReplicaID replica,
    const EpochActivationIdentity &activation,
    ActivationRecoveryNeed need)
{
    return {
        hotstuff::kEpochWireSchemaVersion,
        EpochProtocolMode::adaptive_v1,
        replica,
        activation,
        need};
}

EpochWireLimits generous_wire_limits()
{
    return {8192, 8, 16, 128};
}

struct EpochFixture
{
    EpochStore store{membership7()};
    const EpochDefinition *epoch0{nullptr};

    EpochFixture()
    {
        epoch0 = &store.stage(epoch_zero_input(), epoch_zero_context());
    }

    StageEpochDefinition successor() const
    {
        return stage_message(*epoch0, successor_input(*epoch0));
    }
};

void overwrite_u32(
    bytearray_t &payload,
    std::size_t offset,
    std::uint32_t value)
{
    REQUIRE(offset + sizeof(value) <= payload.size());
    for (std::size_t index = 0; index < sizeof(value); ++index)
    {
        payload[offset + index] = static_cast<std::uint8_t>(
            value >> ((sizeof(value) - index - 1) * 8));
    }
}

void check_identity(
    const EpochActivationIdentity &actual,
    const EpochActivationIdentity &expected)
{
    CHECK(actual.predecessor_epoch_number ==
          expected.predecessor_epoch_number);
    CHECK(actual.predecessor_epoch_digest ==
          expected.predecessor_epoch_digest);
    CHECK(actual.successor_epoch_number == expected.successor_epoch_number);
    CHECK(actual.successor_epoch_digest == expected.successor_epoch_digest);
    CHECK(actual.activation_height == expected.activation_height);
}

void check_effect(
    const EpochActivationEffect &effect,
    const EpochDefinition &definition,
    std::uint32_t tree_id,
    std::uint32_t ordinal)
{
    CHECK(effect.definition == &definition);
    CHECK(effect.configuration.epoch_number == definition.epoch_number());
    CHECK(effect.configuration.tree_id == tree_id);
    CHECK(effect.configuration.epoch_digest == definition.epoch_digest());
    CHECK(effect.rotation_ordinal == ordinal);
    const auto packed =
        (static_cast<std::uint64_t>(definition.epoch_number()) << 32) |
        ordinal;
    REQUIRE(packed != std::numeric_limits<std::uint64_t>::max());
    const auto expected_generation =
        hotstuff::checked_activation_generation(
            definition.epoch_number(), ordinal);
    REQUIRE(expected_generation.has_value());
    CHECK(*expected_generation == packed + 1);
    CHECK(effect.generation == *expected_generation);
}

std::string read_source(const std::string &relative_path)
{
#ifdef KAURI_PROJECT_SOURCE_DIR
    const std::string root = KAURI_PROJECT_SOURCE_DIR;
#else
    const std::string root = ".";
#endif
    std::ifstream input(root + "/" + relative_path);
    REQUIRE(input.good());
    return std::string(
        std::istreambuf_iterator<char>(input),
        std::istreambuf_iterator<char>());
}

} // namespace

TEST_CASE("D11 exposes distinct legacy and adaptive versioned opcodes",
          "[d11][epoch-activation][wire][opcode][contract]"
          "[intentional-red]")
{
    CHECK(KAURI_HAS_D11_EPOCH_ACTIVATION_API == 1);
    CHECK(static_cast<std::uint8_t>(EpochProtocolMode::legacy_static) == 0);
    CHECK(static_cast<std::uint8_t>(EpochProtocolMode::adaptive_v1) == 1);

    CHECK(*legacy_epoch_opcode == 0x10);
    CHECK(*legacy_reputation_opcode == 0x11);
    CHECK(*stage_opcode == 0x13);
    CHECK(*ack_opcode == 0x14);
    CHECK(*arm_opcode == 0x15);
    CHECK(*status_opcode == 0x16);

    const std::vector<opcode_t> adaptive{
        *stage_opcode, *ack_opcode, *arm_opcode, *status_opcode};
    CHECK(std::adjacent_find(adaptive.begin(), adaptive.end()) ==
          adaptive.end());
    for (const auto opcode : adaptive)
    {
        CHECK(opcode != 0x10);
        CHECK(opcode != 0x11);
        CHECK(opcode != 0x12);
    }

    CHECK_FALSE(hotstuff::adaptive_epoch_wire_kind(0x10).has_value());
    CHECK_FALSE(hotstuff::adaptive_epoch_wire_kind(0x11).has_value());
    CHECK_FALSE(hotstuff::adaptive_epoch_wire_kind(0x12).has_value());
    CHECK(hotstuff::adaptive_epoch_wire_kind(0x13) ==
          EpochWireKind::stage_epoch_definition);
    CHECK(hotstuff::adaptive_epoch_wire_kind(0x14) ==
          EpochWireKind::stage_ack);
    CHECK(hotstuff::adaptive_epoch_wire_kind(0x15) ==
          EpochWireKind::arm_activation);
    CHECK(hotstuff::adaptive_epoch_wire_kind(0x16) ==
          EpochWireKind::activation_status);

    static_assert(
        std::is_constructible<
            MsgStageEpochDefinition,
            const StageEpochDefinition &,
            const EpochWireLimits &>::value,
        "outbound staging messages use canonical bounded serialization");
    static_assert(
        std::is_constructible<
            MsgStageEpochDefinition,
            hotstuff::DataStream &&>::value,
        "inbound messages retain opaque bytes until bounded decode");
    static_assert(
        !std::is_copy_constructible<EpochAckTracker>::value,
        "one tracker owns one fixed-membership acknowledgement set");
    static_assert(
        !std::is_copy_constructible<ReplicaEpochActivation>::value,
        "one replica activation state owns one EpochStore boundary");
    static_assert(
        !std::is_copy_assignable<EpochActivationResult>::value,
        "transition results are immutable snapshots");
}

TEST_CASE("all adaptive epoch messages round trip canonical exact values",
          "[d11][epoch-activation][wire][roundtrip][canonical]"
          "[intentional-red]")
{
    EpochFixture fixture;
    auto stage = fixture.successor();
    const auto ack = acknowledgement(3, stage.activation);
    const auto arm = arm_for(stage.activation);
    const auto status = status_for(
        3, stage.activation, ActivationRecoveryNeed::exact_arm);
    const auto limits = generous_wire_limits();

    auto same_logical_stage = stage;
    std::reverse(
        same_logical_stage.definition.trees.begin(),
        same_logical_stage.definition.trees.end());
    CHECK(hotstuff::encode_epoch_wire(stage, limits) ==
          hotstuff::encode_epoch_wire(same_logical_stage, limits));

    const auto stage_wire = hotstuff::encode_epoch_wire(stage, limits);
    const auto decoded_stage = hotstuff::decode_stage_epoch_definition(
        stage_wire, EpochProtocolMode::adaptive_v1, limits);
    REQUIRE(decoded_stage);
    REQUIRE(decoded_stage.value.has_value());
    check_identity(decoded_stage.value->activation, stage.activation);
    CHECK(decoded_stage.value->definition.epoch_number ==
          stage.definition.epoch_number);
    CHECK(decoded_stage.value->definition.epoch_digest ==
          stage.definition.epoch_digest);
    CHECK(hotstuff::canonical_serialize_epoch(
              decoded_stage.value->definition) ==
          hotstuff::canonical_serialize_epoch(stage.definition));
    CHECK(hotstuff::encode_epoch_wire(*decoded_stage.value, limits) ==
          stage_wire);

    const auto ack_wire = hotstuff::encode_epoch_wire(ack, limits);
    const auto decoded_ack = hotstuff::decode_stage_ack(
        ack_wire, EpochProtocolMode::adaptive_v1, limits);
    REQUIRE(decoded_ack);
    CHECK(decoded_ack.value->replica_id == 3);
    check_identity(decoded_ack.value->activation, stage.activation);
    CHECK(hotstuff::encode_epoch_wire(*decoded_ack.value, limits) ==
          ack_wire);

    const auto arm_wire = hotstuff::encode_epoch_wire(arm, limits);
    const auto decoded_arm = hotstuff::decode_arm_activation(
        arm_wire, EpochProtocolMode::adaptive_v1, limits);
    REQUIRE(decoded_arm);
    check_identity(decoded_arm.value->activation, stage.activation);
    CHECK(hotstuff::encode_epoch_wire(*decoded_arm.value, limits) ==
          arm_wire);

    const auto status_wire = hotstuff::encode_epoch_wire(status, limits);
    const auto decoded_status = hotstuff::decode_activation_status(
        status_wire, EpochProtocolMode::adaptive_v1, limits);
    REQUIRE(decoded_status);
    CHECK(decoded_status.value->replica_id == 3);
    CHECK(decoded_status.value->recovery_need ==
          ActivationRecoveryNeed::exact_arm);
    check_identity(decoded_status.value->activation, stage.activation);
    CHECK(hotstuff::encode_epoch_wire(*decoded_status.value, limits) ==
          status_wire);

    MsgStageEpochDefinition outbound_stage(stage, limits);
    CHECK(static_cast<bytearray_t>(outbound_stage.serialized) == stage_wire);
    hotstuff::DataStream received_stage(stage_wire);
    MsgStageEpochDefinition inbound_stage(std::move(received_stage));
    CHECK(static_cast<bytearray_t>(inbound_stage.serialized) == stage_wire);

    MsgStageAck outbound_ack(ack, limits);
    MsgArmActivation outbound_arm(arm, limits);
    MsgActivationStatus outbound_status(status, limits);
    CHECK(static_cast<bytearray_t>(outbound_ack.serialized) == ack_wire);
    CHECK(static_cast<bytearray_t>(outbound_arm.serialized) == arm_wire);
    CHECK(static_cast<bytearray_t>(outbound_status.serialized) == status_wire);
}

TEST_CASE("wire decode rejects malformed mixed or unbounded input",
          "[d11][epoch-activation][wire][bounds][malformed]"
          "[intentional-red]")
{
    EpochFixture fixture;
    const auto stage = fixture.successor();
    const auto limits = generous_wire_limits();
    const auto valid = hotstuff::encode_epoch_wire(stage, limits);

    CHECK(hotstuff::decode_stage_epoch_definition(
              {}, EpochProtocolMode::adaptive_v1, limits)
              .error == EpochWireError::truncated);

    auto truncated = valid;
    truncated.pop_back();
    CHECK(hotstuff::decode_stage_epoch_definition(
              truncated, EpochProtocolMode::adaptive_v1, limits)
              .error == EpochWireError::truncated);

    auto trailing = valid;
    trailing.push_back(0);
    CHECK(hotstuff::decode_stage_epoch_definition(
              trailing, EpochProtocolMode::adaptive_v1, limits)
              .error == EpochWireError::trailing_bytes);

    auto unsupported_schema = valid;
    overwrite_u32(
        unsupported_schema, 0, hotstuff::kEpochWireSchemaVersion + 1);
    CHECK(hotstuff::decode_stage_epoch_definition(
              unsupported_schema,
              EpochProtocolMode::adaptive_v1,
              limits)
              .error == EpochWireError::unsupported_schema);

    auto legacy_mode = valid;
    REQUIRE(legacy_mode.size() > 5);
    legacy_mode[4] =
        static_cast<std::uint8_t>(EpochProtocolMode::legacy_static);
    CHECK(hotstuff::decode_stage_epoch_definition(
              legacy_mode, EpochProtocolMode::adaptive_v1, limits)
              .error == EpochWireError::mode_mismatch);
    CHECK(hotstuff::decode_stage_epoch_definition(
              valid, EpochProtocolMode::legacy_static, limits)
              .error == EpochWireError::mode_mismatch);

    const auto ack_wire = hotstuff::encode_epoch_wire(
        acknowledgement(0, stage.activation), limits);
    CHECK(hotstuff::decode_stage_epoch_definition(
              ack_wire, EpochProtocolMode::adaptive_v1, limits)
              .error == EpochWireError::unexpected_kind);

    auto payload_bounded = limits;
    payload_bounded.maximum_payload_bytes = valid.size() - 1;
    CHECK(hotstuff::decode_stage_epoch_definition(
              valid,
              EpochProtocolMode::adaptive_v1,
              payload_bounded)
              .error == EpochWireError::payload_too_large);

    auto invalid_limits = limits;
    invalid_limits.maximum_payload_bytes = 0;
    CHECK(hotstuff::decode_stage_epoch_definition(
              valid, EpochProtocolMode::adaptive_v1, invalid_limits)
              .error == EpochWireError::invalid_limits);
    CHECK_THROWS(hotstuff::encode_epoch_wire(stage, invalid_limits));
}

TEST_CASE("wire structural limits reject before count-sized allocation",
          "[d11][epoch-activation][wire][bounds][allocation]"
          "[intentional-red]")
{
    EpochFixture fixture;
    auto stage = fixture.successor();
    const auto generous = generous_wire_limits();
    const auto wire = hotstuff::encode_epoch_wire(stage, generous);

    auto tree_bound = generous;
    tree_bound.maximum_trees = 1;
    CHECK(hotstuff::decode_stage_epoch_definition(
              wire, EpochProtocolMode::adaptive_v1, tree_bound)
              .error == EpochWireError::tree_count_exceeded);

    auto member_bound = generous;
    member_bound.maximum_members_per_tree = 6;
    CHECK(hotstuff::decode_stage_epoch_definition(
              wire, EpochProtocolMode::adaptive_v1, member_bound)
              .error == EpochWireError::member_count_exceeded);

    auto string_bound = generous;
    string_bound.maximum_string_bytes = 4;
    CHECK(hotstuff::decode_stage_epoch_definition(
              wire, EpochProtocolMode::adaptive_v1, string_bound)
              .error == EpochWireError::string_length_exceeded);

    stage.activation.successor_epoch_digest = fixture_digest("wrong");
    CHECK_THROWS(hotstuff::encode_epoch_wire(stage, generous));
}

TEST_CASE("wire encoder rejects structurally invalid canonical definitions",
          "[d11][epoch-activation][wire][canonical][rem-d11]"
          "[intentional-red]")
{
    EpochFixture fixture;
    const auto limits = generous_wire_limits();

    SECTION("duplicate tree identities are rejected before emission")
    {
        auto duplicate_tree = fixture.successor();
        REQUIRE(duplicate_tree.definition.trees.size() >= 2);
        duplicate_tree.definition.trees[1].tree_id =
            duplicate_tree.definition.trees[0].tree_id;
        const auto digest =
            hotstuff::compute_epoch_digest(duplicate_tree.definition);
        duplicate_tree.definition.epoch_digest = digest;
        duplicate_tree.activation.successor_epoch_digest = digest;

        CHECK_THROWS(hotstuff::encode_epoch_wire(duplicate_tree, limits));
    }

    SECTION("duplicate members are rejected before emission")
    {
        auto duplicate_member = fixture.successor();
        REQUIRE_FALSE(duplicate_member.definition.trees.empty());
        auto &members =
            duplicate_member.definition.trees.front().members_breadth_first;
        REQUIRE(members.size() >= 2);
        members[1] = members[0];
        const auto digest =
            hotstuff::compute_epoch_digest(duplicate_member.definition);
        duplicate_member.definition.epoch_digest = digest;
        duplicate_member.activation.successor_epoch_digest = digest;

        CHECK_THROWS(hotstuff::encode_epoch_wire(duplicate_member, limits));
    }
}

TEST_CASE("ack tracker derives configured two-f-plus-one from fixed membership",
          "[d11][epoch-activation][ack][membership][threshold]"
          "[intentional-red]")
{
    EpochFixture fixture;
    const auto activation = fixture.successor().activation;

    CHECK_THROWS(EpochAckTracker(activation, {}, 0));
    CHECK_THROWS(EpochAckTracker(activation, {0, 1, 1, 2}, 1));
    CHECK_THROWS(EpochAckTracker(activation, membership7(), 3));
    CHECK_THROWS(EpochAckTracker(
        activation,
        membership7(),
        std::numeric_limits<std::uint32_t>::max()));

    EpochAckTracker tracker(activation, membership7(), 2);
    CHECK(tracker.required_acknowledgements() == 5);
    CHECK(tracker.acknowledgement_count() == 0);
    CHECK_FALSE(tracker.ready());
    CHECK_THROWS(tracker.build_arm());

    for (ReplicaID replica = 0; replica < 5; ++replica)
    {
        const auto result = tracker.record(
            AuthenticatedReporter{replica},
            acknowledgement(replica, activation));
        CHECK(result.disposition == StageAckDisposition::accepted);
        CHECK(result.accepted_acknowledgements == replica + 1);
        CHECK(result.required_acknowledgements == 5);
        CHECK(result.ready == (replica == 4));
    }

    CHECK(tracker.ready());
    CHECK(tracker.acknowledgement_count() == 5);
    const auto arm = tracker.build_arm();
    CHECK(arm.wire_schema_version == hotstuff::kEpochWireSchemaVersion);
    CHECK(arm.protocol_mode == EpochProtocolMode::adaptive_v1);
    check_identity(arm.activation, activation);
}

TEST_CASE("ack identity and authenticated reporter are validated exactly",
          "[d11][epoch-activation][ack][authentication][idempotent]"
          "[intentional-red]")
{
    EpochFixture fixture;
    const auto activation = fixture.successor().activation;
    EpochAckTracker tracker(activation, membership7(), 2);

    const auto exact = acknowledgement(0, activation);
    const auto accepted = tracker.record(AuthenticatedReporter{0}, exact);
    CHECK(accepted.disposition == StageAckDisposition::accepted);
    CHECK(accepted.accepted_acknowledgements == 1);

    const auto duplicate = tracker.record(AuthenticatedReporter{0}, exact);
    CHECK(duplicate.disposition == StageAckDisposition::duplicate);
    CHECK(duplicate.accepted_acknowledgements == 1);

    const auto reporter_mismatch = tracker.record(
        AuthenticatedReporter{1}, exact);
    CHECK(reporter_mismatch.disposition ==
          StageAckDisposition::reporter_mismatch);

    const auto foreign = tracker.record(
        AuthenticatedReporter{99}, acknowledgement(99, activation));
    CHECK(foreign.disposition == StageAckDisposition::foreign_replica);

    auto wrong_digest = acknowledgement(2, activation);
    wrong_digest.activation.successor_epoch_digest =
        fixture_digest("other-successor");
    CHECK(tracker.record(AuthenticatedReporter{2}, wrong_digest).disposition ==
          StageAckDisposition::wrong_activation);

    auto wrong_predecessor = acknowledgement(2, activation);
    wrong_predecessor.activation.predecessor_epoch_number += 1;
    CHECK(tracker.record(
              AuthenticatedReporter{2}, wrong_predecessor)
              .disposition == StageAckDisposition::wrong_activation);

    auto wrong_height = acknowledgement(2, activation);
    ++wrong_height.activation.activation_height;
    CHECK(tracker.record(AuthenticatedReporter{2}, wrong_height).disposition ==
          StageAckDisposition::wrong_activation);

    auto wrong_schema = acknowledgement(2, activation);
    ++wrong_schema.wire_schema_version;
    CHECK(tracker.record(AuthenticatedReporter{2}, wrong_schema).disposition ==
          StageAckDisposition::unsupported_schema);

    auto wrong_mode = acknowledgement(2, activation);
    wrong_mode.protocol_mode = EpochProtocolMode::legacy_static;
    CHECK(tracker.record(AuthenticatedReporter{2}, wrong_mode).disposition ==
          StageAckDisposition::wrong_mode);

    CHECK(tracker.acknowledgement_count() == 1);
    CHECK_FALSE(tracker.ready());
}

TEST_CASE("replica stages only the exact validated successor idempotently",
          "[d11][epoch-activation][replica][stage][epoch-store]"
          "[intentional-red]")
{
    EpochFixture fixture;
    ReplicaEpochActivation replica(fixture.store, *fixture.epoch0, 4);
    const auto exact = fixture.successor();

    auto wrong_schema = exact;
    ++wrong_schema.wire_schema_version;
    CHECK(replica.stage(wrong_schema, successor_context()).disposition ==
          ReplicaStageDisposition::unsupported_schema);

    auto wrong_mode = exact;
    wrong_mode.protocol_mode = EpochProtocolMode::legacy_static;
    CHECK(replica.stage(wrong_mode, successor_context()).disposition ==
          ReplicaStageDisposition::wrong_mode);

    auto sparse = exact;
    ++sparse.activation.successor_epoch_number;
    ++sparse.definition.epoch_number;
    CHECK(replica.stage(sparse, successor_context()).disposition ==
          ReplicaStageDisposition::wrong_successor);

    auto wrong_predecessor = exact;
    wrong_predecessor.activation.predecessor_epoch_digest =
        fixture_digest("wrong-predecessor");
    wrong_predecessor.definition.previous_epoch_digest =
        wrong_predecessor.activation.predecessor_epoch_digest;
    CHECK(replica.stage(
              wrong_predecessor, successor_context())
              .disposition == ReplicaStageDisposition::wrong_predecessor);
    CHECK(fixture.store.size() == 1);

    auto invalid_input = successor_input(*fixture.epoch0);
    invalid_input.membership_digest = fixture_digest("wrong-membership");
    const auto invalid = stage_message(*fixture.epoch0, invalid_input);
    CHECK_THROWS(replica.stage(invalid, successor_context()));
    CHECK(fixture.store.size() == 1);
    CHECK(replica.admits_new_proposals());

    const auto staged = replica.stage(exact, successor_context());
    CHECK(staged.disposition == ReplicaStageDisposition::staged);
    REQUIRE(staged.acknowledgement.has_value());
    CHECK(staged.acknowledgement->replica_id == 4);
    check_identity(staged.acknowledgement->activation, exact.activation);
    CHECK(fixture.store.size() == 2);

    const auto duplicate = replica.stage(exact, successor_context());
    CHECK(duplicate.disposition == ReplicaStageDisposition::duplicate);
    REQUIRE(duplicate.acknowledgement.has_value());
    check_identity(duplicate.acknowledgement->activation, exact.activation);
    CHECK(fixture.store.size() == 2);

    auto divergent_input = successor_input(*fixture.epoch0);
    divergent_input.generation_seed += 1;
    const auto divergent = stage_message(*fixture.epoch0, divergent_input);
    CHECK(replica.stage(divergent, successor_context()).disposition ==
          ReplicaStageDisposition::divergent_definition);
    REQUIRE(fixture.store.find_epoch(1) != nullptr);
    CHECK(fixture.store.find_epoch(1)->epoch_digest() ==
          exact.activation.successor_epoch_digest);
}

TEST_CASE("exact arm and predecessor commit activate immutable tree zero",
          "[d11][epoch-activation][replica][arm][commit]"
          "[intentional-red]")
{
    EpochFixture fixture;
    ReplicaEpochActivation replica(fixture.store, *fixture.epoch0, 0);
    const auto stage = fixture.successor();

    CHECK_FALSE(replica.active_status().has_value());
    CHECK(replica.arm(arm_for(stage.activation)) ==
          ReplicaArmDisposition::missing_definition);
    REQUIRE(replica.stage(stage, successor_context()).acknowledgement);
    CHECK_FALSE(replica.active_status().has_value());

    auto wrong_arm = arm_for(stage.activation);
    wrong_arm.activation.successor_epoch_digest = fixture_digest("wrong-arm");
    CHECK(replica.arm(wrong_arm) ==
          ReplicaArmDisposition::wrong_activation);
    CHECK(replica.arm(arm_for(stage.activation)) ==
          ReplicaArmDisposition::armed);
    CHECK_FALSE(replica.active_status().has_value());
    CHECK(replica.arm(arm_for(stage.activation)) ==
          ReplicaArmDisposition::duplicate);

    const auto early = replica.on_predecessor_commit(
        kActivationHeight - 1, fixture.epoch0->epoch_digest());
    CHECK(early.transition == ActivationTransition::waiting);
    CHECK_FALSE(early.effect.has_value());
    CHECK_FALSE(replica.active_status().has_value());
    CHECK(replica.admits_new_proposals());

    const auto activated = replica.on_predecessor_commit(
        kActivationHeight, fixture.epoch0->epoch_digest());
    CHECK(activated.transition == ActivationTransition::activated);
    CHECK(activated.blocked_reason == ActivationBlockReason::none);
    REQUIRE(activated.effect.has_value());
    REQUIRE(fixture.store.find_epoch(1) != nullptr);
    check_effect(*activated.effect, *fixture.store.find_epoch(1), 0, 0);
    const auto active_status = replica.active_status();
    REQUIRE(active_status.has_value());
    CHECK(active_status->wire_schema_version ==
          hotstuff::kEpochWireSchemaVersion);
    CHECK(active_status->protocol_mode == EpochProtocolMode::adaptive_v1);
    CHECK(active_status->replica_id == 0);
    check_identity(active_status->activation, stage.activation);
    CHECK(active_status->recovery_need == ActivationRecoveryNeed::none);
    CHECK_FALSE(replica.recovery_status().has_value());
    CHECK(replica.admits_new_proposals());

    const auto replay = replica.on_predecessor_commit(
        kActivationHeight, fixture.epoch0->epoch_digest());
    CHECK(replay.transition == ActivationTransition::already_active);
    REQUIRE(replay.effect.has_value());
    check_effect(*replay.effect, *fixture.store.find_epoch(1), 0, 0);
    REQUIRE(replica.active_status().has_value());
    check_identity(
        replica.active_status()->activation, stage.activation);
    CHECK(replica.active_status()->recovery_need ==
          ActivationRecoveryNeed::none);
}

TEST_CASE("missing arm recovers only from stored exact-height commit proof",
          "[d11][epoch-activation][recovery][missing-arm][drain]"
          "[intentional-red]")
{
    EpochFixture fixture;
    ReplicaEpochActivation replica(fixture.store, *fixture.epoch0, 0);
    const auto stage = fixture.successor();
    REQUIRE(replica.stage(stage, successor_context()).acknowledgement);

    const auto blocked = replica.on_predecessor_commit(
        kActivationHeight, fixture.epoch0->epoch_digest());
    CHECK(blocked.transition == ActivationTransition::blocked);
    CHECK(blocked.blocked_reason == ActivationBlockReason::missing_arm);
    CHECK_FALSE(replica.active_status().has_value());
    CHECK_FALSE(replica.admits_new_proposals());

    const ConfigurationId predecessor_context{
        fixture.epoch0->epoch_number(),
        0,
        fixture.epoch0->epoch_digest()};
    CHECK(replica.may_drain_exact_context(predecessor_context));
    auto divergent_context = predecessor_context;
    divergent_context.epoch_digest = fixture_digest("divergent-context");
    CHECK_FALSE(replica.may_drain_exact_context(divergent_context));

    const auto recovery = replica.recovery_status();
    REQUIRE(recovery.has_value());
    CHECK(recovery->recovery_need == ActivationRecoveryNeed::exact_arm);
    check_identity(recovery->activation, stage.activation);

    CHECK(replica.arm(arm_for(stage.activation)) ==
          ReplicaArmDisposition::armed);
    const auto resumed = replica.replay_blocked_commit();
    CHECK(resumed.transition == ActivationTransition::activated);
    REQUIRE(resumed.effect.has_value());
    check_effect(*resumed.effect, *fixture.store.find_epoch(1), 0, 0);
    REQUIRE(replica.active_status().has_value());
    CHECK(replica.active_status()->recovery_need ==
          ActivationRecoveryNeed::none);
    CHECK_FALSE(replica.recovery_status().has_value());
    CHECK(replica.admits_new_proposals());
}

TEST_CASE("missing definition recovery stages and arms before proof replay",
          "[d11][epoch-activation][recovery][missing-definition]"
          "[intentional-red]")
{
    EpochFixture fixture;
    ReplicaEpochActivation replica(fixture.store, *fixture.epoch0, 2);
    const auto stage = fixture.successor();

    CHECK(replica.restore_expectation(status_for(
        2,
        stage.activation,
        ActivationRecoveryNeed::exact_definition)));
    const auto blocked = replica.on_predecessor_commit(
        kActivationHeight, fixture.epoch0->epoch_digest());
    CHECK(blocked.transition == ActivationTransition::blocked);
    CHECK(blocked.blocked_reason ==
          ActivationBlockReason::missing_definition);
    CHECK_FALSE(replica.admits_new_proposals());

    const auto recovery = replica.recovery_status();
    REQUIRE(recovery.has_value());
    CHECK(recovery->recovery_need ==
          ActivationRecoveryNeed::exact_definition);

    REQUIRE(replica.stage(stage, successor_context()).acknowledgement);
    CHECK(replica.arm(arm_for(stage.activation)) ==
          ReplicaArmDisposition::armed);
    const auto resumed = replica.replay_blocked_commit();
    CHECK(resumed.transition == ActivationTransition::activated);
    REQUIRE(resumed.effect.has_value());
    check_effect(*resumed.effect, *fixture.store.find_epoch(1), 0, 0);
}

TEST_CASE("first seen commit past activation height remains blocked",
          "[d11][epoch-activation][commit][missed-height][fail-closed]"
          "[intentional-red]")
{
    EpochFixture fixture;
    ReplicaEpochActivation replica(fixture.store, *fixture.epoch0, 0);
    const auto stage = fixture.successor();
    REQUIRE(replica.stage(stage, successor_context()).acknowledgement);

    const auto missed = replica.on_predecessor_commit(
        kActivationHeight + 1, fixture.epoch0->epoch_digest());
    CHECK(missed.transition == ActivationTransition::blocked);
    CHECK(missed.blocked_reason ==
          ActivationBlockReason::missed_activation_height);
    CHECK_FALSE(replica.admits_new_proposals());

    CHECK(replica.arm(arm_for(stage.activation)) ==
          ReplicaArmDisposition::armed);
    const auto no_fabricated_proof = replica.replay_blocked_commit();
    CHECK(no_fabricated_proof.transition == ActivationTransition::blocked);
    CHECK(no_fabricated_proof.blocked_reason ==
          ActivationBlockReason::missed_activation_height);

    const auto backwards_replay = replica.on_predecessor_commit(
        kActivationHeight, fixture.epoch0->epoch_digest());
    CHECK(backwards_replay.transition == ActivationTransition::blocked);
    CHECK(backwards_replay.blocked_reason ==
          ActivationBlockReason::missed_activation_height);
    CHECK(replica.active_effect().definition == fixture.epoch0);
}

TEST_CASE("wrong predecessor proof at activation never guesses a digest",
          "[d11][epoch-activation][commit][digest][fail-closed]"
          "[intentional-red]")
{
    EpochFixture fixture;
    ReplicaEpochActivation replica(fixture.store, *fixture.epoch0, 0);
    const auto stage = fixture.successor();
    REQUIRE(replica.stage(stage, successor_context()).acknowledgement);
    REQUIRE(replica.arm(arm_for(stage.activation)) ==
            ReplicaArmDisposition::armed);

    const auto blocked = replica.on_predecessor_commit(
        kActivationHeight, fixture_digest("wrong-commit"));
    CHECK(blocked.transition == ActivationTransition::blocked);
    CHECK(blocked.blocked_reason ==
          ActivationBlockReason::predecessor_digest_mismatch);
    CHECK_FALSE(replica.admits_new_proposals());

    const auto contradictory_replay = replica.on_predecessor_commit(
        kActivationHeight, fixture.epoch0->epoch_digest());
    CHECK(contradictory_replay.transition == ActivationTransition::blocked);
    CHECK(contradictory_replay.blocked_reason ==
          ActivationBlockReason::predecessor_digest_mismatch);
    CHECK(replica.active_effect().definition == fixture.epoch0);
}

TEST_CASE("restart replays exact stage arm and commit idempotently",
          "[d11][epoch-activation][restart][replay][idempotent]"
          "[intentional-red]")
{
    EpochFixture fixture;
    const auto stage = fixture.successor();

    {
        ReplicaEpochActivation before_restart(
            fixture.store, *fixture.epoch0, 5);
        const auto staged = before_restart.stage(
            stage, successor_context());
        CHECK(staged.disposition == ReplicaStageDisposition::staged);
        CHECK(fixture.store.size() == 2);
    }

    ReplicaEpochActivation after_restart(
        fixture.store, *fixture.epoch0, 5);
    const auto replayed_stage = after_restart.stage(
        stage, successor_context());
    CHECK(replayed_stage.disposition == ReplicaStageDisposition::duplicate);
    REQUIRE(replayed_stage.acknowledgement.has_value());
    check_identity(replayed_stage.acknowledgement->activation, stage.activation);
    CHECK(fixture.store.size() == 2);

    CHECK(after_restart.arm(arm_for(stage.activation)) ==
          ReplicaArmDisposition::armed);
    const auto activated = after_restart.on_predecessor_commit(
        kActivationHeight, fixture.epoch0->epoch_digest());
    CHECK(activated.transition == ActivationTransition::activated);
    REQUIRE(activated.effect.has_value());

    const auto replayed_commit = after_restart.replay_blocked_commit();
    CHECK(replayed_commit.transition == ActivationTransition::already_active);
    REQUIRE(replayed_commit.effect.has_value());
    CHECK(replayed_commit.effect->generation == activated.effect->generation);
}

TEST_CASE("completed activation state retires before the next exact successor",
          "[d11][epoch-activation][successive][recovery][rem-d11]"
          "[intentional-red]")
{
    EpochFixture fixture;
    ReplicaEpochActivation replica(fixture.store, *fixture.epoch0, 0);
    const auto containment = fixture.successor();
    REQUIRE(replica.stage(containment, successor_context()).acknowledgement);
    REQUIRE(replica.arm(arm_for(containment.activation)) ==
            ReplicaArmDisposition::armed);

    const auto containment_activation = replica.on_predecessor_commit(
        kActivationHeight, fixture.epoch0->epoch_digest());
    REQUIRE(containment_activation.transition ==
            ActivationTransition::activated);
    REQUIRE(containment_activation.effect.has_value());
    const auto *const epoch1 = fixture.store.find_epoch(1);
    REQUIRE(epoch1 != nullptr);

    const ConfigurationId epoch0_tree0{
        fixture.epoch0->epoch_number(),
        0,
        fixture.epoch0->epoch_digest()};
    const ConfigurationId epoch1_tree0{
        epoch1->epoch_number(), 0, epoch1->epoch_digest()};
    CHECK(replica.blocked_reason() == ActivationBlockReason::none);
    CHECK_FALSE(replica.recovery_status().has_value());
    CHECK(replica.admits_new_proposals());
    CHECK(replica.may_drain_exact_context(epoch1_tree0));
    CHECK(replica.may_drain_exact_context(epoch0_tree0));

    const auto exact_replay = replica.on_predecessor_commit(
        kActivationHeight, fixture.epoch0->epoch_digest());
    CHECK(exact_replay.transition == ActivationTransition::already_active);
    CHECK(exact_replay.blocked_reason == ActivationBlockReason::none);
    REQUIRE(exact_replay.effect.has_value());
    check_effect(*exact_replay.effect, *epoch1, 0, 0);

    const auto later_commit = replica.on_predecessor_commit(
        kActivationHeight + 1, fixture.epoch0->epoch_digest());
    CHECK(later_commit.transition == ActivationTransition::waiting);
    CHECK(later_commit.blocked_reason == ActivationBlockReason::none);
    CHECK_FALSE(later_commit.effect.has_value());

    const auto contradictory_commit = replica.on_predecessor_commit(
        kActivationHeight, fixture_digest("contradictory-completed-proof"));
    CHECK(contradictory_commit.transition == ActivationTransition::waiting);
    CHECK(contradictory_commit.blocked_reason == ActivationBlockReason::none);
    CHECK_FALSE(contradictory_commit.effect.has_value());

    check_effect(replica.active_effect(), *epoch1, 0, 0);
    CHECK(replica.blocked_reason() == ActivationBlockReason::none);
    CHECK_FALSE(replica.recovery_status().has_value());
    CHECK(replica.admits_new_proposals());
    CHECK(replica.may_drain_exact_context(epoch1_tree0));
    CHECK(replica.may_drain_exact_context(epoch0_tree0));

    auto optimization_input = successor_input(*epoch1);
    optimization_input.activation_height = kActivationHeight * 2;
    optimization_input.generation_seed = 0xD112;
    optimization_input.policy_version = "d11-optimized-v1";
    optimization_input.evidence_snapshot_id = "d11-window-2";
    optimization_input.evidence_cutoff = 100;
    const auto optimization =
        stage_message(*epoch1, std::move(optimization_input));
    const EpochValidationContext optimization_context{
        kActivationHeight, 5, {}};

    const auto staged_optimization =
        replica.stage(optimization, optimization_context);
    CHECK(staged_optimization.disposition == ReplicaStageDisposition::staged);
    REQUIRE(staged_optimization.acknowledgement.has_value());
    check_identity(
        staged_optimization.acknowledgement->activation,
        optimization.activation);
    CHECK(replica.arm(arm_for(optimization.activation)) ==
          ReplicaArmDisposition::armed);

    const auto before_height = replica.on_predecessor_commit(
        optimization.activation.activation_height - 1,
        epoch1->epoch_digest());
    CHECK(before_height.transition == ActivationTransition::waiting);
    CHECK(replica.admits_new_proposals());

    const auto optimization_activation = replica.on_predecessor_commit(
        optimization.activation.activation_height,
        epoch1->epoch_digest());
    REQUIRE(optimization_activation.transition ==
            ActivationTransition::activated);
    REQUIRE(optimization_activation.effect.has_value());
    const auto *const epoch2 = fixture.store.find_epoch(2);
    REQUIRE(epoch2 != nullptr);
    check_effect(*optimization_activation.effect, *epoch2, 0, 0);

    const ConfigurationId epoch2_tree0{
        epoch2->epoch_number(), 0, epoch2->epoch_digest()};
    CHECK(replica.blocked_reason() == ActivationBlockReason::none);
    CHECK_FALSE(replica.recovery_status().has_value());
    CHECK(replica.admits_new_proposals());
    CHECK(replica.may_drain_exact_context(epoch2_tree0));
    CHECK(replica.may_drain_exact_context(epoch1_tree0));
    CHECK_FALSE(replica.may_drain_exact_context(epoch0_tree0));
}

TEST_CASE("tree rotations use a checked ordinal independent of tree id",
          "[d11][epoch-activation][rotation][generation][overflow]"
          "[intentional-red]")
{
    EpochFixture fixture;
    ReplicaEpochActivation replica(fixture.store, *fixture.epoch0, 0);
    const auto stage = fixture.successor();
    REQUIRE(replica.stage(stage, successor_context()).acknowledgement);
    REQUIRE(replica.arm(arm_for(stage.activation)) ==
            ReplicaArmDisposition::armed);
    REQUIRE(replica.on_predecessor_commit(
                kActivationHeight,
                fixture.epoch0->epoch_digest())
                .transition == ActivationTransition::activated);
    const auto *const epoch1 = fixture.store.find_epoch(1);
    REQUIRE(epoch1 != nullptr);

    check_effect(replica.active_effect(), *epoch1, 0, 0);
    const auto tree_b = replica.rotate_to_tree(1);
    check_effect(tree_b, *epoch1, 1, 1);
    const auto tree_a_again = replica.rotate_to_tree(0);
    check_effect(tree_a_again, *epoch1, 0, 2);
    CHECK(tree_a_again.generation > tree_b.generation);

    const auto before_unknown = replica.active_effect();
    CHECK_THROWS(replica.rotate_to_tree(999));
    const auto after_unknown = replica.active_effect();
    CHECK(after_unknown.configuration == before_unknown.configuration);
    CHECK(after_unknown.rotation_ordinal == before_unknown.rotation_ordinal);
    CHECK(after_unknown.generation == before_unknown.generation);

    REQUIRE(hotstuff::checked_successor_epoch(
                std::numeric_limits<std::uint32_t>::max() - 1)
                .has_value());
    CHECK(*hotstuff::checked_successor_epoch(
              std::numeric_limits<std::uint32_t>::max() - 1) ==
          std::numeric_limits<std::uint32_t>::max());
    CHECK_FALSE(hotstuff::checked_successor_epoch(
                    std::numeric_limits<std::uint32_t>::max())
                    .has_value());

    const auto maximum_ordinal =
        std::numeric_limits<std::uint32_t>::max();
    REQUIRE(hotstuff::checked_activation_generation(1, maximum_ordinal));
    CHECK_FALSE(hotstuff::checked_activation_generation(
                    1,
                    static_cast<std::uint64_t>(maximum_ordinal) + 1)
                    .has_value());

    ReplicaEpochActivation exhausted(
        fixture.store,
        *fixture.epoch0,
        0,
        0,
        maximum_ordinal);
    const auto before_overflow = exhausted.active_effect();
    CHECK_THROWS_AS(exhausted.rotate_to_tree(1), std::overflow_error);
    const auto after_overflow = exhausted.active_effect();
    CHECK(after_overflow.configuration == before_overflow.configuration);
    CHECK(after_overflow.rotation_ordinal == maximum_ordinal);
    CHECK(after_overflow.generation == before_overflow.generation);
}

TEST_CASE("activation generations reserve zero and reject packed overflow",
          "[d11][epoch-activation][generation][overflow]"
          "[intentional-red]")
{
    const auto first = hotstuff::checked_activation_generation(0, 0);
    REQUIRE(first.has_value());
    CHECK(*first == 1);
    EpochFixture fixture;
    ReplicaEpochActivation initial(
        fixture.store, *fixture.epoch0, 0);
    check_effect(initial.active_effect(), *fixture.epoch0, 0, 0);
    CHECK(initial.active_effect().generation == 1);

    const auto maximum_ordinal =
        std::numeric_limits<std::uint32_t>::max();
    const auto maximum_valid =
        hotstuff::checked_activation_generation(
            std::numeric_limits<std::uint32_t>::max(),
            static_cast<std::uint64_t>(maximum_ordinal) - 1);
    REQUIRE(maximum_valid.has_value());
    CHECK(*maximum_valid ==
          std::numeric_limits<std::uint64_t>::max());

    CHECK_FALSE(hotstuff::checked_activation_generation(
                    std::numeric_limits<std::uint32_t>::max(),
                    maximum_ordinal)
                    .has_value());
    CHECK_FALSE(hotstuff::checked_activation_generation(
                    0,
                    static_cast<std::uint64_t>(maximum_ordinal) + 1)
                    .has_value());
}

TEST_CASE("phase one implementation is pure state and wire only",
          "[d11][epoch-activation][source-audit][pure]"
          "[intentional-red]")
{
    const auto activation_header = read_source(
        "include/hotstuff/epoch_activation.h");
    const auto activation_source = read_source(
        "src/epoch_activation.cpp");
    const auto wire_header = read_source("include/hotstuff/epoch_wire.h");
    const auto wire_source = read_source("src/epoch_wire.cpp");
    const auto implementation = activation_header + "\n" +
                                activation_source + "\n" +
                                wire_header + "\n" + wire_source;

    for (const auto *required :
         {"legacy_static",
          "adaptive_v1",
          "externally serialized",
          "exact commit proof",
          "not batch-transactional",
          "does not install message handlers"})
    {
        INFO("Missing D11 phase-1 contract phrase: " << required);
        CHECK(implementation.find(required) != std::string::npos);
    }

    CHECK(wire_source.find("MsgStageEpochDefinition::opcode") !=
          std::string::npos);
    CHECK(wire_source.find("MsgStageAck::opcode") != std::string::npos);
    CHECK(wire_source.find("MsgArmActivation::opcode") !=
          std::string::npos);
    CHECK(wire_source.find("MsgActivationStatus::opcode") !=
          std::string::npos);
    CHECK(implementation.find("0x10") != std::string::npos);
    CHECK(implementation.find("0x11") != std::string::npos);
    CHECK(implementation.find("0x12") != std::string::npos);

    INFO("phase one has no network, handler, timer, or wall-clock ownership");
    for (const auto *forbidden :
         {"PeerNetwork",
          "MsgNetwork",
          "EventContext",
          "NetAddr",
          "send_msg(",
          "reg_handler",
          "TimerEvent",
          "schedule_after",
          "system_clock",
          "steady_clock",
          "high_resolution_clock",
          "gettimeofday",
          "CLOCK_REALTIME"})
    {
        CHECK(implementation.find(forbidden) == std::string::npos);
    }

    INFO("phase one cannot mutate consensus quorum or adaptation scores");
    for (const auto *forbidden :
         {"HotStuffBase",
          "HotStuffCore",
          "QuorumCert",
          "on_receive_vote",
          "add_verified_part",
          "do_consensus",
          "AdaptationSnapshot",
          "build_adaptation_snapshot",
          "responsiveness_score",
          "reputation_score"})
    {
        CHECK(implementation.find(forbidden) == std::string::npos);
    }

    INFO("phase one does not claim live proposal draining or TLS coverage");
    for (const auto *forbidden :
         {"ProposalAdmissionCoordinator",
          "PendingProposalBuffer",
          "SSL_",
          "X509",
          "sockaddr"})
    {
        CHECK(implementation.find(forbidden) == std::string::npos);
    }
}
