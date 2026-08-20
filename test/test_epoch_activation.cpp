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
#include "hotstuff/adaptive_v2_convergence_wire.h"
#include "hotstuff/client.h"
#include "hotstuff/epoch_change.h"
#include "hotstuff/epoch_store.h"
#include "hotstuff/evidence.h"
#include "../src/detail/canonical_wire_codec.h"

/*
 * CERT13-T0 compile contract
 * -------------------------
 * Adaptive-v3 production interfaces do not exist at this checkpoint. Keep
 * declarations test-local, with no fallback implementation, so this target is
 * intentionally RED at compile time until the certified-activation boundary
 * is implemented. Once the production header exists, it replaces these
 * declarations and the same behavioral tests exercise the real API.
 */
#if __has_include("hotstuff/adaptive_v3_activation_readiness.h")
#include "hotstuff/adaptive_v3_activation_readiness.h"
#define KAURI_HAS_CERT13_ACTIVATION_API 1
#else
#define KAURI_HAS_CERT13_ACTIVATION_API 0

namespace hotstuff
{

constexpr std::uint32_t
    kAdaptiveV3ActivationReadinessSchemaVersionV1 = 1;

struct AdaptiveV3ActivationReadinessLimits
{
    std::size_t maximum_payload_bytes{32 * 1024};
    std::size_t maximum_members{31};
};

struct AdaptiveV3ActivationSchedule
{
    uint256_t membership_digest;
    std::uint32_t predecessor_epoch_number{0};
    uint256_t predecessor_epoch_digest;
    std::uint32_t successor_epoch_number{0};
    uint256_t successor_epoch_digest;
    std::uint64_t successor_activation_generation{0};
    uint256_t command_payload_digest;
    std::uint64_t command_block_height{0};
    uint256_t command_block_hash;
    std::uint64_t activation_delay_blocks{0};
    std::uint64_t activation_height{0};
};

struct AdaptiveV3ActivationReadyIdentity
{
    std::uint32_t schema_version{
        kAdaptiveV3ActivationReadinessSchemaVersionV1};
    uint256_t membership_digest;
    ConfigurationId predecessor_boundary_configuration;
    std::uint64_t predecessor_boundary_generation{0};
    ConfigurationId successor_configuration;
    std::uint64_t successor_activation_generation{0};
    uint256_t command_payload_digest;
    std::uint64_t command_block_height{0};
    uint256_t command_block_hash;
    std::uint64_t activation_delay_blocks{0};
    std::uint64_t activation_height{0};
    uint256_t activation_boundary_block_hash;

    bool operator==(
        const AdaptiveV3ActivationReadyIdentity &other) const noexcept;
    bool operator!=(
        const AdaptiveV3ActivationReadyIdentity &other) const noexcept;
};

struct AdaptiveV3ActivationReadyObservation
{
    AdaptiveV3ActivationReadyIdentity identity;
    ReplicaID signer_replica_id{0};
    std::uint64_t signer_source_sequence{0};
    std::uint64_t signer_monotonic_raw_ns{0};
    bool vote_fence_engaged{false};
    SigSecBLS signature;
};

struct AdaptiveV3ActivationReadinessCertificate
{
    std::uint32_t schema_version{
        kAdaptiveV3ActivationReadinessSchemaVersionV1};
    AdaptiveV3ActivationReadyIdentity identity;
    std::vector<AdaptiveV3ActivationReadyObservation> observations;
    uint256_t certificate_digest;
};

struct AdaptiveV3ReadinessMember
{
    ReplicaID replica_id{0};
    PubKeyBLS public_key;
};

enum class AdaptiveV3ActivationReadinessWireError : std::uint8_t
{
    none = 0,
    invalid_limits,
    payload_too_large,
    truncated,
    trailing_bytes,
    invalid_domain,
    unsupported_schema,
    noncanonical_encoding,
    invalid_identity,
    duplicate_signer,
    too_many_members,
    malformed_signature,
    allocation_failure,
    internal_failure,
};

template<typename Value>
struct AdaptiveV3ActivationReadinessDecodeResult
{
    AdaptiveV3ActivationReadinessWireError error{
        AdaptiveV3ActivationReadinessWireError::none};
    std::optional<Value> value;

    explicit operator bool() const noexcept
    {
        return error == AdaptiveV3ActivationReadinessWireError::none &&
               value.has_value();
    }
};

using AdaptiveV3ActivationReadyObservationDecodeResult =
    AdaptiveV3ActivationReadinessDecodeResult<
        AdaptiveV3ActivationReadyObservation>;
using AdaptiveV3ActivationReadinessCertificateDecodeResult =
    AdaptiveV3ActivationReadinessDecodeResult<
        AdaptiveV3ActivationReadinessCertificate>;

const std::string &
adaptive_v3_activation_ready_observation_domain() noexcept;
const std::string &
adaptive_v3_activation_readiness_certificate_domain() noexcept;

bytearray_t encode_adaptive_v3_activation_ready_observation(
    const AdaptiveV3ActivationReadyObservation &observation,
    const AdaptiveV3ActivationReadinessLimits &limits);
AdaptiveV3ActivationReadyObservationDecodeResult
decode_adaptive_v3_activation_ready_observation(
    const bytearray_t &payload,
    const AdaptiveV3ActivationReadinessLimits &limits) noexcept;

bytearray_t encode_adaptive_v3_activation_readiness_certificate(
    const AdaptiveV3ActivationReadinessCertificate &certificate,
    const AdaptiveV3ActivationReadinessLimits &limits);
AdaptiveV3ActivationReadinessCertificateDecodeResult
decode_adaptive_v3_activation_readiness_certificate(
    const bytearray_t &payload,
    const AdaptiveV3ActivationReadinessLimits &limits) noexcept;

AdaptiveV3ActivationReadinessCertificate
make_adaptive_v3_activation_readiness_certificate(
    const AdaptiveV3ActivationReadyIdentity &identity,
    std::vector<AdaptiveV3ActivationReadyObservation> observations,
    const AdaptiveV3ActivationReadinessLimits &limits);

enum class AdaptiveV3CertifiedActivationState : std::uint8_t
{
    awaiting_boundary = 1,
    prepared,
    active,
    blocked,
};

enum class AdaptiveV3BoundaryDisposition : std::uint8_t
{
    waiting = 1,
    prepared,
    already_prepared,
    activated_from_buffered_certificate,
    rejected,
};

struct AdaptiveV3BoundaryResult
{
    AdaptiveV3BoundaryDisposition disposition{
        AdaptiveV3BoundaryDisposition::rejected};
    std::optional<AdaptiveV3ActivationReadyObservation> observation;
};

enum class AdaptiveV3CertificateDisposition : std::uint8_t
{
    accepted = 1,
    duplicate,
    buffered_early,
    rejected_below_quorum,
    rejected_noncanonical,
    rejected_stale,
    rejected_wrong_identity,
    rejected_mixed_identity,
    rejected_invalid_signature,
    rejected_nonmember,
    terminal,
};

class AdaptiveV3CertifiedActivationGate final
{
public:
    AdaptiveV3CertifiedActivationGate(
        AdaptiveV3ActivationSchedule schedule,
        ReplicaID local_replica,
        std::shared_ptr<const PrivKeyBLS> local_private_key,
        std::vector<AdaptiveV3ReadinessMember> membership,
        std::size_t fixed_quorum);
    ~AdaptiveV3CertifiedActivationGate();

    AdaptiveV3CertifiedActivationGate(
        const AdaptiveV3CertifiedActivationGate &) = delete;
    AdaptiveV3CertifiedActivationGate &operator=(
        const AdaptiveV3CertifiedActivationGate &) = delete;
    AdaptiveV3CertifiedActivationGate(
        AdaptiveV3CertifiedActivationGate &&) = delete;
    AdaptiveV3CertifiedActivationGate &operator=(
        AdaptiveV3CertifiedActivationGate &&) = delete;

    AdaptiveV3BoundaryResult observe_predecessor_commit(
        std::uint64_t committed_height,
        const ConfigurationId &configuration,
        std::uint64_t generation,
        const uint256_t &block_hash,
        std::uint64_t source_sequence,
        std::uint64_t monotonic_raw_ns) noexcept;

    AdaptiveV3CertificateDisposition observe_certificate(
        const AdaptiveV3ActivationReadinessCertificate &certificate)
        noexcept;

    AdaptiveV3CertifiedActivationState state() const noexcept;
    const ConfigurationId &active_configuration() const noexcept;
    std::uint64_t active_generation() const noexcept;
    bool vote_fence_engaged() const noexcept;
    bool may_authorize_vote(
        const ConfigurationId &configuration,
        std::uint64_t generation) const noexcept;
    std::optional<std::uint64_t>
    certificate_apply_committed_height() const noexcept;
};

} // namespace hotstuff
#endif

static_assert(
    KAURI_HAS_CERT13_ACTIVATION_API == 1,
    "CERT13-T0 RED: adaptive_v3 certified-activation API is missing");

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
using hotstuff::ActivationRecord;
using hotstuff::ActivationRecordDisposition;
using hotstuff::ActivationRecoveryNeed;
using hotstuff::ActivationStatus;
using hotstuff::ActivationTransition;
using hotstuff::ArmActivation;
using hotstuff::AuthorizedEpochChange;
using hotstuff::AuthenticatedReporter;
using hotstuff::ConfigurationId;
using hotstuff::EpochAckTracker;
using hotstuff::EpochActivationEffect;
using hotstuff::EpochActivationIdentity;
using hotstuff::EpochActivationResult;
using hotstuff::EpochDefinition;
using hotstuff::EpochDefinitionInput;
using hotstuff::EpochChangePayload;
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

EpochDefinitionInput successor_v2_input(const EpochDefinition &predecessor)
{
    auto input = successor_input(predecessor);
    input.schema_version = hotstuff::kEpochDefinitionSchemaVersionV2;
    input.activation_height = 0;
    input.policy_version = "c08a-adaptive-v2";
    input.evidence_snapshot_id = "c08a-containment";
    input.trees[0].wait_exempt_leaves = {4, 5};
    input.trees[1].wait_exempt_leaves = {4, 5};
    std::sort(
        input.trees.begin(), input.trees.end(),
        [](const auto &left, const auto &right) {
            return left.tree_id < right.tree_id;
        });
    input.epoch_digest.reset();
    return input;
}

EpochDefinitionInput epoch_zero_v2_input()
{
    auto input = epoch_zero_input();
    input.schema_version = hotstuff::kEpochDefinitionSchemaVersionV2;
    input.policy_version = "c08a-baseline-v2";
    input.evidence_snapshot_id = "c08a-baseline";
    input.epoch_digest.reset();
    return input;
}

AuthorizedEpochChange prevalidated_v2_command(
    const EpochDefinition &predecessor,
    const uint256_t &successor_digest,
    std::uint64_t delay)
{
    hotstuff::PrivKeySecp256k1 key;
    key.from_hex(
        "4aede145d13021fb43c938bced67511a7740c05786d3e0b94ffbdaa7f15afc57");
    return hotstuff::authorize_epoch_change(
        EpochChangePayload{
            predecessor.epoch_number() + 1,
            predecessor.epoch_digest(),
            successor_digest,
            delay},
        17,
        key);
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

struct EpochV2Fixture
{
    EpochStore store{membership7()};
    const EpochDefinition *epoch0{nullptr};

    EpochV2Fixture()
    {
        epoch0 = &store.stage(epoch_zero_v2_input(), epoch_zero_context());
    }
};

template <typename Fixture>
const EpochDefinition &stage_v2_successor(Fixture &fixture)
{
    const auto staged = fixture.store.stage_available_v2(
        successor_v2_input(*fixture.epoch0), *fixture.epoch0);
    REQUIRE(staged.disposition ==
            hotstuff::DefinitionAvailabilityDisposition::staged);
    REQUIRE(staged.definition != nullptr);
    return *staged.definition;
}

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

void check_record(
    const ActivationRecord &record,
    const EpochDefinition &predecessor,
    const EpochDefinition &successor,
    const AuthorizedEpochChange &command,
    std::uint64_t commit_height,
    std::uint64_t activation_height)
{
    CHECK(record.predecessor_epoch_number == predecessor.epoch_number());
    CHECK(record.predecessor_epoch_digest == predecessor.epoch_digest());
    CHECK(record.successor_epoch_number == successor.epoch_number());
    CHECK(record.successor_epoch_digest == successor.epoch_digest());
    CHECK(record.payload_digest ==
          hotstuff::epoch_change_payload_digest(command.payload));
    CHECK(record.command_commit_height == commit_height);
    CHECK(record.activation_delay_blocks ==
          command.payload.activation_delay_blocks);
    CHECK(record.activation_height == activation_height);
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

TEST_CASE("C08a freezes one committed adaptive-v2 activation record",
          "[c08a][epoch-activation][adaptive-v2][record][idempotent]")
{
    constexpr std::uint64_t commit_height = 40;
    constexpr std::uint64_t delay = 5;
    EpochV2Fixture fixture;
    const auto &successor = stage_v2_successor(fixture);
    ReplicaEpochActivation replica(fixture.store, *fixture.epoch0, 0);
    const auto command = prevalidated_v2_command(
        *fixture.epoch0, successor.epoch_digest(), delay);

    const auto recorded = replica.record_committed_v2(
        command, commit_height);
    CHECK(recorded.disposition == ActivationRecordDisposition::recorded);
    REQUIRE(recorded.record.has_value());
    check_record(
        *recorded.record,
        *fixture.epoch0,
        successor,
        command,
        commit_height,
        commit_height + delay);
    REQUIRE(replica.committed_v2_record().has_value());
    check_record(
        *replica.committed_v2_record(),
        *fixture.epoch0,
        successor,
        command,
        commit_height,
        commit_height + delay);
    CHECK(successor.schema_version() ==
          hotstuff::kEpochDefinitionSchemaVersionV2);
    CHECK(successor.activation_height() == 0);
    check_effect(replica.active_effect(), *fixture.epoch0, 0, 0);

    const auto duplicate = replica.record_committed_v2(
        command, commit_height);
    CHECK(duplicate.disposition == ActivationRecordDisposition::duplicate);
    const auto later_duplicate = replica.record_committed_v2(
        command, commit_height + 3);
    CHECK(later_duplicate.disposition ==
          ActivationRecordDisposition::duplicate);
    REQUIRE(later_duplicate.record.has_value());
    CHECK(*later_duplicate.record == *recorded.record);
    REQUIRE(replica.committed_v2_record().has_value());
    CHECK(replica.committed_v2_record()->command_commit_height ==
          commit_height);
    CHECK(replica.committed_v2_record()->activation_height ==
          commit_height + delay);
    CHECK(replica.blocked_reason() == ActivationBlockReason::none);
    CHECK(replica.admits_new_proposals());
}

TEST_CASE("C08a rejects invalid committed adaptive-v2 records fail closed",
          "[c08a][epoch-activation][adaptive-v2][record][negative]")
{
    constexpr std::uint64_t commit_height = 40;

    SECTION("unsupported command schema")
    {
        EpochV2Fixture fixture;
        const auto &successor = stage_v2_successor(fixture);
        ReplicaEpochActivation replica(fixture.store, *fixture.epoch0, 0);
        auto command = prevalidated_v2_command(
            *fixture.epoch0, successor.epoch_digest(), 5);
        ++command.schema_version;
        CHECK(replica.record_committed_v2(command, commit_height).disposition ==
              ActivationRecordDisposition::unsupported_schema);
        CHECK_FALSE(replica.committed_v2_record().has_value());
        CHECK_FALSE(replica.admits_new_proposals());
    }

    SECTION("non-v2 command")
    {
        EpochV2Fixture fixture;
        const auto &successor = stage_v2_successor(fixture);
        ReplicaEpochActivation replica(fixture.store, *fixture.epoch0, 0);
        auto command = prevalidated_v2_command(
            *fixture.epoch0, successor.epoch_digest(), 5);
        command.protocol_mode = EpochProtocolMode::adaptive_v1;
        CHECK(replica.record_committed_v2(command, commit_height).disposition ==
              ActivationRecordDisposition::wrong_mode);
        CHECK_FALSE(replica.committed_v2_record().has_value());
        CHECK_FALSE(replica.admits_new_proposals());
    }

    SECTION("non-v2 active predecessor")
    {
        EpochFixture fixture;
        const auto &successor = stage_v2_successor(fixture);
        ReplicaEpochActivation replica(fixture.store, *fixture.epoch0, 0);
        const auto command = prevalidated_v2_command(
            *fixture.epoch0, successor.epoch_digest(), 5);
        CHECK(replica.record_committed_v2(command, commit_height).disposition ==
              ActivationRecordDisposition::wrong_mode);
        CHECK_FALSE(replica.committed_v2_record().has_value());
        CHECK_FALSE(replica.admits_new_proposals());
    }

    SECTION("wrong predecessor")
    {
        EpochV2Fixture fixture;
        const auto &successor = stage_v2_successor(fixture);
        ReplicaEpochActivation replica(fixture.store, *fixture.epoch0, 0);
        auto command = prevalidated_v2_command(
            *fixture.epoch0, successor.epoch_digest(), 5);
        command.payload.predecessor_epoch_digest =
            fixture_digest("wrong-v2-predecessor");
        CHECK(replica.record_committed_v2(command, commit_height).disposition ==
              ActivationRecordDisposition::wrong_predecessor);
        CHECK_FALSE(replica.committed_v2_record().has_value());
        CHECK_FALSE(replica.admits_new_proposals());
    }

    SECTION("wrong successor")
    {
        EpochV2Fixture fixture;
        const auto &successor = stage_v2_successor(fixture);
        ReplicaEpochActivation replica(fixture.store, *fixture.epoch0, 0);
        auto command = prevalidated_v2_command(
            *fixture.epoch0, successor.epoch_digest(), 5);
        ++command.payload.successor_epoch_number;
        CHECK(replica.record_committed_v2(command, commit_height).disposition ==
              ActivationRecordDisposition::wrong_successor);
        CHECK_FALSE(replica.committed_v2_record().has_value());
        CHECK_FALSE(replica.admits_new_proposals());
    }

    SECTION("missing definition")
    {
        EpochV2Fixture fixture;
        ReplicaEpochActivation replica(fixture.store, *fixture.epoch0, 0);
        const auto input = successor_v2_input(*fixture.epoch0);
        const auto command = prevalidated_v2_command(
            *fixture.epoch0, hotstuff::compute_epoch_digest(input), 5);
        const auto missing = replica.record_committed_v2(
            command, commit_height);
        CHECK(missing.disposition ==
              ActivationRecordDisposition::missing_definition);
        REQUIRE(missing.record.has_value());
        CHECK(missing.record->predecessor_epoch_number ==
              fixture.epoch0->epoch_number());
        CHECK(missing.record->predecessor_epoch_digest ==
              fixture.epoch0->epoch_digest());
        CHECK(missing.record->successor_epoch_number ==
              input.epoch_number);
        CHECK(missing.record->successor_epoch_digest ==
              hotstuff::compute_epoch_digest(input));
        CHECK(missing.record->payload_digest ==
              hotstuff::epoch_change_payload_digest(command.payload));
        CHECK(missing.record->command_commit_height == commit_height);
        CHECK(missing.record->activation_delay_blocks == 5);
        CHECK(missing.record->activation_height == commit_height + 5);
        REQUIRE(replica.committed_v2_record().has_value());
        CHECK(*replica.committed_v2_record() == *missing.record);
        CHECK(replica.blocked_reason() ==
              ActivationBlockReason::missing_definition);
        CHECK_FALSE(replica.admits_new_proposals());

        SECTION("the exact staged definition resumes the original proof")
        {
            const auto blocked = replica.on_v2_post_block_commit(
                commit_height + 5, fixture.epoch0->epoch_digest());
            CHECK(blocked.transition == ActivationTransition::blocked);
            CHECK(blocked.blocked_reason ==
                  ActivationBlockReason::missing_definition);

            const auto staged = fixture.store.stage_available_v2(
                input, *fixture.epoch0);
            REQUIRE(staged.disposition ==
                    hotstuff::DefinitionAvailabilityDisposition::staged);
            REQUIRE(staged.definition != nullptr);
            CHECK_FALSE(replica.admits_new_proposals());

            const auto replayed = replica.record_committed_v2(
                command, commit_height + 1);
            CHECK(replayed.disposition ==
                  ActivationRecordDisposition::duplicate);
            REQUIRE(replayed.record.has_value());
            CHECK(*replayed.record == *missing.record);
            CHECK(replayed.record->command_commit_height ==
                  commit_height);
            CHECK(replayed.record->activation_height ==
                  commit_height + 5);
            CHECK(replica.blocked_reason() ==
                  ActivationBlockReason::none);
            CHECK(replica.admits_new_proposals());

            const auto activated = replica.on_v2_post_block_commit(
                commit_height + 5, fixture.epoch0->epoch_digest());
            CHECK(activated.transition ==
                  ActivationTransition::activated);
            REQUIRE(activated.effect.has_value());
            check_effect(*activated.effect, *staged.definition, 0, 0);
        }

        SECTION("the same payload at a later height retains the boundary")
        {
            const auto later_duplicate = replica.record_committed_v2(
                command, commit_height + 1);
            CHECK(later_duplicate.disposition ==
                  ActivationRecordDisposition::duplicate);
            REQUIRE(later_duplicate.record.has_value());
            CHECK(*later_duplicate.record == *missing.record);
            CHECK(later_duplicate.record->command_commit_height ==
                  commit_height);
            CHECK(later_duplicate.record->activation_height ==
                  commit_height + 5);
            REQUIRE(replica.committed_v2_record().has_value());
            CHECK(*replica.committed_v2_record() == *missing.record);
            CHECK(replica.blocked_reason() ==
                  ActivationBlockReason::missing_definition);
            CHECK_FALSE(replica.admits_new_proposals());
        }

        SECTION("a genuinely different payload conflicts")
        {
            auto conflicting_command = command;
            ++conflicting_command.payload.activation_delay_blocks;
            const auto rejected = replica.record_committed_v2(
                conflicting_command, commit_height + 1);
            CHECK(rejected.disposition ==
                  ActivationRecordDisposition::conflicting_record);
            CHECK_FALSE(rejected.record.has_value());
            REQUIRE(replica.committed_v2_record().has_value());
            CHECK(*replica.committed_v2_record() == *missing.record);
            CHECK(replica.blocked_reason() ==
                  ActivationBlockReason::conflicting_activation_record);
            CHECK_FALSE(replica.admits_new_proposals());
        }
    }

    SECTION("mismatched scheduled definition")
    {
        EpochV2Fixture fixture;
        const auto &scheduled = fixture.store.stage(
            successor_input(*fixture.epoch0), successor_context());
        ReplicaEpochActivation replica(fixture.store, *fixture.epoch0, 0);
        const auto command = prevalidated_v2_command(
            *fixture.epoch0, scheduled.epoch_digest(), 5);
        CHECK(replica.record_committed_v2(command, commit_height).disposition ==
              ActivationRecordDisposition::mismatched_definition);
        CHECK_FALSE(replica.committed_v2_record().has_value());
        CHECK_FALSE(replica.admits_new_proposals());
    }

    SECTION("activation height overflow")
    {
        EpochV2Fixture fixture;
        const auto &successor = stage_v2_successor(fixture);
        ReplicaEpochActivation replica(fixture.store, *fixture.epoch0, 0);
        const auto command = prevalidated_v2_command(
            *fixture.epoch0, successor.epoch_digest(), 5);
        CHECK(replica.record_committed_v2(
                  command,
                  std::numeric_limits<std::uint64_t>::max() - 4)
                  .disposition ==
              ActivationRecordDisposition::activation_height_overflow);
        CHECK_FALSE(replica.committed_v2_record().has_value());
        CHECK_FALSE(replica.admits_new_proposals());
    }
}

TEST_CASE("C08a explicit committed adaptive-v2 failure pauses proposals",
          "[c08a][epoch-activation][adaptive-v2][record][fail-closed]")
{
    EpochV2Fixture fixture;
    ReplicaEpochActivation replica(fixture.store, *fixture.epoch0, 0);

    replica.fail_committed_v2(
        ActivationBlockReason::invalid_activation_record);

    CHECK(replica.blocked_reason() ==
          ActivationBlockReason::invalid_activation_record);
    CHECK_FALSE(replica.admits_new_proposals());
    const auto blocked = replica.on_v2_post_block_commit(
        40, fixture.epoch0->epoch_digest());
    CHECK(blocked.transition == ActivationTransition::blocked);
    CHECK(blocked.blocked_reason ==
          ActivationBlockReason::invalid_activation_record);
    CHECK_FALSE(blocked.effect.has_value());

    replica.fail_committed_v2(ActivationBlockReason::none);
    CHECK(replica.blocked_reason() ==
          ActivationBlockReason::invalid_activation_record);
    CHECK_FALSE(replica.admits_new_proposals());
}

TEST_CASE("C08a preserves the first record and blocks a conflicting schedule",
          "[c08a][epoch-activation][adaptive-v2][record][conflict]")
{
    constexpr std::uint64_t commit_height = 40;
    EpochV2Fixture fixture;
    const auto &successor = stage_v2_successor(fixture);
    ReplicaEpochActivation replica(fixture.store, *fixture.epoch0, 0);
    const auto command = prevalidated_v2_command(
        *fixture.epoch0, successor.epoch_digest(), 5);
    REQUIRE(replica.record_committed_v2(command, commit_height).disposition ==
            ActivationRecordDisposition::recorded);

    const auto conflicting = prevalidated_v2_command(
        *fixture.epoch0, successor.epoch_digest(), 6);
    const auto result = replica.record_committed_v2(
        conflicting, commit_height + 1);
    CHECK(result.disposition ==
          ActivationRecordDisposition::conflicting_record);
    REQUIRE(replica.committed_v2_record().has_value());
    check_record(
        *replica.committed_v2_record(),
        *fixture.epoch0,
        successor,
        command,
        commit_height,
        commit_height + 5);
    CHECK_FALSE(replica.admits_new_proposals());
}

TEST_CASE("C08a activates v2 only after the exact post-block boundary",
          "[c08a][epoch-activation][adaptive-v2][post-block]")
{
    constexpr std::uint64_t commit_height = 40;
    constexpr std::uint64_t delay = 5;
    constexpr std::uint64_t activation_height = commit_height + delay;
    EpochV2Fixture fixture;
    const auto &successor = stage_v2_successor(fixture);
    ReplicaEpochActivation replica(fixture.store, *fixture.epoch0, 0);
    const auto command = prevalidated_v2_command(
        *fixture.epoch0, successor.epoch_digest(), delay);
    REQUIRE(replica.record_committed_v2(command, commit_height).disposition ==
            ActivationRecordDisposition::recorded);

    const auto preview = replica.preview_v2_post_block_commit(
        activation_height, fixture.epoch0->epoch_digest());
    CHECK(preview.transition == ActivationTransition::activated);
    REQUIRE(preview.effect.has_value());
    check_effect(*preview.effect, successor, 0, 0);
    check_effect(replica.active_effect(), *fixture.epoch0, 0, 0);

    const auto early = replica.on_v2_post_block_commit(
        activation_height - 1, fixture.epoch0->epoch_digest());
    CHECK(early.transition == ActivationTransition::waiting);
    CHECK_FALSE(early.effect.has_value());
    check_effect(replica.active_effect(), *fixture.epoch0, 0, 0);

    const auto activated = replica.on_v2_post_block_commit(
        activation_height, fixture.epoch0->epoch_digest());
    CHECK(activated.transition == ActivationTransition::activated);
    CHECK(activated.blocked_reason == ActivationBlockReason::none);
    REQUIRE(activated.effect.has_value());
    check_effect(*activated.effect, successor, 0, 0);
    check_effect(replica.active_effect(), successor, 0, 0);
    CHECK(successor.activation_height() == 0);
    CHECK_FALSE(replica.active_status().has_value());
    CHECK(replica.admits_new_proposals());

    const auto replay = replica.on_v2_post_block_commit(
        activation_height, fixture.epoch0->epoch_digest());
    CHECK(replay.transition == ActivationTransition::already_active);
    REQUIRE(replay.effect.has_value());
    check_effect(*replay.effect, successor, 0, 0);
}

TEST_CASE("C08a schedules a later v2 epoch after completing the prior record",
          "[c08a][epoch-activation][adaptive-v2][record][multi-epoch]")
{
    constexpr std::uint64_t first_commit_height = 40;
    constexpr std::uint64_t first_delay = 5;
    constexpr std::uint64_t second_commit_height = 60;
    constexpr std::uint64_t second_delay = 4;
    EpochV2Fixture fixture;
    const auto &epoch1 = stage_v2_successor(fixture);
    ReplicaEpochActivation replica(fixture.store, *fixture.epoch0, 0);
    const auto epoch1_command = prevalidated_v2_command(
        *fixture.epoch0, epoch1.epoch_digest(), first_delay);

    REQUIRE(replica.record_committed_v2(
                epoch1_command, first_commit_height)
                .disposition == ActivationRecordDisposition::recorded);
    REQUIRE(replica.on_v2_post_block_commit(
                first_commit_height + first_delay,
                fixture.epoch0->epoch_digest())
                .transition == ActivationTransition::activated);
    check_effect(replica.active_effect(), epoch1, 0, 0);

    const auto old_duplicate = replica.record_committed_v2(
        epoch1_command, second_commit_height);
    CHECK(old_duplicate.disposition ==
          ActivationRecordDisposition::duplicate);
    REQUIRE(old_duplicate.record.has_value());
    CHECK(old_duplicate.record->command_commit_height == first_commit_height);

    const auto epoch2_staged = fixture.store.stage_available_v2(
        successor_v2_input(epoch1), epoch1);
    REQUIRE(epoch2_staged.disposition ==
            hotstuff::DefinitionAvailabilityDisposition::staged);
    REQUIRE(epoch2_staged.definition != nullptr);
    const auto &epoch2 = *epoch2_staged.definition;
    const auto epoch2_command = prevalidated_v2_command(
        epoch1, epoch2.epoch_digest(), second_delay);

    const auto recorded = replica.record_committed_v2(
        epoch2_command, second_commit_height);
    CHECK(recorded.disposition == ActivationRecordDisposition::recorded);
    REQUIRE(recorded.record.has_value());
    check_record(
        *recorded.record,
        epoch1,
        epoch2,
        epoch2_command,
        second_commit_height,
        second_commit_height + second_delay);
    check_effect(replica.active_effect(), epoch1, 0, 0);

    const auto completed_replay = replica.record_committed_v2(
        epoch1_command, second_commit_height + 1);
    CHECK(completed_replay.disposition ==
          ActivationRecordDisposition::duplicate);
    REQUIRE(completed_replay.record.has_value());
    CHECK(completed_replay.record->command_commit_height ==
          first_commit_height);
    REQUIRE(replica.committed_v2_record().has_value());
    CHECK(replica.committed_v2_record()->payload_digest ==
          hotstuff::epoch_change_payload_digest(epoch2_command.payload));
    CHECK(replica.committed_v2_record()->command_commit_height ==
          second_commit_height);
    CHECK(replica.admits_new_proposals());

    const auto early = replica.on_v2_post_block_commit(
        second_commit_height + second_delay - 1,
        epoch1.epoch_digest());
    CHECK(early.transition == ActivationTransition::waiting);
    check_effect(replica.active_effect(), epoch1, 0, 0);

    const auto activated = replica.on_v2_post_block_commit(
        second_commit_height + second_delay,
        epoch1.epoch_digest());
    CHECK(activated.transition == ActivationTransition::activated);
    REQUIRE(activated.effect.has_value());
    check_effect(*activated.effect, epoch2, 0, 0);
    check_effect(replica.active_effect(), epoch2, 0, 0);

}

TEST_CASE("C08a does not mask a permanent v2 block as already active",
          "[c08a][epoch-activation][adaptive-v2][fail-closed][completed]")
{
    constexpr std::uint64_t commit_height = 40;
    constexpr std::uint64_t delay = 5;
    EpochV2Fixture fixture;
    const auto &epoch1 = stage_v2_successor(fixture);
    ReplicaEpochActivation replica(fixture.store, *fixture.epoch0, 0);
    const auto epoch1_command = prevalidated_v2_command(
        *fixture.epoch0, epoch1.epoch_digest(), delay);

    REQUIRE(replica.record_committed_v2(epoch1_command, commit_height)
                .disposition == ActivationRecordDisposition::recorded);
    REQUIRE(replica.on_v2_post_block_commit(
                commit_height + delay,
                fixture.epoch0->epoch_digest())
                .transition == ActivationTransition::activated);
    check_effect(replica.active_effect(), epoch1, 0, 0);

    auto invalid_epoch2 = prevalidated_v2_command(
        epoch1, fixture_digest("invalid-epoch2"), delay);
    invalid_epoch2.payload.predecessor_epoch_digest =
        fixture_digest("wrong-current-predecessor");
    const auto rejected = replica.record_committed_v2(
        invalid_epoch2, commit_height + 10);
    CHECK(rejected.disposition ==
          ActivationRecordDisposition::wrong_predecessor);
    CHECK_FALSE(replica.admits_new_proposals());

    const auto preview = replica.preview_v2_post_block_commit(
        commit_height + delay, fixture.epoch0->epoch_digest());
    CHECK(preview.transition == ActivationTransition::blocked);
    CHECK(preview.blocked_reason ==
          ActivationBlockReason::predecessor_digest_mismatch);
    CHECK_FALSE(preview.effect.has_value());

    const auto applied = replica.on_v2_post_block_commit(
        commit_height + delay, fixture.epoch0->epoch_digest());
    CHECK(applied.transition == ActivationTransition::blocked);
    CHECK(applied.blocked_reason ==
          ActivationBlockReason::predecessor_digest_mismatch);
    CHECK_FALSE(applied.effect.has_value());
    check_effect(replica.active_effect(), epoch1, 0, 0);
}

TEST_CASE("C08a misses or mismatches the v2 boundary fail closed",
          "[c08a][epoch-activation][adaptive-v2][post-block][fail-closed]")
{
    constexpr std::uint64_t commit_height = 40;
    constexpr std::uint64_t activation_height = 45;

    SECTION("first post-block observation is late")
    {
        EpochV2Fixture fixture;
        const auto &successor = stage_v2_successor(fixture);
        ReplicaEpochActivation replica(fixture.store, *fixture.epoch0, 0);
        const auto command = prevalidated_v2_command(
            *fixture.epoch0, successor.epoch_digest(), 5);
        REQUIRE(replica.record_committed_v2(command, commit_height).disposition ==
                ActivationRecordDisposition::recorded);

        const auto preview = replica.preview_v2_post_block_commit(
            activation_height + 1, fixture.epoch0->epoch_digest());
        CHECK(preview.transition == ActivationTransition::blocked);
        CHECK(preview.blocked_reason ==
              ActivationBlockReason::missed_activation_height);
        CHECK(replica.admits_new_proposals());

        const auto missed = replica.on_v2_post_block_commit(
            activation_height + 1, fixture.epoch0->epoch_digest());
        CHECK(missed.transition == ActivationTransition::blocked);
        CHECK(missed.blocked_reason ==
              ActivationBlockReason::missed_activation_height);
        CHECK_FALSE(replica.admits_new_proposals());
        check_effect(replica.active_effect(), *fixture.epoch0, 0, 0);

        const auto backwards = replica.on_v2_post_block_commit(
            activation_height, fixture.epoch0->epoch_digest());
        CHECK(backwards.transition == ActivationTransition::blocked);
        CHECK(backwards.blocked_reason ==
              ActivationBlockReason::missed_activation_height);
    }

    SECTION("committed predecessor digest mismatches")
    {
        EpochV2Fixture fixture;
        const auto &successor = stage_v2_successor(fixture);
        ReplicaEpochActivation replica(fixture.store, *fixture.epoch0, 0);
        const auto command = prevalidated_v2_command(
            *fixture.epoch0, successor.epoch_digest(), 5);
        REQUIRE(replica.record_committed_v2(command, commit_height).disposition ==
                ActivationRecordDisposition::recorded);

        const auto mismatched = replica.on_v2_post_block_commit(
            activation_height - 1,
            fixture_digest("wrong-post-block-predecessor"));
        CHECK(mismatched.transition == ActivationTransition::blocked);
        CHECK(mismatched.blocked_reason ==
              ActivationBlockReason::predecessor_digest_mismatch);
        CHECK_FALSE(replica.admits_new_proposals());
        check_effect(replica.active_effect(), *fixture.epoch0, 0, 0);
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

namespace
{

using hotstuff::AdaptiveV3ActivationReadinessCertificate;
using hotstuff::AdaptiveV3ActivationReadinessLimits;
using hotstuff::AdaptiveV3ActivationReadyIdentity;
using hotstuff::AdaptiveV3ActivationReadyObservation;
using hotstuff::AdaptiveV3ActivationSchedule;
using hotstuff::AdaptiveV3BoundaryDisposition;
using hotstuff::AdaptiveV3CertificateDisposition;
using hotstuff::AdaptiveV3CertifiedActivationGate;
using hotstuff::AdaptiveV3CertifiedActivationState;
using hotstuff::AdaptiveV3ReadinessMember;
using hotstuff::ConfigurationId;
using hotstuff::PrivKeyBLS;
using hotstuff::PubKeyBLS;

hotstuff::bytearray_t cert13_private_key_bytes(ReplicaID replica)
{
    hotstuff::bytearray_t bytes(bls::PrivateKey::PRIVATE_KEY_SIZE, 0);
    const auto scalar = static_cast<std::uint32_t>(replica) + 1;
    bytes[bytes.size() - 4] =
        static_cast<std::uint8_t>(scalar >> 24);
    bytes[bytes.size() - 3] =
        static_cast<std::uint8_t>(scalar >> 16);
    bytes[bytes.size() - 2] =
        static_cast<std::uint8_t>(scalar >> 8);
    bytes[bytes.size() - 1] = static_cast<std::uint8_t>(scalar);
    return bytes;
}

struct Cert13N7Fixture
{
    static constexpr std::size_t replica_count = 7;
    static constexpr std::size_t quorum = 5;
    static constexpr std::uint64_t command_height = 1972;
    static constexpr std::uint64_t activation_delay = 5;
    static constexpr std::uint64_t activation_height =
        command_height + activation_delay;
    static constexpr std::uint64_t predecessor_generation = 0x100000004;
    static constexpr std::uint64_t successor_generation = 0x200000001;

    ConfigurationId predecessor{
        1, 3, fixture_digest("cert13-e1-digest")};
    ConfigurationId successor{
        2, 0, fixture_digest("cert13-e2-digest")};
    hotstuff::uint256_t boundary_hash{
        fixture_digest("cert13-e1-boundary-h1977")};
    AdaptiveV3ActivationSchedule schedule;
    AdaptiveV3ActivationReadinessLimits limits{32 * 1024, replica_count};
    std::vector<std::shared_ptr<const PrivKeyBLS>> private_keys;
    std::vector<AdaptiveV3ReadinessMember> members;

    Cert13N7Fixture()
    {
        schedule.membership_digest =
            hotstuff::canonical_membership_digest(membership7());
        schedule.predecessor_epoch_number = predecessor.epoch_number;
        schedule.predecessor_epoch_digest = predecessor.epoch_digest;
        schedule.successor_epoch_number = successor.epoch_number;
        schedule.successor_epoch_digest = successor.epoch_digest;
        schedule.successor_activation_generation = successor_generation;
        schedule.command_payload_digest =
            fixture_digest("cert13-e1-e2-command-payload");
        schedule.command_block_height = command_height;
        schedule.command_block_hash =
            fixture_digest("cert13-e1-e2-command-block");
        schedule.activation_delay_blocks = activation_delay;
        schedule.activation_height = activation_height;

        private_keys.reserve(replica_count);
        members.reserve(replica_count);
        for (ReplicaID replica = 0; replica < replica_count; ++replica)
        {
            auto key = std::make_shared<const PrivKeyBLS>(
                cert13_private_key_bytes(replica));
            members.push_back(
                AdaptiveV3ReadinessMember{
                    replica, PubKeyBLS(*key)});
            private_keys.push_back(std::move(key));
        }
    }

    std::unique_ptr<AdaptiveV3CertifiedActivationGate> gate(
        ReplicaID replica) const
    {
        return std::make_unique<AdaptiveV3CertifiedActivationGate>(
            schedule,
            replica,
            private_keys.at(replica),
            members,
            quorum);
    }

    AdaptiveV3ActivationReadyObservation prepare(
        AdaptiveV3CertifiedActivationGate &gate,
        ReplicaID replica,
        std::uint64_t source_sequence = 1,
        std::uint64_t monotonic_raw_ns = 1'000'000) const
    {
        const auto result = gate.observe_predecessor_commit(
            activation_height,
            predecessor,
            predecessor_generation,
            boundary_hash,
            source_sequence,
            monotonic_raw_ns + replica);
        REQUIRE(result.disposition ==
                AdaptiveV3BoundaryDisposition::prepared);
        REQUIRE(result.observation.has_value());
        CHECK(result.observation->signer_replica_id == replica);
        CHECK(result.observation->vote_fence_engaged);
        CHECK(gate.vote_fence_engaged());
        CHECK(gate.state() == AdaptiveV3CertifiedActivationState::prepared);
        CHECK(gate.active_configuration() == predecessor);
        CHECK_FALSE(gate.may_authorize_vote(
            predecessor, predecessor_generation));
        CHECK_FALSE(gate.may_authorize_vote(
            successor, successor_generation));
        return *result.observation;
    }

    AdaptiveV3ActivationReadinessCertificate certificate(
        std::vector<AdaptiveV3ActivationReadyObservation> observations) const
    {
        REQUIRE_FALSE(observations.empty());
        return hotstuff::make_adaptive_v3_activation_readiness_certificate(
            observations.front().identity,
            std::move(observations),
            limits);
    }
};

std::vector<AdaptiveV3ActivationReadyObservation> cert13_prefix(
    const std::vector<AdaptiveV3ActivationReadyObservation> &observations,
    std::size_t count)
{
    REQUIRE(count <= observations.size());
    return {
        observations.begin(),
        observations.begin() + static_cast<std::ptrdiff_t>(count)};
}

void encode_cert13_configuration_unchecked(
    hotstuff::detail::CanonicalWireWriter &writer,
    const ConfigurationId &configuration)
{
    writer.integer(configuration.epoch_number);
    writer.integer(configuration.tree_id);
    writer.digest(
        configuration.epoch_digest,
        "CERT13 test configuration digest is not 32 bytes");
}

void encode_cert13_identity_unchecked(
    hotstuff::detail::CanonicalWireWriter &writer,
    const AdaptiveV3ActivationReadyIdentity &identity)
{
    writer.integer(identity.schema_version);
    writer.digest(
        identity.membership_digest,
        "CERT13 test membership digest is not 32 bytes");
    encode_cert13_configuration_unchecked(
        writer, identity.predecessor_boundary_configuration);
    writer.integer(identity.predecessor_boundary_generation);
    encode_cert13_configuration_unchecked(
        writer, identity.successor_configuration);
    writer.integer(identity.successor_activation_generation);
    writer.digest(
        identity.command_payload_digest,
        "CERT13 test command digest is not 32 bytes");
    writer.integer(identity.command_block_height);
    writer.digest(
        identity.command_block_hash,
        "CERT13 test command block hash is not 32 bytes");
    writer.integer(identity.activation_delay_blocks);
    writer.integer(identity.activation_height);
    writer.digest(
        identity.activation_boundary_block_hash,
        "CERT13 test boundary hash is not 32 bytes");
}

hotstuff::uint256_t cert13_observation_digest_unchecked(
    const AdaptiveV3ActivationReadyIdentity &identity,
    ReplicaID signer,
    std::uint64_t source_sequence,
    std::uint64_t monotonic_raw_ns)
{
    hotstuff::detail::CanonicalWireWriter writer;
    writer.domain(
        "kauri-adaptive-v3-activation-ready-observation-v1");
    encode_cert13_identity_unchecked(writer, identity);
    writer.integer(signer);
    writer.integer(source_sequence);
    writer.integer(monotonic_raw_ns);
    writer.integer(static_cast<std::uint8_t>(1));
    return hotstuff::DataStream(std::move(writer).finish()).get_hash();
}

void encode_cert13_observation_unchecked(
    hotstuff::detail::CanonicalWireWriter &writer,
    const AdaptiveV3ActivationReadyObservation &observation)
{
    encode_cert13_identity_unchecked(writer, observation.identity);
    writer.integer(observation.signer_replica_id);
    writer.integer(observation.signer_source_sequence);
    writer.integer(observation.signer_monotonic_raw_ns);
    writer.integer(static_cast<std::uint8_t>(
        observation.vote_fence_engaged ? 1 : 0));
    writer.bytes(observation.signature.to_bytes());
}

hotstuff::uint256_t cert13_certificate_digest_unchecked(
    const AdaptiveV3ActivationReadinessCertificate &certificate)
{
    hotstuff::detail::CanonicalWireWriter writer;
    writer.domain(
        "kauri-adaptive-v3-activation-readiness-certificate-digest-v1");
    writer.integer(certificate.schema_version);
    encode_cert13_identity_unchecked(writer, certificate.identity);
    writer.integer(static_cast<std::uint32_t>(
        certificate.observations.size()));
    for (const auto &observation : certificate.observations)
        encode_cert13_observation_unchecked(writer, observation);
    return hotstuff::DataStream(std::move(writer).finish()).get_hash();
}

AdaptiveV3ActivationReadinessCertificate
cert13_resign_noncanonical_generation(
    const Cert13N7Fixture &fixture,
    AdaptiveV3ActivationReadyIdentity identity)
{
    std::vector<AdaptiveV3ActivationReadyObservation> observations;
    observations.reserve(Cert13N7Fixture::quorum);
    for (ReplicaID signer = 0;
         signer < Cert13N7Fixture::quorum; ++signer)
    {
        const auto sequence = static_cast<std::uint64_t>(signer) + 1;
        const auto clock =
            std::uint64_t{7'000'000} + static_cast<std::uint64_t>(signer);
        const auto digest = cert13_observation_digest_unchecked(
            identity, signer, sequence, clock);
        observations.push_back(AdaptiveV3ActivationReadyObservation{
            identity,
            signer,
            sequence,
            clock,
            true,
            hotstuff::SigSecBLS(digest, *fixture.private_keys.at(signer))});
        CHECK(observations.back().signature.verify(
            digest, fixture.members.at(signer).public_key));
    }

    AdaptiveV3ActivationReadinessCertificate certificate;
    certificate.identity = std::move(identity);
    certificate.observations = std::move(observations);
    certificate.certificate_digest =
        cert13_certificate_digest_unchecked(certificate);
    return certificate;
}

} // namespace

TEST_CASE(
    "CERT13 readiness wire is canonical signed and isolated from adaptive v2",
    "[cert13][adaptive-v3][readiness][wire][unit][intentional-red]")
{
    CHECK(KAURI_HAS_CERT13_ACTIVATION_API == 1);
    Cert13N7Fixture fixture;
    auto replica = fixture.gate(2);
    const auto observation = fixture.prepare(*replica, 2);

    const auto observation_bytes =
        hotstuff::encode_adaptive_v3_activation_ready_observation(
            observation, fixture.limits);
    const auto decoded_observation =
        hotstuff::decode_adaptive_v3_activation_ready_observation(
            observation_bytes, fixture.limits);
    REQUIRE(decoded_observation);
    REQUIRE(decoded_observation.value.has_value());
    CHECK(decoded_observation.value->identity == observation.identity);
    CHECK(decoded_observation.value->signer_replica_id == 2);
    CHECK(decoded_observation.value->vote_fence_engaged);
    CHECK(observation_bytes ==
          hotstuff::encode_adaptive_v3_activation_ready_observation(
              *decoded_observation.value, fixture.limits));

    CHECK(hotstuff::adaptive_v3_activation_ready_observation_domain() !=
          hotstuff::adaptive_v3_activation_readiness_certificate_domain());
    CHECK(hotstuff::adaptive_v3_activation_ready_observation_domain() !=
          hotstuff::adaptive_v2_epoch_activated_observation_domain());

    std::vector<std::unique_ptr<AdaptiveV3CertifiedActivationGate>> gates;
    std::vector<AdaptiveV3ActivationReadyObservation> observations;
    for (ReplicaID source = 2; source <= 6; ++source)
    {
        gates.push_back(fixture.gate(source));
        observations.push_back(fixture.prepare(
            *gates.back(), source, source + 1, 2'000'000));
    }
    const auto certificate = fixture.certificate(observations);
    const auto certificate_bytes =
        hotstuff::encode_adaptive_v3_activation_readiness_certificate(
            certificate, fixture.limits);
    const auto decoded_certificate =
        hotstuff::decode_adaptive_v3_activation_readiness_certificate(
            certificate_bytes, fixture.limits);
    REQUIRE(decoded_certificate);
    REQUIRE(decoded_certificate.value.has_value());
    CHECK(decoded_certificate.value->identity == certificate.identity);
    CHECK(decoded_certificate.value->observations.size() ==
          Cert13N7Fixture::quorum);
    CHECK(certificate_bytes ==
          hotstuff::encode_adaptive_v3_activation_readiness_certificate(
              *decoded_certificate.value, fixture.limits));

    auto reordered = certificate;
    std::vector<AdaptiveV3ActivationReadyObservation> reverse_order;
    for (auto iterator = observations.rbegin();
         iterator != observations.rend(); ++iterator)
        reverse_order.push_back(*iterator);
    reordered.observations = std::move(reverse_order);
    CHECK_THROWS(
        hotstuff::encode_adaptive_v3_activation_readiness_certificate(
            reordered, fixture.limits));

    auto duplicate = certificate;
    duplicate.observations.clear();
    duplicate.observations.push_back(observations[0]);
    duplicate.observations.push_back(observations[1]);
    duplicate.observations.push_back(observations[2]);
    duplicate.observations.push_back(observations[3]);
    duplicate.observations.push_back(observations[3]);
    CHECK_THROWS(
        hotstuff::encode_adaptive_v3_activation_readiness_certificate(
            duplicate, fixture.limits));

    auto oversized_limits = fixture.limits;
    oversized_limits.maximum_members = 4;
    const auto oversized =
        hotstuff::decode_adaptive_v3_activation_readiness_certificate(
            certificate_bytes, oversized_limits);
    CHECK_FALSE(oversized);
    CHECK(oversized.error ==
          hotstuff::AdaptiveV3ActivationReadinessWireError::too_many_members);
}

TEST_CASE(
    "CERT13 N7 Q5 two-crash transition prepares all survivors before activation",
    "[cert13][adaptive-v3][n7][q5][two-crash][activation-skew]"
    "[integration][intentional-red]")
{
    Cert13N7Fixture fixture;
    const std::vector<ReplicaID> crashed{0, 1};
    const std::vector<ReplicaID> survivors{2, 3, 4, 5, 6};
    REQUIRE(crashed.size() == 2);
    REQUIRE(survivors.size() == Cert13N7Fixture::quorum);

    std::vector<std::unique_ptr<AdaptiveV3CertifiedActivationGate>> gates;
    std::vector<AdaptiveV3ActivationReadyObservation> observations;
    for (const auto survivor : survivors)
    {
        gates.push_back(fixture.gate(survivor));
        auto &gate = *gates.back();

        const auto early = gate.observe_predecessor_commit(
            Cert13N7Fixture::activation_height - 1,
            fixture.predecessor,
            Cert13N7Fixture::predecessor_generation,
            fixture_digest("cert13-pre-boundary"),
            1,
            900'000 + survivor);
        CHECK(early.disposition == AdaptiveV3BoundaryDisposition::waiting);
        CHECK_FALSE(early.observation.has_value());
        CHECK(gate.active_configuration() == fixture.predecessor);
        CHECK(gate.may_authorize_vote(
            fixture.predecessor,
            Cert13N7Fixture::predecessor_generation));
        CHECK_FALSE(gate.may_authorize_vote(
            fixture.successor,
            Cert13N7Fixture::successor_generation));

        observations.push_back(fixture.prepare(
            gate, survivor, 2, 1'000'000));
        CHECK(gate.active_configuration() == fixture.predecessor);
        CHECK_FALSE(gate.certificate_apply_committed_height().has_value());
    }

    const auto below_quorum = fixture.certificate(
        cert13_prefix(observations, Cert13N7Fixture::quorum - 1));
    for (auto &gate : gates)
    {
        CHECK(gate->observe_certificate(below_quorum) ==
              AdaptiveV3CertificateDisposition::rejected_below_quorum);
        CHECK(gate->state() == AdaptiveV3CertifiedActivationState::prepared);
        CHECK(gate->active_configuration() == fixture.predecessor);
    }

    auto stale = fixture.certificate(observations);
    stale.identity.predecessor_boundary_configuration.epoch_number = 0;
    for (auto &observation : stale.observations)
        observation.identity = stale.identity;
    CHECK(gates.front()->observe_certificate(stale) ==
          AdaptiveV3CertificateDisposition::rejected_stale);

    auto mixed = fixture.certificate(observations);
    mixed.observations.back().identity.successor_configuration.epoch_digest =
        fixture_digest("cert13-mixed-successor");
    CHECK(gates.front()->observe_certificate(mixed) ==
          AdaptiveV3CertificateDisposition::rejected_mixed_identity);

    auto wrong_command = fixture.certificate(observations);
    wrong_command.identity.command_block_hash =
        fixture_digest("cert13-wrong-command-block");
    for (auto &observation : wrong_command.observations)
        observation.identity = wrong_command.identity;
    CHECK(gates.front()->observe_certificate(wrong_command) ==
          AdaptiveV3CertificateDisposition::rejected_wrong_identity);

    auto bad_signature_observations = observations;
    ++bad_signature_observations.back().signer_source_sequence;
    const auto bad_signature = fixture.certificate(
        std::move(bad_signature_observations));
    CHECK(gates.front()->observe_certificate(bad_signature) ==
          AdaptiveV3CertificateDisposition::rejected_invalid_signature);

    for (std::size_t index = 0; index < gates.size(); ++index)
    {
        const auto later = gates[index]->observe_predecessor_commit(
            Cert13N7Fixture::activation_height + 2,
            fixture.predecessor,
            Cert13N7Fixture::predecessor_generation,
            fixture_digest("cert13-later-predecessor-commit"),
            3,
            3'000'000 + survivors[index]);
        CHECK(later.disposition ==
              AdaptiveV3BoundaryDisposition::already_prepared);
        CHECK_FALSE(later.observation.has_value());
        CHECK(gates[index]->active_configuration() == fixture.predecessor);
    }

    const auto valid = fixture.certificate(observations);
    std::size_t activated = 0;
    for (auto &gate : gates)
    {
        CHECK(gate->observe_certificate(valid) ==
              AdaptiveV3CertificateDisposition::accepted);
        ++activated;
        CHECK(gate->state() == AdaptiveV3CertifiedActivationState::active);
        CHECK(gate->active_configuration() == fixture.successor);
        CHECK_FALSE(gate->may_authorize_vote(
            fixture.predecessor,
            Cert13N7Fixture::predecessor_generation));
        CHECK(gate->may_authorize_vote(
            fixture.successor,
            Cert13N7Fixture::successor_generation));
        REQUIRE(gate->certificate_apply_committed_height().has_value());
        CHECK(*gate->certificate_apply_committed_height() ==
              Cert13N7Fixture::activation_height + 2);

        for (const auto &candidate : gates)
            CHECK_FALSE(candidate->may_authorize_vote(
                fixture.predecessor,
                Cert13N7Fixture::predecessor_generation));
    }
    CHECK(activated == survivors.size());
}

TEST_CASE(
    "CERT13 early certificate is inert until the local vote fence is installed",
    "[cert13][adaptive-v3][certificate][early][vote-fence]"
    "[unit][intentional-red]")
{
    Cert13N7Fixture fixture;
    std::vector<std::unique_ptr<AdaptiveV3CertifiedActivationGate>> signers;
    std::vector<AdaptiveV3ActivationReadyObservation> observations;
    for (ReplicaID signer = 0; signer < Cert13N7Fixture::quorum; ++signer)
    {
        signers.push_back(fixture.gate(signer));
        observations.push_back(fixture.prepare(
            *signers.back(), signer, 1, 4'000'000));
    }
    const auto valid = fixture.certificate(observations);

    auto late_replica = fixture.gate(6);
    CHECK(late_replica->observe_certificate(valid) ==
          AdaptiveV3CertificateDisposition::buffered_early);
    CHECK(late_replica->state() ==
          AdaptiveV3CertifiedActivationState::awaiting_boundary);
    CHECK(late_replica->active_configuration() == fixture.predecessor);
    CHECK_FALSE(late_replica->vote_fence_engaged());
    CHECK(late_replica->may_authorize_vote(
        fixture.predecessor,
        Cert13N7Fixture::predecessor_generation));
    CHECK_FALSE(late_replica->may_authorize_vote(
        fixture.successor,
        Cert13N7Fixture::successor_generation));

    const auto boundary = late_replica->observe_predecessor_commit(
        Cert13N7Fixture::activation_height,
        fixture.predecessor,
        Cert13N7Fixture::predecessor_generation,
        fixture.boundary_hash,
        2,
        5'000'006);
    CHECK(boundary.disposition ==
          AdaptiveV3BoundaryDisposition::activated_from_buffered_certificate);
    REQUIRE(boundary.observation.has_value());
    CHECK(boundary.observation->vote_fence_engaged);
    CHECK(late_replica->vote_fence_engaged());
    CHECK(late_replica->active_configuration() == fixture.successor);
    CHECK_FALSE(late_replica->may_authorize_vote(
        fixture.predecessor,
        Cert13N7Fixture::predecessor_generation));
    CHECK(late_replica->may_authorize_vote(
        fixture.successor,
        Cert13N7Fixture::successor_generation));
}

TEST_CASE(
    "CERT13 N7 rejects f false identities then accepts and deduplicates E1",
    "[cert13][adaptive-v3][n7][false-identity][duplicate][unit]")
{
    Cert13N7Fixture fixture;
    auto replica = fixture.gate(6);
    const auto local_observation = fixture.prepare(*replica, 6);

    for (std::uint32_t false_identity = 0;
         false_identity < 2; ++false_identity)
    {
        auto identity = local_observation.identity;
        identity.command_block_hash = fixture_digest(
            "cert13-false-identity-" +
            std::to_string(false_identity));
        const auto certificate = cert13_resign_noncanonical_generation(
            fixture, std::move(identity));
        CHECK(replica->observe_certificate(certificate) ==
              AdaptiveV3CertificateDisposition::rejected_wrong_identity);
        CHECK(replica->state() ==
              AdaptiveV3CertifiedActivationState::prepared);
        CHECK(replica->active_configuration() == fixture.predecessor);
        CHECK_FALSE(replica->certificate_apply_committed_height().has_value());
    }

    std::vector<AdaptiveV3ActivationReadyObservation> observations;
    for (ReplicaID signer = 0;
         signer < Cert13N7Fixture::quorum; ++signer)
    {
        auto signing_gate = fixture.gate(signer);
        observations.push_back(fixture.prepare(
            *signing_gate, signer, signer + 1, 8'000'000));
    }
    const auto valid = fixture.certificate(std::move(observations));
    CHECK(replica->observe_certificate(valid) ==
          AdaptiveV3CertificateDisposition::accepted);
    REQUIRE(replica->certificate_apply_committed_height().has_value());
    const auto applied_height =
        *replica->certificate_apply_committed_height();
    CHECK(replica->observe_certificate(valid) ==
          AdaptiveV3CertificateDisposition::duplicate);
    CHECK(replica->state() == AdaptiveV3CertifiedActivationState::active);
    CHECK(replica->active_configuration() == fixture.successor);
    CHECK(*replica->certificate_apply_committed_height() == applied_height);

    auto stale_identity = local_observation.identity;
    --stale_identity.predecessor_boundary_configuration.epoch_number;
    --stale_identity.successor_configuration.epoch_number;
    const auto stale = cert13_resign_noncanonical_generation(
        fixture, std::move(stale_identity));
    CHECK(replica->observe_certificate(stale) ==
          AdaptiveV3CertificateDisposition::rejected_stale);
    CHECK(replica->active_configuration() == fixture.successor);
    CHECK(*replica->certificate_apply_committed_height() == applied_height);
}

TEST_CASE(
    "CERT13 replica activation rejects a re-signed wrong-epoch predecessor generation",
    "[cert13][adaptive-v3][activation][generation][fail-closed][unit]")
{
    EpochV2Fixture epochs;
    const auto &predecessor = stage_v2_successor(epochs);
    const auto staged_successor = epochs.store.stage_available_v2(
        successor_v2_input(predecessor), predecessor);
    REQUIRE(staged_successor.disposition ==
            hotstuff::DefinitionAvailabilityDisposition::staged);
    REQUIRE(staged_successor.definition != nullptr);
    const auto &successor = *staged_successor.definition;

    constexpr std::uint32_t current_rotation_ordinal = 3;
    const auto canonical_predecessor_generation =
        hotstuff::checked_activation_generation(
            predecessor.epoch_number(), current_rotation_ordinal);
    const auto wrong_epoch_generation =
        hotstuff::checked_activation_generation(
            successor.epoch_number(), current_rotation_ordinal);
    const auto successor_generation =
        hotstuff::checked_activation_generation(
            successor.epoch_number(), 0);
    REQUIRE(canonical_predecessor_generation.has_value());
    REQUIRE(wrong_epoch_generation.has_value());
    REQUIRE(successor_generation.has_value());
    REQUIRE(*wrong_epoch_generation != *canonical_predecessor_generation);

    Cert13N7Fixture readiness;
    auto signer = readiness.gate(0);
    auto prepared = readiness.prepare(*signer, 0);
    auto identity = prepared.identity;
    identity.predecessor_boundary_configuration = {
        predecessor.epoch_number(), 0, predecessor.epoch_digest()};
    identity.predecessor_boundary_generation = *wrong_epoch_generation;
    identity.successor_configuration = {
        successor.epoch_number(), 0, successor.epoch_digest()};
    identity.successor_activation_generation = *successor_generation;
    const auto certificate = cert13_resign_noncanonical_generation(
        readiness, std::move(identity));
    REQUIRE(certificate.observations.size() == Cert13N7Fixture::quorum);
    CHECK(certificate.identity.predecessor_boundary_generation ==
          *wrong_epoch_generation);
    CHECK(certificate.certificate_digest ==
          cert13_certificate_digest_unchecked(certificate));
    for (const auto &observation : certificate.observations)
    {
        CHECK(observation.identity == certificate.identity);
    }
    CHECK_THROWS(
        hotstuff::encode_adaptive_v3_activation_readiness_certificate(
            certificate, readiness.limits));

    ReplicaEpochActivation preview_replica(
        epochs.store, predecessor, 0, 0, current_rotation_ordinal);
    const auto preview_before = preview_replica.active_effect();
    const auto preview = preview_replica.preview_v3_certified_activation(
        certificate.identity.predecessor_boundary_configuration,
        certificate.identity.predecessor_boundary_generation,
        certificate.identity.successor_configuration,
        certificate.identity.successor_activation_generation);
    CHECK(preview.transition == ActivationTransition::blocked);
    CHECK(preview.blocked_reason ==
          ActivationBlockReason::conflicting_activation_record);
    check_effect(
        preview_replica.active_effect(),
        predecessor,
        preview_before.configuration.tree_id,
        preview_before.rotation_ordinal);
    CHECK(preview_replica.blocked_reason() == ActivationBlockReason::none);
    CHECK(preview_replica.admits_new_proposals());

    ReplicaEpochActivation apply_replica(
        epochs.store, predecessor, 0, 0, current_rotation_ordinal);
    const auto apply_before = apply_replica.active_effect();
    const auto applied = apply_replica.apply_v3_certified_activation(
        certificate.identity.predecessor_boundary_configuration,
        certificate.identity.predecessor_boundary_generation,
        certificate.identity.successor_configuration,
        certificate.identity.successor_activation_generation);
    CHECK(applied.transition == ActivationTransition::blocked);
    CHECK(applied.blocked_reason ==
          ActivationBlockReason::conflicting_activation_record);
    check_effect(
        apply_replica.active_effect(),
        predecessor,
        apply_before.configuration.tree_id,
        apply_before.rotation_ordinal);
    CHECK(apply_replica.blocked_reason() ==
          ActivationBlockReason::conflicting_activation_record);
    CHECK_FALSE(apply_replica.admits_new_proposals());
}

TEST_CASE(
    "CERT13 leaves archived adaptive-v2 height activation unchanged",
    "[cert13][archive][adaptive-v2][unchanged][unit]")
{
    constexpr std::uint64_t command_height = 40;
    constexpr std::uint64_t activation_height = 45;
    EpochV2Fixture fixture;
    const auto &successor = stage_v2_successor(fixture);
    ReplicaEpochActivation replica(fixture.store, *fixture.epoch0, 0);
    const auto command = prevalidated_v2_command(
        *fixture.epoch0, successor.epoch_digest(), 5);

    REQUIRE(replica.record_committed_v2(command, command_height).disposition ==
            ActivationRecordDisposition::recorded);
    const auto activated = replica.on_v2_post_block_commit(
        activation_height, fixture.epoch0->epoch_digest());
    REQUIRE(activated.transition == ActivationTransition::activated);
    REQUIRE(activated.effect.has_value());
    check_effect(*activated.effect, successor, 0, 0);
    check_effect(replica.active_effect(), successor, 0, 0);
    CHECK(replica.admits_new_proposals());
}
