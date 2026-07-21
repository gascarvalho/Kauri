/**
 * Bounded, transport-independent coordination for adaptive containment epochs.
 */

#ifndef HOTSTUFF_ADAPTATION_MANAGER_H_INCLUDED
#define HOTSTUFF_ADAPTATION_MANAGER_H_INCLUDED

#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <vector>

#include "hotstuff/adaptation.h"
#include "hotstuff/adaptive_v2_manager_session.h"
#include "hotstuff/epoch_activation.h"
#include "hotstuff/tree_policy.h"

namespace hotstuff
{

constexpr std::uint32_t kAdaptationManagerSchemaVersion = 1;
constexpr std::size_t kAdaptationCertificateFingerprintBytes = 32;

using AdaptationCertificateFingerprint =
    std::array<std::uint8_t, kAdaptationCertificateFingerprintBytes>;

enum class AdaptationIdentityRole : std::uint8_t
{
    manager = 1,
    replica = 2,
};

struct PinnedAdaptationIdentity
{
    ReplicaID replica_id{0};
    AdaptationIdentityRole role{AdaptationIdentityRole::replica};
    AdaptationCertificateFingerprint certificate_fingerprint;
};

enum class AdaptationManagerState : std::uint8_t
{
    waiting_for_readiness = 1,
    collecting_baseline_evidence,
    containment_generated,
    containment_armed,
    waiting_for_containment_activation,
    containment_converged,
};

enum class AdaptationManagerRecordDisposition : std::uint8_t
{
    accepted = 1,
    duplicate,
    unknown_identity,
    wrong_role,
    payload_identity_mismatch,
    wrong_configuration,
    wrong_generation,
    wrong_state,
    incomplete_evidence,
    mixed_evidence_configuration,
    stale_evidence_cutoff,
    acknowledgement_not_required,
};

struct AdaptationManagerRecordResult
{
    AdaptationManagerRecordDisposition disposition{
        AdaptationManagerRecordDisposition::accepted};
    AdaptationManagerState state{
        AdaptationManagerState::waiting_for_readiness};
};

struct AdaptationManagerDecisionView
{
    const AdaptationSnapshot *snapshot{nullptr};
    const std::vector<ReplicaID> *responsive_replicas{nullptr};
    const TreePlacementResult *placement{nullptr};
    const StageEpochDefinition *stage{nullptr};
};

/**
 * Single-writer phase-one manager state.
 *
 * The caller supplies already authenticated certificate fingerprints. The
 * coordinator compares them with immutable pins and never receives process
 * identifiers, crash targets, or performance-profile labels.
 */
class AdaptationManagerCoordinator final
{
public:
    AdaptationManagerCoordinator(
        const EpochDefinition &current_epoch,
        ConfigurationId current_configuration,
        std::uint64_t current_activation_generation,
        std::vector<PinnedAdaptationIdentity> pinned_identities,
        AdaptationPolicy adaptation_policy,
        TreePlacementInput placement_input,
        FaultContainmentPolicy containment_policy,
        std::uint32_t tolerated_faults,
        std::uint64_t activation_height,
        std::uint64_t minimum_activation_grace);
    ~AdaptationManagerCoordinator();

    AdaptationManagerCoordinator(
        const AdaptationManagerCoordinator &) = delete;
    AdaptationManagerCoordinator &operator=(
        const AdaptationManagerCoordinator &) = delete;
    AdaptationManagerCoordinator(
        AdaptationManagerCoordinator &&) = delete;
    AdaptationManagerCoordinator &operator=(
        AdaptationManagerCoordinator &&) = delete;

    AdaptationManagerRecordResult record_authenticated_readiness(
        const AdaptationCertificateFingerprint &certificate_fingerprint,
        ReplicaID payload_replica_id,
        const ConfigurationId &active_configuration,
        std::uint64_t activation_generation,
        std::uint64_t committed_height) noexcept;

    AdaptationManagerRecordResult freeze_baseline_evidence(
        AcceptedEvidenceView accepted_evidence,
        std::uint64_t evidence_cutoff);

    std::optional<AdaptationManagerDecisionView>
    containment_decision() const noexcept;

    AdaptationManagerRecordResult record_authenticated_stage_ack(
        const AdaptationCertificateFingerprint &certificate_fingerprint,
        const StageAck &acknowledgement) noexcept;

    std::optional<ArmActivation> arm_activation() const;

    AdaptationManagerRecordResult record_authenticated_activation(
        const AdaptationCertificateFingerprint &certificate_fingerprint,
        const ActivationStatus &status) noexcept;

    AdaptationManagerState state() const noexcept;
    bool converged() const noexcept;

private:
    struct State;
    std::unique_ptr<State> state_;
};

/**
 * Ordered, transport-independent transition-request cursor.
 *
 * Requests are copied into immutable owned storage. The cursor consumes only
 * newly appended session terminal records whose explicit policy intent
 * matches the current request. Epoch numbers and cycle ordinals are not part
 * of this sequencing decision, and the sequencer has no consensus or
 * activation authority.
 */
class AdaptiveV2ManagerRequestSequence final
{
public:
    explicit AdaptiveV2ManagerRequestSequence(
        std::vector<AdaptiveV2TransitionPolicy> requests);
    ~AdaptiveV2ManagerRequestSequence();

    AdaptiveV2ManagerRequestSequence(
        const AdaptiveV2ManagerRequestSequence &) = delete;
    AdaptiveV2ManagerRequestSequence &operator=(
        const AdaptiveV2ManagerRequestSequence &) = delete;
    AdaptiveV2ManagerRequestSequence(
        AdaptiveV2ManagerRequestSequence &&) = delete;
    AdaptiveV2ManagerRequestSequence &operator=(
        AdaptiveV2ManagerRequestSequence &&) = delete;

    std::size_t cursor() const noexcept;
    const AdaptiveV2TransitionPolicy *current_policy() const noexcept;

    /**
     * Consume at most one previously unseen append-only terminal record.
     *
     * A matching `advanced` record advances the request cursor. Matching
     * `no_op` and `failed` records are observed without advancing it. A
     * replay, malformed outcome, or policy-intent mismatch is inert.
     */
    bool observe_terminal_records(
        const std::vector<AdaptiveV2ManagerSessionTerminalRecord> &records)
        noexcept;

    bool shutdown_eligible() const noexcept;

private:
    struct State;
    std::unique_ptr<State> state_;
};

} // namespace hotstuff

#endif
