/**
 * Pure state for adaptive epoch staging and commit-correlated activation.
 *
 * Phase one is externally serialized by its caller, is not batch-transactional
 * across replicas, and does not install message handlers.
 * A transition is made only from an exact commit proof; live transport,
 * proposal admission, and consensus integration remain outside this layer.
 */

#ifndef HOTSTUFF_EPOCH_ACTIVATION_H_INCLUDED
#define HOTSTUFF_EPOCH_ACTIVATION_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <vector>

#include "hotstuff/epoch_change.h"
#include "hotstuff/epoch_store.h"
#include "hotstuff/epoch_wire.h"
#include "hotstuff/evidence.h"

namespace hotstuff
{

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
    invalid_activation_record,
    conflicting_activation_record,
    activation_height_overflow,
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

/**
 * Immutable schedule derived from one prevalidated adaptive-v2 command after
 * the command itself commits.  The successor definition remains schedule-free;
 * activation_height is checked and derived only as h_c + Delta.
 */
struct ActivationRecord
{
    const std::uint32_t predecessor_epoch_number;
    const uint256_t predecessor_epoch_digest;
    const std::uint32_t successor_epoch_number;
    const uint256_t successor_epoch_digest;
    const uint256_t payload_digest;
    const std::uint64_t command_commit_height;
    const std::uint64_t activation_delay_blocks;
    const std::uint64_t activation_height;

    bool operator==(const ActivationRecord &other) const noexcept;
    bool operator!=(const ActivationRecord &other) const noexcept;
};

enum class ActivationRecordDisposition : std::uint8_t
{
    recorded = 1,
    duplicate,
    unsupported_schema,
    wrong_mode,
    wrong_predecessor,
    wrong_successor,
    missing_definition,
    mismatched_definition,
    activation_height_overflow,
    conflicting_record,
};

struct ActivationRecordResult
{
    const ActivationRecordDisposition disposition;
    const std::optional<ActivationRecord> record;
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

    ActivationRecordResult record_committed_v2(
        const AuthorizedEpochChange &prevalidated_command,
        std::uint64_t command_commit_height);
    void fail_committed_v2(ActivationBlockReason reason) noexcept;
    std::optional<ActivationRecord> committed_v2_record() const;
    EpochActivationResult preview_v2_post_block_commit(
        std::uint64_t height,
        const uint256_t &predecessor_digest) const;
    EpochActivationResult on_v2_post_block_commit(
        std::uint64_t height,
        const uint256_t &predecessor_digest);

    /**
     * Adaptive-v3 applies only after the separate certified-activation gate
     * has authenticated the exact readiness certificate.  These methods do
     * not inspect evidence; they atomically validate and publish the exact
     * predecessor-to-successor runtime identity selected by that gate.
     */
    EpochActivationResult preview_v3_certified_activation(
        const ConfigurationId &predecessor_configuration,
        std::uint64_t predecessor_generation,
        const ConfigurationId &successor_configuration,
        std::uint64_t successor_generation) const;
    EpochActivationResult apply_v3_certified_activation(
        const ConfigurationId &predecessor_configuration,
        std::uint64_t predecessor_generation,
        const ConfigurationId &successor_configuration,
        std::uint64_t successor_generation);

    EpochActivationResult on_predecessor_commit(
        std::uint64_t height,
        const uint256_t &digest);
    EpochActivationResult replay_blocked_commit();
    EpochActivationResult preview_predecessor_commit(
        std::uint64_t height,
        const uint256_t &digest) const;
    EpochActivationResult preview_blocked_commit() const;
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
