/**
 * Transport-independent adaptive-v2 manager ingress ownership boundary.
 */

#ifndef HOTSTUFF_ADAPTIVE_V2_MANAGER_INGRESS_H_INCLUDED
#define HOTSTUFF_ADAPTIVE_V2_MANAGER_INGRESS_H_INCLUDED

#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <vector>

#include "hotstuff/adaptive_v2_readiness_wire.h"
#include "hotstuff/evidence_lifecycle_wire.h"

namespace hotstuff
{

class AdaptiveV2ManagerSession;

constexpr std::size_t kMaximumAdaptiveV2ManagerMembers = 4096;
constexpr std::size_t
    kMaximumAdaptiveV2PendingLifecycleFactsPerSource = 64;

struct AdaptiveV2ManagerIngressLimits
{
    std::size_t maximum_members{kMaximumAdaptiveV2ManagerMembers};
    AdaptiveV2ReadinessWireLimits readiness_wire;
    ProposalLifecycleWireLimits lifecycle_wire;
    EvidenceWireLimits evidence_wire;
    ProposalEvidenceIndexLimits proposal_index;
    EvidenceStoreLimits evidence_store;
    EvidenceLifecycleLimits lifecycle;
    EvidenceLifecycleAccountingLimits lifecycle_accounting;
    std::size_t maximum_pending_lifecycle_facts_per_source{
        kMaximumAdaptiveV2PendingLifecycleFactsPerSource};
};

enum class AdaptiveV2ManagerIngressStatus : std::uint8_t
{
    processed = 1,
    rejected_nonmember,
    rejected_spoofed_source,
    rejected_sequence,
    rejected_configuration,
    rejected_generation,
    rejected_height_regression,
    awaiting_corroboration,
    already_applied,
    rejected_capacity,
    rejected_wire,
    rejected_lifecycle,
    evidence_unhealthy,
    stopped,
};

struct AdaptiveV2ManagerReadinessStats
{
    std::size_t total_members{0};
    std::size_t ready_members{0};
    std::uint64_t accepted_notices{0};
    std::uint64_t rejected_notices{0};
    bool all_members_ready{false};
};

struct AdaptiveV2ManagerIngressAuditStats
{
    std::uint64_t readiness_wire_rejections{0};
    std::uint64_t lifecycle_wire_rejections{0};
    std::uint64_t evidence_wire_rejections{0};
    std::uint64_t nonmember_rejections{0};
    std::uint64_t spoofed_source_rejections{0};
    std::uint64_t state_rejections{0};
    std::uint64_t evidence_sequence_rejections{0};
    std::uint64_t lifecycle_fence_mismatch_rejections{0};
    std::uint64_t lifecycle_quota_rejections{0};
    std::uint64_t capacity_failures{0};
    std::size_t lifecycle_corroboration_threshold{0};
    std::size_t pending_lifecycle_facts{0};
    std::size_t pending_lifecycle_associations{0};
    std::size_t reporter_causal_retained_proposals{0};
    std::size_t reporter_causal_open_reporters{0};
};

struct AdaptiveV2ManagerReadinessResult
{
    AdaptiveV2ManagerIngressStatus status{
        AdaptiveV2ManagerIngressStatus::evidence_unhealthy};
    std::optional<AdaptiveV2ReadinessWireError> wire_error;
    AdaptiveV2ManagerReadinessStats stats;
};

struct AdaptiveV2ManagerLifecycleResult
{
    AdaptiveV2ManagerIngressStatus status{
        AdaptiveV2ManagerIngressStatus::evidence_unhealthy};
    std::optional<ProposalLifecycleWireError> wire_error;
    ProposalLifecycleApplyResult lifecycle;
};

struct AdaptiveV2ManagerEvidenceResult
{
    AdaptiveV2ManagerIngressStatus status{
        AdaptiveV2ManagerIngressStatus::evidence_unhealthy};
    std::optional<EvidenceWireError> wire_error;
    std::size_t decoded_observations{0};
    std::size_t processed_observations{0};
    std::size_t accepted_observations{0};
    std::size_t rejected_observations{0};
    std::size_t newly_quarantined_observations{0};
    std::size_t duplicate_quarantined_observations{0};
    std::size_t quarantine_capacity_rejections{0};
    std::size_t remaining_quarantined_observations{0};
    std::uint64_t ledger_high_watermark{0};
};

/**
 * Single-writer manager ingress core for sequential exact epochs.
 *
 * Construction accepts only the trusted epoch-zero bootstrap. Later exact
 * epochs are reached through rotate_to_successor(), which preserves the
 * predecessor chain and assigns the canonical epoch-packed generation.
 *
 * Authentication is supplied by the transport owner as an already mapped
 * configured ReplicaID. This class owns no TLS, sockets, clocks, crash
 * identities, scoring, selection, signing, topology generation, activation,
 * voting, certificate, or quorum authority. Its quorum metadata is immutable
 * descriptive state derived from the fixed N=3f+1 membership.
 */
class AdaptiveV2ManagerIngress final
{
public:
    AdaptiveV2ManagerIngress(
        std::vector<ReplicaID> membership,
        EpochDefinitionInput initial_epoch,
        std::uint32_t active_tree_id,
        std::uint64_t activation_generation,
        AdaptiveV2ManagerIngressLimits limits = {});
    ~AdaptiveV2ManagerIngress();

    AdaptiveV2ManagerIngress(
        const AdaptiveV2ManagerIngress &) = delete;
    AdaptiveV2ManagerIngress &operator=(
        const AdaptiveV2ManagerIngress &) = delete;
    AdaptiveV2ManagerIngress(
        AdaptiveV2ManagerIngress &&) = delete;
    AdaptiveV2ManagerIngress &operator=(
        AdaptiveV2ManagerIngress &&) = delete;

    AdaptiveV2ManagerReadinessResult ingest_readiness(
        const AuthenticatedReporter &authenticated_source,
        const MsgAdaptiveV2ReadinessNotice &message) noexcept;

    AdaptiveV2ManagerReadinessResult ingest_readiness(
        const AuthenticatedReporter &authenticated_source,
        const bytearray_t &canonical_payload) noexcept;

    AdaptiveV2ManagerLifecycleResult ingest_lifecycle(
        const AuthenticatedReporter &authenticated_source,
        const MsgProposalLifecycleNotice &message) noexcept;

    AdaptiveV2ManagerLifecycleResult ingest_lifecycle(
        const AuthenticatedReporter &authenticated_source,
        const bytearray_t &canonical_payload) noexcept;

    AdaptiveV2ManagerEvidenceResult ingest_evidence(
        const AuthenticatedReporter &authenticated_reporter,
        const MsgEvidenceReport &message) noexcept;

    AdaptiveV2ManagerEvidenceResult ingest_evidence(
        const AuthenticatedReporter &authenticated_reporter,
        const bytearray_t &canonical_payload) noexcept;

    /**
     * Replace the mutable ingress window with one for the exact successor.
     *
     * The successor must retain the fixed membership, name the current epoch
     * by number and digest as its exact predecessor, and contain the selected
     * active tree. Session-wide authenticated source sequences survive the
     * rotation while readiness, lifecycle, and evidence state starts empty.
     */
    AdaptiveV2ManagerIngressStatus rotate_to_successor(
        const EpochDefinitionInput &successor,
        std::uint32_t active_tree_id) noexcept;

    const std::vector<ReplicaID> &membership() const noexcept;
    const ByzantineQuorum &quorum_metadata() const noexcept;
    const ConfigurationId &current_configuration() const noexcept;
    std::uint64_t activation_generation() const noexcept;

    const EpochDefinition &current_epoch() const noexcept;
    const EvidenceLedger &ledger() const noexcept;
    AdaptiveV2ManagerReadinessStats readiness_stats() const noexcept;
    EvidenceLifecycleStats lifecycle_stats() const noexcept;
    AdaptiveV2ManagerIngressAuditStats audit_stats() const noexcept;

    /**
     * Whether a derived Byzantine quorum of distinct members is ready.
     *
     * This is operational observation only. It neither changes the fixed
     * quorum metadata nor grants consensus or activation authority.
     */
    bool operationally_ready() const noexcept;
    bool all_members_ready() const noexcept;
    bool healthy() const noexcept;
    void shutdown() noexcept;

private:
    friend class AdaptiveV2ManagerSession;

    AdaptiveV2ManagerIngressStatus prepare_same_epoch_window_reset()
        noexcept;
    AdaptiveV2ManagerIngressStatus prepare_successor_rotation(
        const EpochDefinitionInput &successor,
        std::uint32_t active_tree_id) noexcept;
    AdaptiveV2ManagerIngressStatus seed_prepared_successor_readiness(
        const std::vector<ReplicaID> &sources,
        std::uint64_t committed_height) noexcept;
    void publish_prepared_window() noexcept;
    void discard_prepared_window() noexcept;

    struct State;
    std::unique_ptr<State> state_;
};

} // namespace hotstuff

#endif
