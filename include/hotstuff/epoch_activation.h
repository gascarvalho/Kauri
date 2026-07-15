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
